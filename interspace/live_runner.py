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
  - neg-t  : marks inverse-correlation pairs (inverse-relation-style)

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

from collections import defaultdict

from .cross_refs import extract_cross_references, fnv1a_128_hex
from .speed_square import spurious_seam_score


# Discovery cadence per runner. Jitter applied each cycle so the four don't
# lock-step. Adaptive sliding scale based on activity is a v2 enhancement.
DEFAULT_CYCLE_SECONDS: dict[str, tuple[float, float]] = {
    "t-cell": (120.0, 20.0),  # mean 120s ± 20s jitter
    "rel":    (150.0, 25.0),
    "neg-t":  (180.0, 30.0),
    "red":    (210.0, 30.0),  # repair runner — heavier scan, less frequent
}

# Cap members per duplicate bucket to avoid edge explosion in pathological
# cases (CHECKSUMS-style files with thousands of identical lines).
_RED_DUPLICATE_BUCKET_CAP = 6
# Skip very-short paragraphs from duplicate detection — too noisy below this
# (boilerplate / ruler lines / micro-fragments collide spuriously).
_RED_DUPLICATE_MIN_CHARS = 80
# Stitch pass: paragraphs below this char_count are "chopped" — atoms of
# nothing on their own. Runs of consecutive shorts in the same source_file
# get rejoined via `stitch` edges so the operator can read the recovered
# context string instead of disjoint fragments.
_RED_SHORT_CHARS = 120
# Seam pass: confidence threshold above which a `spurious_seam` edge is
# emitted. The static generator pass (`_bind_spurious_seams`) already
# absorbed the high-precision cases at render time using binary rules;
# this runtime pass uses `speed_square.spurious_seam_score`'s continuous
# compound (continuation + lexical-chain + shingle + char-class-shift) to
# find what the static heuristics missed. 0.55 sits between speed_square's
# "cautious" (0.5) and "aggressive" (0.7) recommended cutoffs.
_RED_SEAM_THRESHOLD = 0.55
# Per-cycle broadcast cap. Red can find tens of thousands of duplicate edges
# in one pass; broadcasting them all at once overwhelms the SSE client queue
# (which caps at 200) and floods the lattice with simultaneous edge pulses.
# Cap at this many per cycle; the rest defer to subsequent cycles by leaving
# them out of known_edge_keys (next cycle re-discovers and tries again).
_RUNNER_BROADCAST_CAP_PER_CYCLE = 80

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

    def _run_one_cycle(self, runner_name: str) -> None:
        import sys as _sys
        tag = f"[{self.rendered_dir.name}/{runner_name}]"
        # Skip the cycle entirely if nobody is listening. Otherwise the
        # runner would consume new discoveries into known_edge_keys and
        # broadcast them to zero clients — by the time a client connects
        # the burst is already "known" and silently deduped next cycle.
        with self._clients_lock:
            client_count = len(self._clients)
        if client_count == 0:
            print(f"{tag} skip (0 clients)", file=_sys.stderr, flush=True)
            return
        data = self.lattice()
        if not data:
            print(f"{tag} skip (no lattice data)", file=_sys.stderr, flush=True)
            return
        nodes = data.get("nodes", [])
        if not nodes:
            return
        discovered = self._discover(runner_name, nodes)
        candidate_new: list[dict[str, Any]] = []
        for edge in discovered:
            if self._edge_key(edge) in self.known_edge_keys:
                continue
            candidate_new.append(edge)
        print(
            f"{tag} cycle: {len(discovered)} discovered, {len(candidate_new)} new, "
            f"{client_count} clients",
            file=_sys.stderr, flush=True,
        )
        # Cap broadcasts per cycle so large duplicate sets trickle out over
        # subsequent cycles rather than flooding the client queue (which
        # caps at 200) in a single burst.
        emit = candidate_new[:_RUNNER_BROADCAST_CAP_PER_CYCLE]
        # Mark only what we emit; the rest stay unknown so the next cycle
        # rediscovers and emits the next batch.
        for edge in emit:
            self.known_edge_keys.add(self._edge_key(edge))
            self.broadcast({
                "type": "edge_added",
                "runner": runner_name,
                "edge": edge,
            })
        if emit:
            import sys as _sys
            deferred = max(0, len(candidate_new) - len(emit))
            print(
                f"[{self.rendered_dir.name}/{runner_name}] +{len(emit)} edges"
                + (f" (+{deferred} deferred)" if deferred else ""),
                file=_sys.stderr, flush=True,
            )
            self.broadcast({
                "type": "cycle_complete",
                "runner": runner_name,
                "added": len(emit),
                "deferred": deferred,
            })

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
        filtered to its own role. Lightweight — t-cell/rel/neg-t share the
        same regex pass then filter by edge kind. Red runner has its own
        discovery pass (duplicate detection via sig128)."""
        if runner_name == "red":
            return self._discover_red(nodes)
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

    def _discover_red(
        self, nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Red runner: two-pass discovery.

        Pass A — near-duplicate detection: bucket text-bearing nodes by
        sig128, emit `near_duplicate_of` between members of any 2+ bucket.
        Surfaces forked-doc / pasted-block copies across source files.

        Pass B — short-fragment stitching: find runs of CONSECUTIVE short
        paragraphs in the same source_file (paragraphs below `_RED_SHORT_CHARS`
        char_count — atoms-of-nothing that mean little alone) and emit
        `stitch` edges between consecutive members of each run. Recovers
        the context string that paragraph-splitting chopped apart.

        Pass C — seam detection: walk adjacent paragraph pairs per
        source_file and score the break with `spurious_seam_score`
        (continuation_likelihood + lexical_chain_directional + shingle_overlap
        - char_class_shift). The static generator pass already absorbed
        the high-precision cases at render time; this pass catches the
        less-obvious seams the static heuristics miss (long-paragraph
        continuations, lexical chains without anaphora cues, literal
        text overlap). Above threshold, emit `spurious_seam` edges.

        All three passes share the per-cycle broadcast cap upstream so no
        single pass starves the others when buckets are huge.

        Future expansion (deferred to v0.7):
          - Citation/source repair: re-run R1/R3 cross-ref regex against
            paragraphs that didn't resolve at first pass.
          - Content_type re-classification: re-run content_classifier on
            post-merge data so unclassified nodes get typed."""
        edges: list[dict[str, Any]] = []
        edges.extend(self._discover_red_duplicates(nodes))
        edges.extend(self._discover_red_stitch(nodes))
        edges.extend(self._discover_red_seams(nodes))
        return edges

    def _discover_red_duplicates(
        self, nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for n in nodes:
            meta = n.get("meta") or {}
            if meta.get("kind") not in (
                "paragraph", "section_anchor", "chat_turn",
                "finding", "observation",
            ):
                continue
            sig = meta.get("sig128")
            if not sig:
                content = meta.get("content") or ""
                if len(content) < _RED_DUPLICATE_MIN_CHARS:
                    continue
                sig = fnv1a_128_hex(content)
            buckets[sig].append(n)

        edges: list[dict[str, Any]] = []
        for sig, members in buckets.items():
            if len(members) < 2:
                continue
            if len(members) > _RED_DUPLICATE_BUCKET_CAP:
                members = members[:_RED_DUPLICATE_BUCKET_CAP]
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    edges.append({
                        "source": members[i]["id"],
                        "target": members[j]["id"],
                        "kind": "near_duplicate_of",
                        "weight": 0.85,
                        "meta": {
                            "sig128": sig,
                            "via": "red-runner",
                        },
                    })
        return edges

    def _discover_red_stitch(
        self, nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Group consecutive short paragraphs per source_file, emit
        `stitch` edges between adjacent members of each run.

        Requirements (per node):
          - meta.kind == "paragraph"
          - meta.source_file present
          - meta.paragraph_index present (so we can sort + detect adjacency)
          - meta.char_count present and < _RED_SHORT_CHARS

        A 'run' is 2+ paragraphs in the same source_file with consecutive
        paragraph_index values, ALL of them short. The edge chain
        (run[0]→run[1]→run[2]→...) gives the operator a visual context
        string for what was over-atomized."""
        by_file: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for n in nodes:
            meta = n.get("meta") or {}
            if meta.get("kind") != "paragraph":
                continue
            sf = meta.get("source_file")
            idx = meta.get("paragraph_index")
            cc = meta.get("char_count")
            if sf is None or not isinstance(idx, int) or not isinstance(cc, int):
                continue
            if cc >= _RED_SHORT_CHARS:
                continue
            by_file[sf].append((idx, n))

        edges: list[dict[str, Any]] = []
        for sf, items in by_file.items():
            items.sort(key=lambda t: t[0])
            run: list[dict[str, Any]] = []
            prev_idx: int | None = None
            for idx, n in items:
                if prev_idx is None or idx == prev_idx + 1:
                    run.append(n)
                else:
                    self._emit_stitch_run(run, edges)
                    run = [n]
                prev_idx = idx
            self._emit_stitch_run(run, edges)
        return edges

    @staticmethod
    def _emit_stitch_run(
        run: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> None:
        if len(run) < 2:
            return
        for i in range(len(run) - 1):
            edges.append({
                "source": run[i]["id"],
                "target": run[i + 1]["id"],
                "kind": "stitch",
                "weight": 1.0,
                "meta": {"via": "red-runner-stitch", "run_size": len(run)},
            })

    def _discover_red_seams(
        self, nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Score every adjacent paragraph pair per source_file with the
        compound `spurious_seam_score` and emit `spurious_seam` edges
        above the threshold.

        Walks paragraph + chat_turn nodes. Skips pairs that cross a
        section_anchor or a conversation_segment_id change — those are
        meaningful boundaries that even strong continuation cues should
        not erase (parallel to the generator's NEVER-bind rules).

        The score and short-prev hint travel on the edge meta so the
        downstream visualization can render seam confidence and the
        operator's accept/reject UI can rank candidates.

        Per-cycle cap shares the global red broadcast budget upstream.
        """
        by_file: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for n in nodes:
            meta = n.get("meta") or {}
            if meta.get("kind") not in ("paragraph", "chat_turn"):
                continue
            sf = meta.get("source_file")
            idx = meta.get("paragraph_index")
            if not sf or not isinstance(idx, int):
                continue
            by_file[sf].append((idx, n))

        edges: list[dict[str, Any]] = []
        for sf, items in by_file.items():
            items.sort(key=lambda t: t[0])
            for i in range(len(items) - 1):
                a_idx, a = items[i]
                b_idx, b = items[i + 1]
                if b_idx != a_idx + 1:
                    continue
                a_meta = a.get("meta") or {}
                b_meta = b.get("meta") or {}
                # Hard never-bind boundaries (parallel to generator pass).
                seg_a = a_meta.get("conversation_segment_id")
                seg_b = b_meta.get("conversation_segment_id")
                if seg_a is not None and seg_b is not None and seg_a != seg_b:
                    continue
                a_text = a_meta.get("content") or ""
                b_text = b_meta.get("content") or ""
                if not a_text or not b_text:
                    continue
                a_short = bool(
                    isinstance(a_meta.get("char_count"), int)
                    and a_meta["char_count"] < _RED_SHORT_CHARS
                )
                score = spurious_seam_score(a_text, b_text, prev_short=a_short)
                if score < _RED_SEAM_THRESHOLD:
                    continue
                edges.append({
                    "source": a["id"],
                    "target": b["id"],
                    "kind": "spurious_seam",
                    "weight": round(score, 3),
                    "meta": {
                        "via": "red-runner-seam",
                        "score": round(score, 3),
                        "prev_short": a_short,
                    },
                })
        return edges


class MultiLiveRunnerState:
    """Hub-layout wrapper: one LiveRunnerState per dataset subdir, all
    sharing a single client queue. A browser subscribed to
    /api/runners/stream sees events from every dataset; the lattice
    viewer's `handleEdgeAdded` filters by `nodeIndex` so edges for
    other datasets are silently ignored at the client.

    Duck-types LiveRunnerState (register/unregister/start/stop/lattice)
    so the HTTP handler doesn't care which one it has.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._clients: list[queue.Queue[str]] = []
        self._clients_lock = threading.Lock()
        self.sub_states: list[LiveRunnerState] = []
        for sub in sorted(base_dir.iterdir(), key=lambda p: p.name.lower()):
            if not sub.is_dir():
                continue
            if not (sub / "lattice.html").exists():
                continue
            s = LiveRunnerState(sub)
            # Share the client list — broadcasts from any sub-state reach
            # all subscribed clients.
            s._clients = self._clients
            s._clients_lock = self._clients_lock
            self.sub_states.append(s)

    @property
    def dataset_names(self) -> list[str]:
        return [s.rendered_dir.name for s in self.sub_states]

    def lattice(self) -> dict[str, Any] | None:
        # Loads each sub-state's lattice so known_edge_keys is seeded.
        # Returns the first one (caller uses it only for the seed-check).
        first: dict[str, Any] | None = None
        for s in self.sub_states:
            d = s.lattice()
            if first is None:
                first = d
        return first

    def start(self, cycles: dict[str, tuple[float, float]] | None = None) -> None:
        for s in self.sub_states:
            s.start(cycles=cycles)

    def stop(self) -> None:
        for s in self.sub_states:
            s.stop()

    def register_client(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=200)
        with self._clients_lock:
            self._clients.append(q)
        return q

    def unregister_client(self, q: queue.Queue[str]) -> None:
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)


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
