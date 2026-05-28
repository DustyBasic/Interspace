"""Local HTTP server for serving a rendered Interspace directory.

Uses only stdlib (`http.server`, `socketserver`, `webbrowser`, `threading`).
Designed to be invoked via `python -m interspace serve <dir>`; see
`__main__.py` for the CLI wiring.

The `--live` flag mounts a live discovery layer: three lightweight runners
(T-cell, REL, NEG-T at Interspace weights) cycle in background threads,
scan the rendered lattice for new cross-source relations, and broadcast
discoveries via Server-Sent Events at `/api/runners/stream`. The lattice
page subscribes and animates each new edge as a runner-colored visual
event. Aesthetic + suggestion layer; persistence is operator-opt-in.
"""

from __future__ import annotations

import functools
import http.server
import sys
import webbrowser
from pathlib import Path

from .live_runner import LiveRunnerState, MultiLiveRunnerState, stream_events


def run_server(
    directory: Path,
    port: int = 8000,
    open_browser: bool = True,
    host: str = "127.0.0.1",
    live: bool = False,
) -> int:
    """Serve `directory` over HTTP on `host:port` until Ctrl-C.

    When `live=True`, mount the live runner subsystem: background runner
    threads (t-cell / rel / neg-t / red) + SSE endpoint at /api/runners/stream.

    Returns 0 on clean shutdown, 2 if the directory is missing or not a dir,
    4 if the port is already in use.
    """
    if not directory.exists():
        print(f"error: directory not found: {directory}", file=sys.stderr)
        return 2
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    runner_state: LiveRunnerState | MultiLiveRunnerState | None = None
    runner_mode: str = ""
    if live:
        # Hub layout (base_dir has subdirs each with their own lattice.html)
        # gets a MultiLiveRunnerState that runs a runner cohort per dataset
        # and merges their broadcasts onto one client list. Single-dataset
        # mode (directory itself contains lattice.html) uses the simple state.
        if (directory / "lattice.html").exists():
            runner_state = LiveRunnerState(directory)
            runner_mode = f"single ({directory.name})"
        else:
            multi = MultiLiveRunnerState(directory)
            if not multi.sub_states:
                print(
                    f"warning: --live given but no lattice.html found under "
                    f"{directory} or its subdirs; runners disabled",
                    file=sys.stderr,
                )
                runner_state = None
            else:
                runner_state = multi
                runner_mode = f"hub ({', '.join(multi.dataset_names)})"
        if runner_state is not None:
            # Force initial lattice load + edge-set seed so first cycle diffs cleanly
            runner_state.lattice()
            runner_state.start()

    handler = functools.partial(
        _InterspaceRequestHandler,
        directory=str(directory),
        runner_state=runner_state,
    )

    try:
        with http.server.ThreadingHTTPServer((host, port), handler) as httpd:
            url = f"http://{host}:{port}/"
            print(f"serving {directory} at {url}", file=sys.stderr)
            if live and runner_state is not None:
                from .live_runner import DEFAULT_CYCLE_SECONDS
                runners = ", ".join(DEFAULT_CYCLE_SECONDS.keys())
                print(
                    f"live runner: ON ({len(DEFAULT_CYCLE_SECONDS)} runners — {runners}) "
                    f"mode={runner_mode}",
                    file=sys.stderr,
                )
            print("press Ctrl-C to stop.", file=sys.stderr)
            if open_browser:
                webbrowser.open(url)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped", file=sys.stderr)
                if runner_state is not None:
                    runner_state.stop()
                return 0
    except OSError as e:
        # EADDRINUSE: 98 on Linux, 10048 on Windows
        if e.errno in (48, 98, 10048):
            print(
                f"error: port {port} already in use. "
                f"Either stop the existing server or pass --port <N>.",
                file=sys.stderr,
            )
            return 4
        raise
    return 0


class _InterspaceRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static file server + SSE endpoint for the live runner."""

    def __init__(self, *args, runner_state: LiveRunnerState | None = None, **kwargs):
        self._runner_state = runner_state
        # SimpleHTTPRequestHandler accepts `directory=...` kwarg
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        if self.path == "/api/runners/stream":
            if self._runner_state is None:
                self.send_error(404, "Live runner not enabled (start server with --live)")
                return
            self._handle_sse()
            return
        super().do_GET()

    def _handle_sse(self) -> None:
        assert self._runner_state is not None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        q = self._runner_state.register_client()
        try:
            for chunk in stream_events(q, stop_check=lambda: False):
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
        finally:
            self._runner_state.unregister_client(q)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib API
        # Quieter logging: skip SSE heartbeat noise but keep regular requests.
        # args[0] is usually the request line, but for log_error it's an
        # HTTPStatus enum — coerce defensively.
        first = str(args[0]) if args else ""
        if "/api/runners/stream" in first:
            return
        super().log_message(format, *args)
