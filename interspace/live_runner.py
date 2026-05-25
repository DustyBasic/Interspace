"""Live discovery runner — Interspace's in-server T-cell / REL / NEG-T layer.

Three lightweight runners cycle independently in background threads, each
scanning the current lattice for new cross-source relations and broadcasting
discoveries to connected browsers via Server-Sent Events. The browser
animates these as runner-colored visual events — the lattice feels alive
between renders, with new bridges forming as the operator watches.

Architectural weighting (Interspace-light, not Pi-spec heavy):
  - No sig128 fast headers — compare `meta.content` directly (~1700 nodes fits)
  - No REL binder nodes with affect/rebound/acceptance — just typed edges
  - No slot bands or write-law gates — Interspace doesn't allocate, it shows
  - No 21% activation gate — runners just always run on the rendered data
  - Discoveries are SUGGESTIONS surfaced visibly; persistence is operator-opt-in

Runner roles:
  - t-cell : scans for pairwise cross-source mentions (cites_file, cites_section)
  - rel    : binds triples that share content/concept across docs
  - neg-t  : marks inverse-correlation pairs (ledger_inversion-style)

Each cycle:
  1. Load current lattice (embedded JSON in lattice.html)
  2. Run that runner's discovery pass (subset of extract_cross_references)
  3. Diff against known edges
  4. Broadcast each new edge as an SSE event tagged with runner_id
  5. Sleep with jitter until next cycle

The visual animation clock (runners pulsing/traveling between nodes) is
client-side and independent of the server cycle — see static/js/lattice_3d.js.
"""

from __future__ import annotations

import json
import queue
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .cross_refs import extract_cross_references


# Discovery cadence per runner. Jitter applied each cycle so the three don't
# lock-step. Adaptive sliding scale based on activity is a v2 enhancement.
DEFAULT_CYCLE_SECONDS: dict[str, tuple[float, float]] = {
    "t-cell": (120.0, 20.0),  # mean 120s ± 20s jitter
    "rel":    (150.0, 25.0),
    "neg-t":  (180.0, 30.0),
}

_LATTICE_DATA_RE = re.compile(
    r'<script id="lattice-data" type="application/json">(.+?)</script>',
    re.DOTALL,
)


