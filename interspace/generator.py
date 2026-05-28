"""HTML page generator for Interspace.

Reads an Interspace JSON input file, validates it against the schema in
docs/INPUT_SCHEMA.md, and renders a set of static HTML pages plus copies the
JS/CSS asset bundle. Output layout:

    output_dir/
      index.html
      lattice.html                    3D force-directed graph
      clusters/{cluster_id}.html      one per cluster
      nodes/{node_id}.html            one per node
      static/
        js/three.min.js               3D renderer (peer dep of 3d-force-graph)
        js/3d-force-graph.min.js      3D force-directed graph engine
        js/lattice_3d.js              lattice init + zoom controls + nav
        js/theme.js                   dark mode toggle
        css/style.css

Templates live in `<package>/../templates/`, assets in `<package>/../static/`.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import re

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .cross_refs import fnv1a_128_hex
from .validator import validate_input

# Node kinds whose lattice-JSON nodes carry a slim `meta` payload (kind +
# sig128 + stitch fields) so the live red runner can do its discovery
# passes without needing the full content embedded in lattice.html.
_RUNNER_VISIBLE_KINDS = frozenset({
    "paragraph", "section_anchor", "chat_turn",
    "finding", "observation", "page", "document_section",
})

# Dense-document sectioning. Files whose paragraph/page child count exceeds
# _DENSE_DOC_MIN_CHILDREN trigger an extra layer: synthetic `document_section`
# anchors group the children into ~_SECTION_CHUNK_SIZE-item chunks so the
# lattice renders the file as one heavy anchor + stepped section branches
# (one per chunk) + paragraph leaves — instead of a flat 500-paragraph radial.
_DENSE_DOC_MIN_CHILDREN = 30
_SECTION_CHUNK_SIZE = 20

# Short-run page consolidation thresholds. Paragraphs below this char_count
# get rolled into a `page` anchor when 2+ appear consecutively in the same
# source_file — atomized text collapses back into navigable pages instead
# of staying as a sea of individual fragments.
_PAGE_CONSOLIDATE_SHORT_CHARS = 120
_PAGE_CONSOLIDATE_MIN_RUN = 2
_PAGE_ID_PART_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Seam-binding heuristics. Detect "spurious seams" — paragraph breaks
# that artificially chop a continuous thought — and merge the pair into
# a single node, eliminating the noise atom.
_SEAM_CONTINUER_END_RE = re.compile(r"[:;,→—]\s*$|—\s*$")
_SEAM_SENTENCE_TERMINATOR_RE = re.compile(r'[.!?][\"\')\]\}]?\s*$')
_SEAM_ANAPHORIC_START_RE = re.compile(
    r"^\s*(That|This|These|Those|It|Its|They|Their|Indeed|Therefore|"
    r"Furthermore|Moreover|However|But|And|So|Then|Hence|Thus|Also|"
    r"Additionally|Specifically|Namely)\b",
    re.IGNORECASE,
)
_SEAM_LIST_MARKER_START_RE = re.compile(r"^\s*([-*•‣▪–—]|\d+[.)]|[a-zA-Z][.)])\s")


def _safe_id_part(s: str) -> str:
    return _PAGE_ID_PART_RE.sub("_", s).strip("_")[:60]


def _is_spurious_seam(
    a_meta: dict[str, Any], b_meta: dict[str, Any]
) -> bool:
    """Heuristic seam classifier. Returns True if the paragraph break
    between A and B is likely spurious (mid-thought break) and should
    be erased by binding the two nodes.

    Signals (ANY fires → spurious):
      1. A is short AND ends with continuation punctuation (`:` `;` `,`
         `—` `→`)         → header/lead-in continues into B
      2. A is short AND lacks any sentence terminator (`.` `!` `?`)
                          → mid-sentence break
      3. A is short AND B starts with anaphoric reference (That, This,
         These, Indeed, Therefore, ...)   → reference back to A
      4. A ends with `:` AND B starts with a list marker (-, *, •, 1.,
         a., etc.)        → introducer + list

    NEVER bind:
      - Across kind=section_anchor (meaningful boundary)
      - Across conversation_segment_id change (segment boundary)
      - When A's first line starts with `#` (treat as header)
    """
    if a_meta.get("kind") == "section_anchor" or b_meta.get("kind") == "section_anchor":
        return False
    seg_a = a_meta.get("conversation_segment_id")
    seg_b = b_meta.get("conversation_segment_id")
    if seg_a is not None and seg_b is not None and seg_a != seg_b:
        return False

    a_content = (a_meta.get("content") or "").rstrip()
    b_content = (b_meta.get("content") or "").lstrip()
    if not a_content or not b_content:
        return False
    # Headers stay as their own node — don't absorb them into the next
    if a_content.lstrip().startswith("#"):
        return False

    a_cc = a_meta.get("char_count") or len(a_content)
    a_short = a_cc < _PAGE_CONSOLIDATE_SHORT_CHARS

    # Rule 1: short A ends with continuation cue
    if a_short and _SEAM_CONTINUER_END_RE.search(a_content):
        return True
    # Rule 2: short A lacks sentence terminator
    if a_short and not _SEAM_SENTENCE_TERMINATOR_RE.search(a_content):
        return True
    # Rule 3: short A + anaphoric next
    if a_short and _SEAM_ANAPHORIC_START_RE.match(b_content):
        return True
    # Rule 4: A ends with `:` + B is a list
    if a_content.endswith(":") and _SEAM_LIST_MARKER_START_RE.match(b_content):
        return True
    return False


def _bind_spurious_seams(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[int, int]:
    """Pre-consolidation seam-binding pass. For each adjacent paragraph
    pair in the same source_file, if the seam looks spurious (a thought
    chopped in two), absorb A INTO B: concatenate A's content into B,
    re-route any edges pointing at A so they target B, then remove A
    from the node list.

    Iteration walks forward; chains (A→B→C all binding) collapse
    naturally because B's char_count grows after absorbing A, which
    may or may not still trigger the short-paragraph rules into C.

    Returns (pairs_merged, edges_dropped).
    """
    from collections import defaultdict

    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        meta = n.get("meta") or {}
        if meta.get("kind") not in ("paragraph", "chat_turn"):
            continue
        sf = meta.get("source_file")
        idx = meta.get("paragraph_index")
        if not sf or not isinstance(idx, int):
            continue
        by_file[sf].append(n)

    merged: dict[str, str] = {}  # absorbed_id → survivor_id
    pairs_merged = 0

    for sf, items in by_file.items():
        items.sort(key=lambda n: (n.get("meta") or {}).get("paragraph_index", 999999))
        for i in range(len(items) - 1):
            a = items[i]
            b = items[i + 1]
            if a["id"] in merged:
                continue
            a_meta = a.get("meta") or {}
            b_meta = b.get("meta") or {}
            a_idx = a_meta.get("paragraph_index")
            b_idx = b_meta.get("paragraph_index")
            if not isinstance(a_idx, int) or not isinstance(b_idx, int):
                continue
            if b_idx != a_idx + 1:
                continue
            if not _is_spurious_seam(a_meta, b_meta):
                continue
            # Merge A INTO B
            a_content = a_meta.get("content") or ""
            b_content = b_meta.get("content") or ""
            merged_content = a_content.rstrip() + "\n\n" + b_content.lstrip()
            b_meta["content"] = merged_content
            b_meta["char_count"] = len(merged_content)
            # Track that B's content starts at A's paragraph_index
            existing_start = b_meta.get("paragraph_index_start")
            if existing_start is None or a_idx < existing_start:
                b_meta["paragraph_index_start"] = a_idx
            # Refresh sig128
            if "sig128" in b_meta or "sig128" in a_meta:
                b_meta["sig128"] = fnv1a_128_hex(merged_content)
            merged[a["id"]] = b["id"]
            pairs_merged += 1

    if not merged:
        return (0, 0)

    # Remove absorbed nodes
    nodes[:] = [n for n in nodes if n["id"] not in merged]

    # Re-route edges. Resolve chains: if A merged → B and B merged → C,
    # then an edge to A becomes an edge to C.
    def _ep(e: dict[str, Any], key: str) -> str:
        v = e.get(key)
        if isinstance(v, dict):
            return v.get("id", "") or ""
        return v or ""

    def _resolve(node_id: str) -> str:
        seen_local: set[str] = set()
        while node_id in merged and node_id not in seen_local:
            seen_local.add(node_id)
            node_id = merged[node_id]
        return node_id

    new_edges: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    edges_dropped = 0
    for e in edges:
        s = _resolve(_ep(e, "source"))
        t = _resolve(_ep(e, "target"))
        if s == t or not s or not t:
            edges_dropped += 1
            continue
        key = (s, t, e.get("kind", "related"))
        if key in seen_keys:
            edges_dropped += 1
            continue
        seen_keys.add(key)
        ne = dict(e)
        ne["source"] = s
        ne["target"] = t
        new_edges.append(ne)
    edges[:] = new_edges
    return (pairs_merged, edges_dropped)


def _consolidate_short_runs_into_pages(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[int, int, int]:
    """Post-merge SUBTRACTIVE consolidation: runs of consecutive short
    paragraphs in the same source_file collapse into a single `page` node;
    the fragment paragraphs themselves are REMOVED from the node list and
    their inbound/outbound edges re-routed to the page.

    Atomized text (chat archives, paragraph-exploded docs) becomes tens of
    thousands of individual paragraph nodes — far too granular for the
    operator to navigate. A `datastring` (continuous prose chopped into
    short fragments) shouldn't surface as N atoms of nothing; it should
    surface as one navigable page per run.

    Operation (mutates `nodes` and `edges` in place):
      1. Detect each run of MIN_RUN+ consecutive short paragraphs per
         source_file (sorted by paragraph_index).
      2. Build one `page` node per run. Its `meta.content` is the
         concatenated full text of all member fragments — the page IS
         the text now, not a container. `meta.fragment_count`,
         `meta.paragraph_index_start/end` record the provenance.
      3. Re-route every edge that referenced a fragment ID to point at
         the page ID instead. Sequence edges where BOTH endpoints
         belong to the same run dissolve (they were internal to the
         page's text). Cross-run sequence edges become page↔page or
         page↔neighbour-paragraph.
      4. DELETE the fragment nodes from `nodes`.

    Mixed runs (long↔short alternating) and isolated short paragraphs
    (run_size < MIN_RUN) are left untouched.

    Returns (pages_added, fragments_removed, edges_dropped).
    """
    from collections import defaultdict

    # Step 1: collect short paragraphs by source_file
    by_file: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for n in nodes:
        meta = n.get("meta") or {}
        if meta.get("kind") != "paragraph":
            continue
        sf = meta.get("source_file")
        idx = meta.get("paragraph_index")
        cc = meta.get("char_count")
        if (
            not sf
            or not isinstance(idx, int)
            or not isinstance(cc, int)
        ):
            continue
        if cc >= _PAGE_CONSOLIDATE_SHORT_CHARS:
            continue
        by_file[sf].append((idx, n))

    runs: list[tuple[str, list[dict[str, Any]]]] = []
    for sf, items in by_file.items():
        items.sort(key=lambda t: t[0])
        run: list[dict[str, Any]] = []
        prev_idx: int | None = None
        for idx, n in items:
            if prev_idx is None or idx == prev_idx + 1:
                run.append(n)
            else:
                if len(run) >= _PAGE_CONSOLIDATE_MIN_RUN:
                    runs.append((sf, run))
                run = [n]
            prev_idx = idx
        if len(run) >= _PAGE_CONSOLIDATE_MIN_RUN:
            runs.append((sf, run))

    if not runs:
        return (0, 0, 0)

    # Step 2: build pages + the fragment→page remap
    fragment_to_page: dict[str, str] = {}  # fragment node id → page id
    new_pages: list[dict[str, Any]] = []
    page_count = 0
    for sf, members in runs:
        page_id = f"page.{_safe_id_part(sf)}.{page_count:04d}"
        page_count += 1
        first = members[0]
        first_meta = first.get("meta") or {}
        # Concatenate full content into the page's text
        chunks: list[str] = []
        for m in members:
            c = (m.get("meta") or {}).get("content") or ""
            if c:
                chunks.append(c)
        merged_content = "\n\n".join(chunks)
        # Label: ~first ~3 fragment previews joined
        preview_parts: list[str] = []
        for m in members[:3]:
            mc = (m.get("meta") or {}).get("content") or m.get("label") or ""
            preview_parts.append(mc[:80].strip())
        label = (" · ".join(p for p in preview_parts if p))[:200] or sf
        cluster = first.get("cluster", "")
        idx_start = first_meta.get("paragraph_index")
        idx_end = (members[-1].get("meta") or {}).get("paragraph_index")
        tags = ["page", "consolidated"]
        for t in (first.get("tags") or []):
            if t not in tags:
                tags.append(t)
        # sig128 of merged content for downstream similarity passes
        sig = fnv1a_128_hex(merged_content) if merged_content else None

        page_meta: dict[str, Any] = {
            "kind": "page",
            "source_file": sf,
            "content": merged_content,
            "char_count": len(merged_content),
            "fragment_count": len(members),
            "paragraph_index_start": idx_start,
            "paragraph_index_end": idx_end,
            "created_at": first_meta.get("created_at"),
        }
        if sig:
            page_meta["sig128"] = sig

        new_pages.append({
            "id": page_id,
            "label": label,
            "cluster": cluster,
            "weight": 1.5,  # heavier than constituent paragraphs (default 1.0)
            "tags": tags,
            "meta": page_meta,
        })
        for m in members:
            fragment_to_page[m["id"]] = page_id

    # Step 3: delete fragment nodes from the list
    nodes_before = len(nodes)
    nodes[:] = [n for n in nodes if n["id"] not in fragment_to_page]
    fragments_removed = nodes_before - len(nodes)

    # Step 4: append the new page nodes
    nodes.extend(new_pages)

    # Step 5: re-route or drop edges that reference fragment ids
    def _ep(e: dict[str, Any], key: str) -> str:
        v = e.get(key)
        if isinstance(v, dict):
            return v.get("id", "") or ""
        return v or ""

    new_edges: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    edges_dropped = 0
    for e in edges:
        s = _ep(e, "source")
        t = _ep(e, "target")
        s_was_frag = s in fragment_to_page
        t_was_frag = t in fragment_to_page
        ns = fragment_to_page.get(s, s)
        nt = fragment_to_page.get(t, t)
        # Dissolve self-edges that arise from collapsing fragments-within-run
        if ns == nt:
            edges_dropped += 1
            continue
        # Deduplicate after rerouting
        key = (ns, nt, e.get("kind", "related"))
        if key in seen_keys:
            edges_dropped += 1
            continue
        seen_keys.add(key)
        if s_was_frag or t_was_frag:
            ne = dict(e)
            ne["source"] = ns
            ne["target"] = nt
            new_edges.append(ne)
        else:
            new_edges.append(e)
    edges[:] = new_edges

    return (page_count, fragments_removed, edges_dropped)


# Markers used to detect natural section boundaries inside dense
# documents (markdown / chat-archive / governance prose). Any paragraph
# whose first non-empty line matches one of these patterns is treated
# as a section header. We prefer natural sections over mechanical
# chunking when 2+ are found in a file.
_SECTION_HEADER_PATTERNS = [
    # Markdown H1-H4: "# Title", "## 6A. Triangulation"
    re.compile(r"^\s*#{1,4}\s+\S"),
    # Numbered/lettered with capital: "1. Authority", "6A. Triangulation"
    re.compile(r"^\s*\d+[A-Z]?\.\s+[A-Z]"),
    # Verbose prefixes: "Part 3:", "Section 6", "Chapter 2", "Phase 1"
    re.compile(r"^\s*(Part|Section|Chapter|Phase|Step|Topic)\s+\d+", re.IGNORECASE),
    # Roman numeral header: "IV. ", "VII. "
    re.compile(r"^\s*[IVX]{1,5}\.\s+[A-Z]"),
]


def _looks_like_section_header(content: str) -> bool:
    if not content:
        return False
    first_line = content.lstrip().split("\n", 1)[0]
    if len(first_line) > 200:
        return False  # too long to be a header
    for rx in _SECTION_HEADER_PATTERNS:
        if rx.match(first_line):
            return True
    # ALL CAPS header (mostly uppercase letters, 5-80 chars, single line)
    stripped = first_line.strip().rstrip(":")
    if 5 <= len(stripped) <= 80 and "\n" not in stripped:
        letters = [c for c in stripped if c.isalpha()]
        if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.85:
            return True
    return False


def _section_label_from_header(content: str) -> str:
    """Derive a clean label from a header paragraph: strip markdown #'s,
    collapse whitespace, trim to a reasonable length."""
    if not content:
        return ""
    first_line = content.lstrip().split("\n", 1)[0].strip()
    # Strip leading markdown #'s
    while first_line.startswith("#"):
        first_line = first_line[1:].lstrip()
    return first_line[:160]


def _consolidate_dense_docs_into_sections(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[int, int]:
    """Second consolidation pass: dense file anchors get a mid-tier of
    `document_section` nodes. For each file with > _DENSE_DOC_MIN_CHILDREN
    paragraph/page children, look for NATURAL section headers in the
    children's content (markdown #, numbered, ALL CAPS, etc.). If 2+
    headers are found, group children by section (header paragraph
    becomes the section's label; subsequent children become its
    contents). If no natural headers found, fall back to mechanical
    ~_SECTION_CHUNK_SIZE chunking.

    Either way, the `contains` edge re-routes from
        file → contains → child × N
    to
        file → contains → section → contains → child × ~chunk
    giving the lattice a stepped mid-tier instead of a flat radial.

    Returns (sections_added, edges_rerouted).
    """
    from collections import defaultdict

    # Index file anchors and their existing children
    file_meta_by_id: dict[str, dict[str, Any]] = {}
    file_cluster_by_id: dict[str, str] = {}
    for n in nodes:
        if (n.get("meta") or {}).get("kind") == "file":
            file_meta_by_id[n["id"]] = n.get("meta") or {}
            file_cluster_by_id[n["id"]] = n.get("cluster", "")
    if not file_meta_by_id:
        return (0, 0)

    def _ep(e: dict[str, Any], key: str) -> str:
        v = e.get(key)
        if isinstance(v, dict):
            return v.get("id", "") or ""
        return v or ""

    file_to_children: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.get("kind") != "contains":
            continue
        s = _ep(e, "source")
        t = _ep(e, "target")
        if s in file_meta_by_id and t:
            file_to_children[s].append(t)

    # Only act on dense files
    dense = {
        fid: kids for fid, kids in file_to_children.items()
        if len(kids) > _DENSE_DOC_MIN_CHILDREN
    }
    if not dense:
        return (0, 0)

    node_by_id = {n["id"]: n for n in nodes}

    new_sections: list[dict[str, Any]] = []
    edges_to_add: list[dict[str, Any]] = []
    drop_pairs: set[tuple[str, str]] = set()

    def _sort_key(child_id: str) -> tuple[int, str]:
        ch = node_by_id.get(child_id) or {}
        cmeta = ch.get("meta") or {}
        idx = cmeta.get("paragraph_index")
        if idx is None:
            idx = cmeta.get("paragraph_index_start")
        if not isinstance(idx, int):
            idx = 999999
        return (idx, child_id)

    total_sections = 0
    for file_id, kids in dense.items():
        meta = file_meta_by_id[file_id]
        source_file = meta.get("path") or meta.get("source_file") or ""
        cluster = file_cluster_by_id.get(file_id, "")
        sorted_kids = sorted(kids, key=_sort_key)

        # Pass 1: detect natural section headers in the children.
        # Two signals: (1) an explicit kind=section_anchor child from the
        # adapter (highest confidence), (2) a paragraph whose content
        # starts with a header pattern (fallback). Either qualifies.
        headers: list[tuple[int, str, str]] = []  # (idx_in_sorted_kids, child_id, label)
        for i, cid in enumerate(sorted_kids):
            ch = node_by_id.get(cid) or {}
            cmeta = ch.get("meta") or {}
            content = cmeta.get("content") or ch.get("label") or ""
            if cmeta.get("kind") == "section_anchor" or _looks_like_section_header(content):
                headers.append((i, cid, _section_label_from_header(content)))

        # Decide strategy
        natural = len(headers) >= 2
        if natural:
            # Build spans from natural headers: each span starts at header i
            # and runs until the next header (or end of file). Children
            # before the first header (preamble) stay as direct file children.
            spans: list[tuple[int, int, str]] = []
            for h_idx in range(len(headers)):
                start_i = headers[h_idx][0]
                end_i = headers[h_idx + 1][0] if h_idx + 1 < len(headers) else len(sorted_kids)
                spans.append((start_i, end_i, headers[h_idx][2]))
        else:
            # Mechanical fallback: even-sized chunks
            n_sections = max(2, (len(sorted_kids) + _SECTION_CHUNK_SIZE - 1) // _SECTION_CHUNK_SIZE)
            base = len(sorted_kids) // n_sections
            rem = len(sorted_kids) % n_sections
            spans = []
            start = 0
            for s_idx in range(n_sections):
                this_size = base + (1 if s_idx < rem else 0)
                spans.append((start, start + this_size, ""))
                start += this_size

        for s_idx, (start_i, end_i, natural_label) in enumerate(spans):
            chunk = sorted_kids[start_i:end_i]
            if not chunk:
                continue
            section_id = f"section.{_safe_id_part(source_file)}.{s_idx:03d}"
            if natural_label:
                label = f"§{s_idx + 1} · {natural_label}"[:160]
            else:
                first_child = node_by_id.get(chunk[0]) or {}
                first_meta = first_child.get("meta") or {}
                preview = (
                    first_meta.get("content")
                    or first_child.get("label")
                    or ""
                )[:100].strip()
                label = f"§{s_idx + 1}/{len(spans)} · {preview}"[:160]
            new_sections.append({
                "id": section_id,
                "label": label,
                "cluster": cluster,
                "weight": 1.2,
                "tags": ["filesystem", "document_section"]
                        + (["natural-section"] if natural else ["mechanical-chunk"]),
                "meta": {
                    "kind": "document_section",
                    "source_file": source_file,
                    "parent_file_id": file_id,
                    "section_index": s_idx,
                    "section_count": len(spans),
                    "child_count": len(chunk),
                    "natural_section": natural,
                },
            })
            edges_to_add.append({
                "source": file_id,
                "target": section_id,
                "kind": "contains",
                "weight": 1.0,
            })
            for cid in chunk:
                edges_to_add.append({
                    "source": section_id,
                    "target": cid,
                    "kind": "contains",
                    "weight": 1.0,
                })
                drop_pairs.add((file_id, cid))
            # Sequence between adjacent sections so navigation reads in
            # document order
            if s_idx > 0:
                prev_id = f"section.{_safe_id_part(source_file)}.{s_idx - 1:03d}"
                edges_to_add.append({
                    "source": prev_id,
                    "target": section_id,
                    "kind": "sequence",
                    "weight": 1.0,
                })
            total_sections += 1

    # Drop old file→child contains edges that are now replaced by
    # file→section→child
    edges_dropped = 0
    new_edges: list[dict[str, Any]] = []
    for e in edges:
        if e.get("kind") == "contains":
            s = _ep(e, "source")
            t = _ep(e, "target")
            if (s, t) in drop_pairs:
                edges_dropped += 1
                continue
        new_edges.append(e)
    new_edges.extend(edges_to_add)
    edges[:] = new_edges
    nodes.extend(new_sections)

    return (total_sections, edges_dropped)


_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent
_TEMPLATES_DIR = _PROJECT_DIR / "templates"
_STATIC_DIR = _PROJECT_DIR / "static"


def render_pages(
    input_path: Path,
    output_dir: Path,
    title: str | None = None,
) -> int:
    """Render Interspace JSON input to a directory of static HTML pages.

    Returns 0 on success, 2 on file/JSON errors, 3 on schema validation errors.
    """
    if not input_path.exists():
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        return 2

    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: input is not valid JSON: {e}", file=sys.stderr)
        return 2

    errors, normalized = validate_input(data)
    if errors or normalized is None:
        print(
            f"error: {len(errors)} schema validation issue(s) in {input_path}:",
            file=sys.stderr,
        )
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 3

    if title:
        normalized["meta"]["title"] = title
    elif not normalized["meta"].get("title"):
        normalized["meta"]["title"] = input_path.stem

    output_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    nodes = normalized["nodes"]
    edges = normalized["edges"]
    clusters = normalized["clusters"]
    meta = normalized["meta"]

    # Pass 1: seam-binding. Detect spurious paragraph breaks (mid-sentence,
    # header-with-colon → list, anaphoric reference) and absorb the pair
    # into a single node. Eliminates the "tiny string + full paragraph"
    # noise pattern where a continuous thought got artificially chopped.
    _seams_bound, _seam_edges = _bind_spurious_seams(nodes, edges)
    if _seams_bound:
        print(
            f"seam-binding: {_seams_bound} spurious-seam pairs merged "
            f"(-{_seams_bound} nodes, {_seam_edges} duplicate edges dropped)",
            file=sys.stderr,
        )

    # Pass 2: short-run page consolidation. Runs of consecutive short
    # paragraphs (atomized prose) collapse — fragments are removed,
    # their content absorbed into a single `page` node per run, and
    # edges to fragments re-routed to the page.
    _pages_added, _frags_removed, _edges_dropped = _consolidate_short_runs_into_pages(nodes, edges)
    if _pages_added:
        _net = _frags_removed - _pages_added
        print(
            f"consolidation: {_frags_removed} short fragments → {_pages_added} pages "
            f"(net -{_net} nodes, {_edges_dropped} internal edges dissolved)",
            file=sys.stderr,
        )

    # Dense-doc sectioning. Files with too many paragraph/page children
    # get a stepped layer: file → contains → document_section → contains
    # → paragraph/page. Gives the lattice a navigable mid-tier instead
    # of a flat 500-paragraph radial off one file anchor.
    _sections_added, _file_edges_rerouted = _consolidate_dense_docs_into_sections(nodes, edges)
    if _sections_added:
        print(
            f"sectioning:   {_sections_added} document_sections inserted "
            f"({_file_edges_rerouted} file→child edges rerouted via sections)",
            file=sys.stderr,
        )

    node_by_id = {n["id"]: n for n in nodes}
    cluster_by_id = {c["id"]: c for c in clusters}

    counts = {
        "pages": 0,
        "nodes": len(nodes),
        "edges": len(edges),
        "clusters": len(clusters),
    }

    counts["pages"] += _write_index(env, output_dir, meta, nodes, edges, clusters)
    counts["pages"] += _write_lattice(
        env, output_dir, meta, nodes, edges, clusters
    )
    counts["pages"] += _write_cluster_pages(
        env, output_dir, meta, nodes, edges, clusters, node_by_id
    )
    counts["pages"] += _write_node_pages(
        env, output_dir, meta, nodes, edges, clusters, node_by_id, cluster_by_id
    )

    _copy_static_assets(output_dir / "static")

    _write_render_meta(output_dir, normalized, counts)

    print(
        f"rendered: {counts['pages']} pages "
        f"({counts['nodes']} nodes, {counts['edges']} edges, "
        f"{counts['clusters']} clusters) -> {output_dir}",
        file=sys.stderr,
    )
    return 0


def _write_render_meta(
    output_dir: Path,
    normalized: dict[str, Any],
    counts: dict[str, int],
) -> None:
    """Write a _meta.json describing this render. Consumed by the hub generator."""
    archived_count = sum(1 for n in normalized["nodes"] if n.get("archived"))
    epoch_min_ms, epoch_max_ms = _node_epoch_range(normalized["nodes"])
    meta = {
        "title": normalized["meta"].get("title") or output_dir.name,
        "description": normalized["meta"].get("description"),
        "source": normalized["meta"].get("source"),
        "nodes": counts["nodes"],
        "edges": counts["edges"],
        "clusters": counts["clusters"],
        "archived": archived_count,
        "epoch_min": _ms_to_date_str(epoch_min_ms),
        "epoch_max": _ms_to_date_str(epoch_max_ms),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


_KEY_NODES_TOP_N = 50
_RECENCY_HALFLIFE_DAYS = 180.0
_PHASE_FACTOR = {"current": 1.0, "foundation": 0.7, "archived": 0.3}


def _recency_factor(node: dict[str, Any], now: datetime) -> float:
    """Exponential decay on age. Missing date → 0.5 (neutral)."""
    meta = node.get("meta") or {}
    date_str = (
        meta.get("created_at")
        or meta.get("latest_mtime")
        or meta.get("archived_at")
    )
    if not date_str:
        return 0.5
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.5
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return math.exp(-days / _RECENCY_HALFLIFE_DAYS)


def _compute_key_nodes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    top_n: int = _KEY_NODES_TOP_N,
) -> list[dict[str, Any]]:
    """Score every node by (inbound + 1) × weight × recency × phase, return
    top-N. Lets the overview page show signal instead of dumping all 30K+
    nodes as a flat list (line-bloat problem).

      - inbound:  edge-count pointing at this node (popularity proxy)
      - weight:   node.weight (adapter-supplied importance, typically 1-2)
      - recency:  exp(-days_old / 180) — half-life 180 days
      - phase:    current=1.0, foundation=0.7, archived=0.3
    """
    now = datetime.now(timezone.utc)
    inbound: dict[str, int] = defaultdict(int)
    for e in edges:
        tgt = e.get("target")
        if tgt:
            inbound[tgt] += 1

    scored: list[tuple[float, dict[str, Any]]] = []
    for n in nodes:
        weight = float(n.get("weight", 1.0)) or 1.0
        phase = n.get("phase") or ("archived" if n.get("archived") else "current")
        phase_f = _PHASE_FACTOR.get(phase, 1.0)
        score = (inbound.get(n["id"], 0) + 1) * weight * _recency_factor(n, now) * phase_f
        scored.append((score, n))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [n for (_, n) in scored[:top_n]]


def _write_index(
    env: Environment,
    output_dir: Path,
    meta: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> int:
    key_nodes = _compute_key_nodes(nodes, edges)
    html = env.get_template("index.html").render(
        asset_prefix="",
        meta=meta,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
        key_nodes=key_nodes,
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    return 1


def _write_lattice(
    env: Environment,
    output_dir: Path,
    meta: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> int:
    """Render the lattice page (3D force-directed graph via 3d-force-graph)."""
    lattice_payload = {
        "nodes": [_lattice_node(n) for n in nodes],
        "edges": [
            {
                "source": e["source"],
                "target": e["target"],
                "kind": e["kind"],
                "weight": e["weight"],
            }
            for e in edges
        ],
        "clusters": [
            {"id": c["id"], "label": c["label"], "color": c.get("color")}
            for c in clusters
        ],
    }
    # Defensive: escape any `</` so the JSON can't terminate the surrounding
    # <script type="application/json"> block.
    lattice_json = (
        json.dumps(lattice_payload, separators=(",", ":"))
        .replace("</", "<\\/")
    )
    html = env.get_template("lattice.html").render(
        asset_prefix="",
        meta=meta,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
        lattice_data_json=lattice_json,
    )
    (output_dir / "lattice.html").write_text(html, encoding="utf-8")
    return 1


def _lattice_node(n: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": n["id"],
        "label": n["label"],
        "cluster": n["cluster"],
        "weight": n["weight"],
        "tags": n["tags"],
    }
    ts = _node_created_ms(n)
    if ts is not None:
        out["ts"] = ts
    if n.get("archived"):
        out["archived"] = True
        if n.get("archived_at"):
            out["archived_at"] = n["archived_at"]
    phase = n.get("phase")
    if phase and phase != "current":
        out["phase"] = phase
    # Document anchor (Pi-style middle layer between cluster and atom).
    # For filesystem_tree paragraph nodes, group by source_file so cose can
    # apply nested compound containment.
    meta = n.get("meta") or {}
    kind = meta.get("kind")
    if kind == "paragraph" and meta.get("source_file"):
        out["document"] = meta["source_file"]

    # Slim runner-visible meta — kind for filtering + sig128 for duplicate
    # detection + paragraph_index/source_file/char_count for the red
    # runner's short-run stitch pass. Backfills sig128 if the upstream
    # adapter didn't compute it. Keeps lattice.html bounded — no content
    # blobs — while giving live runners enough surface to do their job.
    if kind in _RUNNER_VISIBLE_KINDS:
        runner_meta: dict[str, Any] = {"kind": kind}
        sig = meta.get("sig128")
        if not sig:
            content = meta.get("content") or ""
            if content:
                sig = fnv1a_128_hex(content)
        if sig:
            runner_meta["sig128"] = sig
        # Fields the red runner uses for short-fragment stitching.
        if meta.get("source_file"):
            runner_meta["source_file"] = meta["source_file"]
        if isinstance(meta.get("paragraph_index"), int):
            runner_meta["paragraph_index"] = meta["paragraph_index"]
        # char_count is small; backfill from content length if absent.
        cc = meta.get("char_count")
        if cc is None and meta.get("content"):
            cc = len(meta["content"])
        if isinstance(cc, int):
            runner_meta["char_count"] = cc
        out["meta"] = runner_meta
    return out


def _node_created_ms(n: dict[str, Any]) -> int | None:
    meta = n.get("meta") or {}
    raw = meta.get("created_at")
    if not isinstance(raw, str):
        return None
    return _parse_iso_to_ms(raw)


def _parse_iso_to_ms(raw: str) -> int | None:
    # Tolerate a trailing "Z" combined with a numeric offset (e.g.
    # "2026-04-22T22:43:06+00:00Z") by stripping a redundant "Z".
    s = raw.strip()
    if s.endswith("Z") and ("+" in s[10:] or "-" in s[11:]):
        s = s[:-1]
    elif s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _node_epoch_range(
    nodes: list[dict[str, Any]],
) -> tuple[int | None, int | None]:
    stamps = [_node_created_ms(n) for n in nodes]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def _ms_to_date_str(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _write_cluster_pages(
    env: Environment,
    output_dir: Path,
    meta: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
) -> int:
    cluster_dir = output_dir / "clusters"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    template = env.get_template("cluster.html")
    written = 0

    for cluster in clusters:
        cid = cluster["id"]
        cluster_nodes = [n for n in nodes if n["cluster"] == cid]
        intra_edges: list[dict[str, Any]] = []
        cross_edges: list[dict[str, Any]] = []

        for edge in edges:
            src_node = node_by_id.get(edge["source"])
            tgt_node = node_by_id.get(edge["target"])
            if src_node is None or tgt_node is None:
                continue
            src_cluster = src_node["cluster"]
            tgt_cluster = tgt_node["cluster"]
            enriched = dict(edge)
            enriched["source_label"] = src_node["label"]
            enriched["target_label"] = tgt_node["label"]
            enriched["source_cluster"] = src_cluster
            enriched["target_cluster"] = tgt_cluster
            if src_cluster == cid and tgt_cluster == cid:
                intra_edges.append(enriched)
            elif src_cluster == cid or tgt_cluster == cid:
                cross_edges.append(enriched)

        html = template.render(
            asset_prefix="../",
            meta=meta,
            nodes=nodes,
            edges=edges,
            clusters=clusters,
            cluster=cluster,
            cluster_nodes=cluster_nodes,
            intra_edges=intra_edges,
            cross_edges=cross_edges,
        )
        (cluster_dir / f"{cid}.html").write_text(html, encoding="utf-8")
        written += 1

    return written


def _compute_document_paragraphs(
    nodes: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """For each paragraph / section_anchor node AND its file-anchor parent,
    return the ordered list of all paragraphs from the same `meta.source_file`.
    File anchors map by `meta.path == source_file`. Empty for nodes not
    associated with a paragraph-split source file.
    """
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        m = n.get("meta") or {}
        if m.get("kind") in ("paragraph", "section_anchor") and m.get("source_file"):
            by_source[str(m["source_file"])].append(n)

    for paras in by_source.values():
        paras.sort(
            key=lambda n: (n.get("meta") or {}).get("paragraph_index", 0)
        )

    result: dict[str, list[dict[str, Any]]] = {}
    for paras in by_source.values():
        for n in paras:
            result[n["id"]] = paras

    # File-anchor nodes map to their contained paragraphs by path == source_file.
    # Clicking a file anchor (e.g., from a `full_doctrine_of` edge) then shows
    # the full source body scrollable, same as a paragraph node would.
    for n in nodes:
        m = n.get("meta") or {}
        if m.get("kind") == "file" and m.get("path"):
            paras = by_source.get(str(m["path"]))
            if paras:
                result[n["id"]] = paras

    # Conversation-segment anchors map to ONLY their member chat_turns
    # (the slice of paragraphs in start_paragraph_index..end_paragraph_index
    # of the source_file). Clicking a segment anchor renders the scene's
    # dialogue lines scrollable, scoped to that segment.
    for n in nodes:
        m = n.get("meta") or {}
        if m.get("kind") != "conversation_segment":
            continue
        sf = m.get("source_file")
        start_idx = m.get("start_paragraph_index")
        end_idx = m.get("end_paragraph_index")
        if not sf or start_idx is None or end_idx is None:
            continue
        all_paras = by_source.get(str(sf), [])
        # Slice paragraphs whose paragraph_index is in [start, end]
        scoped = [
            p for p in all_paras
            if start_idx <= (p.get("meta") or {}).get("paragraph_index", -1) <= end_idx
        ]
        if scoped:
            result[n["id"]] = scoped

    return result


def _compute_siblings(
    nodes: list[dict[str, Any]],
) -> dict[str, tuple[str | None, str | None]]:
    """For each node id, return (prev_id, next_id) within its natural sibling group.

    Grouping: same `meta.source_file` if present (paragraph nodes from the same
    document), otherwise same cluster. Within each group, sort by
    `paragraph_index` (sequential text), else `created_at` (chronological
    findings), else id (stable fallback).
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        meta_n = n.get("meta") or {}
        if meta_n.get("source_file"):
            key = ("file", str(meta_n["source_file"]))
        else:
            key = ("cluster", str(n.get("cluster", "uncategorized")))
        groups[key].append(n)

    def sort_key(n: dict[str, Any]) -> tuple[int, Any]:
        meta_n = n.get("meta") or {}
        if "paragraph_index" in meta_n:
            try:
                return (0, int(meta_n["paragraph_index"]))
            except (TypeError, ValueError):
                pass
        if meta_n.get("created_at"):
            return (1, str(meta_n["created_at"]))
        return (2, str(n.get("id", "")))

    result: dict[str, tuple[str | None, str | None]] = {}
    for group_nodes in groups.values():
        group_nodes_sorted = sorted(group_nodes, key=sort_key)
        for i, n in enumerate(group_nodes_sorted):
            prev_id = group_nodes_sorted[i - 1]["id"] if i > 0 else None
            next_id = (
                group_nodes_sorted[i + 1]["id"]
                if i < len(group_nodes_sorted) - 1
                else None
            )
            result[n["id"]] = (prev_id, next_id)
    return result


def _write_node_pages(
    env: Environment,
    output_dir: Path,
    meta: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    cluster_by_id: dict[str, dict[str, Any]],
) -> int:
    node_dir = output_dir / "nodes"
    node_dir.mkdir(parents=True, exist_ok=True)
    template = env.get_template("node.html")
    written = 0
    siblings = _compute_siblings(nodes)
    document_paragraphs_lookup = _compute_document_paragraphs(nodes)

    for node in nodes:
        nid = node["id"]
        outgoing: list[dict[str, Any]] = []
        incoming: list[dict[str, Any]] = []
        for edge in edges:
            if edge["source"] == nid:
                tgt = node_by_id.get(edge["target"])
                if tgt is None:
                    continue
                enriched = dict(edge)
                enriched["target_label"] = tgt["label"]
                enriched["target_cluster"] = tgt["cluster"]
                outgoing.append(enriched)
            if edge["target"] == nid:
                src = node_by_id.get(edge["source"])
                if src is None:
                    continue
                enriched = dict(edge)
                enriched["source_label"] = src["label"]
                enriched["source_cluster"] = src["cluster"]
                incoming.append(enriched)

        cluster_obj = cluster_by_id.get(node["cluster"])
        node_cluster_label = cluster_obj["label"] if cluster_obj else node["cluster"]

        prev_id, next_id = siblings.get(node["id"], (None, None))
        prev_node = node_by_id.get(prev_id) if prev_id else None
        next_node = node_by_id.get(next_id) if next_id else None
        document_paragraphs = document_paragraphs_lookup.get(node["id"], [])

        html = template.render(
            asset_prefix="../",
            meta=meta,
            nodes=nodes,
            edges=edges,
            clusters=clusters,
            node=node,
            node_cluster_label=node_cluster_label,
            outgoing=outgoing,
            incoming=incoming,
            prev_node=prev_node,
            next_node=next_node,
            document_paragraphs=document_paragraphs,
        )
        (node_dir / f"{nid}.html").write_text(html, encoding="utf-8")
        written += 1

    return written


def _copy_static_assets(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(_STATIC_DIR, target)
