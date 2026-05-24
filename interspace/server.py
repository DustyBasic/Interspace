"""Local HTTP server for serving a rendered Interspace directory.

Uses only stdlib (`http.server`, `socketserver`, `webbrowser`). Designed to
be invoked via `python -m interspace serve <dir>`; see `__main__.py` for the
CLI wiring.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
import webbrowser
from pathlib import Path


def run_server(
    directory: Path,
    port: int = 8000,
    open_browser: bool = True,
    host: str = "127.0.0.1",
) -> int:
    """Serve `directory` over HTTP on `host:port` until Ctrl-C.

    Returns 0 on clean shutdown, 2 if the directory is missing or not a dir,
    4 if the port is already in use.
    """
    if not directory.exists():
        print(f"error: directory not found: {directory}", file=sys.stderr)
        return 2
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )

    try:
        with socketserver.TCPServer((host, port), handler) as httpd:
            url = f"http://{host}:{port}/"
            print(f"serving {directory} at {url}", file=sys.stderr)
            print("press Ctrl-C to stop.", file=sys.stderr)
            if open_browser:
                webbrowser.open(url)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped", file=sys.stderr)
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