class LiveRunnerState:
    """Shared state for the live runner subsystem. One instance per server."""

    def __init__(self, rendered_dir: Path):
        self.rendered_dir = rendered_dir
        self.lattice_path = rendered_dir / "lattice.html"
        self.known_edge_keys: set[tuple[str, str, str]] = set()
        self._clients: list[queue.Queue[str]] = []
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        self._lattice_cache: dict[str, Any] | None = None
        self._lattice_mtime: float = 0.0

    # ----------------------------------------------------------------
    # Lattice loading (re-reads when lattice.html mtime advances)
    # ----------------------------------------------------------------
    def lattice(self) -> dict[str, Any] | None:
        if not self.lattice_path.exists():
            return None
        mtime = self.lattice_path.stat().st_mtime
        if self._lattice_cache is not None and mtime == self._lattice_mtime:
            return self._lattice_cache
        try:
            html = self.lattice_path.read_text(encoding="utf-8")
        except OSError:
            return self._lattice_cache
        m = _LATTICE_DATA_RE.search(html)
        if not m:
            return self._lattice_cache
        raw = m.group(1).replace("<\\/", "</")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return self._lattice_cache
        self._lattice_cache = data
        self._lattice_mtime = mtime
        # Initial load also seeds known-edge set so the first cycle doesn't
        # broadcast everything that already exists in the static render.
        if not self.known_edge_keys:
            for e in data.get("edges", []):
                self.known_edge_keys.add(self._edge_key(e))
        return data

    @staticmethod
    def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str]:
        return (edge.get("source", ""), edge.get("target", ""), edge.get("kind", ""))

    # ----------------------------------------------------------------
    # SSE client registry
    # ----------------------------------------------------------------
    def register_client(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=200)
        with self._clients_lock:
            self._clients.append(q)
        return q

    def unregister_client(self, q: queue.Queue[str]) -> None:
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def broadcast(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        with self._clients_lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Slow client — drop event rather than block. SSE is best-effort.
                pass

    # ----------------------------------------------------------------
    # Runner lifecycle
    # ----------------------------------------------------------------
    def start(self, cycles: dict[str, tuple[float, float]] | None = None) -> None:
        """Start the 3 runner threads. Idempotent."""
        if self._stop.is_set():
            return  # was stopped
        cycle_map = cycles or DEFAULT_CYCLE_SECONDS
        for runner_name, (mean, jitter) in cycle_map.items():
            t = threading.Thread(
                target=self._runner_loop,
                args=(runner_name, mean, jitter),
                name=f"interspace-runner-{runner_name}",
                daemon=True,
            )
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _runner_loop(self, runner_name: str, mean: float, jitter: float) -> None:
        # First sleep before first cycle so we don't slam-fire on startup
        time.sleep(min(30.0, mean / 4))
        while not self._stop.is_set():
            try:
                self._run_one_cycle(runner_name)
            except Exception:  # noqa: BLE001 — runner must not crash the server
                pass
            sleep_for = max(10.0, mean + random.uniform(-jitter, jitter))
            # Wake every second so we can stop responsively
            slept = 0.0
            while slept < sleep_for and not self._stop.is_set():
                time.sleep(1.0)
                slept += 1.0

    # ----------------------------------------------------------------
    # Discovery passes — one per runner kind
    # ----------------------------------------------------------------
    def _run_one_cycle(self, runner_name: str) -> None:
        data = self.lattice()
        if not data:
            return
        nodes = data.get("nodes", [])
        if not nodes:
            return

        discovered = self._discover(runner_name, nodes)
        new_count = 0
        for edge in discovered:
            key = self._edge_key(edge)
            if key in self.known_edge_keys:
                continue
            self.known_edge_keys.add(key)
            self.broadcast({
                "type": "edge_added",
                "runner": runner_name,
                "edge": edge,
            })
            new_count += 1
        if new_count:
            self.broadcast({
                "type": "cycle_complete",
                "runner": runner_name,
                "added": new_count,
            })

    def _discover(
        self, runner_name: str, nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Each runner returns a subset of extract_cross_references's output,
        filtered to its own role. Lightweight — all three share the same
        regex pass, then filter by edge kind."""
        all_edges = extract_cross_references(nodes)
        if runner_name == "t-cell":
            # T-cell catches pairwise mentions across files / sections
            wanted = {"cites_file", "cites_section", "full_doctrine_of"}
        elif runner_name == "rel":
            # REL binds shared concept across docs (temporal variants form
            # cliques of 3+; counts as relational binding at Interspace weight)
            wanted = {"temporal_variant_of"}
        elif runner_name == "neg-t":
            # NEG-T scope is small at Interspace weight — defer to v2 when we
            # add an inverse-correlation detector. For now, emit nothing.
            wanted = set()
        else:
            wanted = set()
        return [e for e in all_edges if e.get("kind") in wanted]


# ----------------------------------------------------------------
# SSE helper — formats a queue.Queue stream into SSE bytes
# ----------------------------------------------------------------
def stream_events(
    q: queue.Queue[str], stop_check: Callable[[], bool], heartbeat_seconds: float = 15.0
):
    """Yield SSE-formatted byte chunks from the client queue. Sends a
    comment heartbeat every `heartbeat_seconds` to keep proxies from
    closing the connection during quiet stretches."""
    yield b"event: hello\ndata: {}\n\n"
    while not stop_check():
        try:
            data = q.get(timeout=heartbeat_seconds)
        except queue.Empty:
            yield b": heartbeat\n\n"
            continue
        yield f"data: {data}\n\n".encode("utf-8")
