"""Content-type classifier for text-bearing lattice nodes.

Heuristic rule-based (no ML), Interspace-weight Stage 2 enrichment.
Reads each paragraph / section_anchor / finding / observation's
`meta.content`, classifies it into a content_type label, aggregates
per-file dominant-type, and identifies conversation segments
(consecutive chat-turn runs within the same source file).

The 4th red runner's beat in design terms — currently runs as a
one-shot post-merge pass (deterministic, idempotent). Later wiring
into the live_runner allows continuous re-classification + visual
signal when types change.

Content types (per paragraph):
  - section_header   numbered+divider section landmark
  - code_block       quoted-prefix lines (`>`/`>>`), uniform-indent,
                     or high code-character density
  - chat_turn        short + chat-marker prefix or mid-sentence start
  - sequence_step    starts with "1.", "Step N", numbered procedure
  - dictionary_entry WHAT/WHY/HOW/etc header with divider line
  - formal_doctrine  long + governance vocabulary density
  - prose            default — well-formed paragraph

File types (per file anchor, aggregated from member paragraphs):
  - chat_file          >40% chat_turn paragraphs
  - code_paste_file    >30% code_block paragraphs
  - governance_doc     >5% section_header or >30% formal_doctrine
  - definition_archive >30% dictionary_entry
  - procedure_doc      >30% sequence_step
  - mixed              fallback when no dominant type emerges

Conversation segments: runs of 3+ consecutive chat_turn paragraphs
in the same source_file get a shared `conversation_segment_id`.
Renderer can group these visually while preserving turn-level
addressability.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any


_DIVIDER_LINE_RE = re.compile(r"^[=\-_]{3,}\s*$", re.MULTILINE)
_SECTION_PATTERN_RE = re.compile(r"^\s*\d+[A-Z]?\.\s+[A-Z]", re.MULTILINE)
_QUOTE_LINE_RE = re.compile(r"^\s*>")
_CODE_CHARS = set("${}();[]=<>")
_CHAT_MARKERS = (
    "you said", "i said", "gpt:", "claude:", "grok:", "chatgpt:",
    "system:", "user:", "assistant:", "human:",
)
_SEQUENCE_PREFIX_RE = re.compile(
    r"^\s*(?:\d+[\.\)]|\bSTEP\s+\d+|\bStep\s+\d+|\bPhase\s+\d+)", re.IGNORECASE
)
_DICTIONARY_HEADER_RE = re.compile(
    r"^\s*(WHAT|WHY|WHEN|HOW|WHERE|RULE|LAW|ENGINE|PURPOSE|END|BEGIN)\s*\n[-=]{2,}",
    re.MULTILINE,
)
_DOCTRINE_TERMS = (
    "ΔA", "authority", "hardpoint", "OSIRIS", "POST", "BUILD",
    "sovereign", "triplicate", "triangulation", "doctrine", "axiom",
    "constraint field", "amplification", "sovereignty",
)

# Emoji / unicode markers signaling "this file is a chat capture" —
# operators often use these to mark approved/rejected steps inline.
_EMOJI_MARKERS = ("✅", "❌", "✨", "\U0001F916", "\U0001F914", "\U0001F4CC")
_CHAT_HEADER_PHRASES = (
    "you said", "i said", "gpt:", "claude:", "grok:", "chatgpt:",
    "system:", "user:", "assistant:", "human:",
)


def classify_paragraph(content: str) -> str:
    """Return a single content_type label for the given text body."""
    if not content or not content.strip():
        return "empty"
    lines = [l for l in content.split("\n") if l.strip()]
    if not lines:
        return "empty"
    alnum_count = sum(1 for c in content if c.isalnum())

    # Section header — numbered pattern + divider line co-present
    if _SECTION_PATTERN_RE.search(content) and _DIVIDER_LINE_RE.search(content):
        return "section_header"

    # Code block — quote prefix OR uniform indent OR code-char density
    quote_line_count = sum(1 for l in lines if _QUOTE_LINE_RE.match(l))
    if quote_line_count / len(lines) > 0.7:
        return "code_block"
    indent_lines = sum(
        1 for l in lines if (len(l) - len(l.lstrip())) >= 4
    )
    if indent_lines / len(lines) > 0.7 and len(lines) >= 3:
        return "code_block"
    code_char_count = sum(1 for c in content if c in _CODE_CHARS)
    if alnum_count > 50 and code_char_count > alnum_count * 0.12:
        return "code_block"

    # Sequence step — numbered procedure marker at start
    if _SEQUENCE_PREFIX_RE.match(content):
        return "sequence_step"

    # Dictionary entry — short header with ruler line
    if _DICTIONARY_HEADER_RE.search(content):
        return "dictionary_entry"

    # Chat turn — short + chat marker prefix or lowercase first letter
    if alnum_count < 200:
        lower_content = content.lower()
        if any(lower_content.startswith(m) for m in _CHAT_MARKERS):
            return "chat_turn"
        # First alphabetic char check
        first_alpha = next((c for c in content if c.isalpha()), "")
        if first_alpha and first_alpha.islower():
            return "chat_turn"

    # Formal doctrine — long + governance vocabulary density
    if alnum_count > 300:
        doctrine_hits = sum(1 for t in _DOCTRINE_TERMS if t in content)
        if doctrine_hits >= 2:
            return "formal_doctrine"

    return "prose"


def classify_file(paragraph_types: list[str]) -> str:
    """Aggregate per-paragraph types into a single file_type label."""
    if not paragraph_types:
        return "unknown"
    counter = Counter(paragraph_types)
    total = sum(counter.values())
    if not total:
        return "unknown"

    def pct(k: str) -> float:
        return counter.get(k, 0) / total

    if pct("chat_turn") > 0.40:
        return "chat_file"
    if pct("code_block") > 0.30:
        return "code_paste_file"
    if pct("section_header") > 0.05 or pct("formal_doctrine") > 0.30:
        return "governance_doc"
    if pct("dictionary_entry") > 0.30:
        return "definition_archive"
    if pct("sequence_step") > 0.30:
        return "procedure_doc"
    return "mixed"


def _build_file_context(
    paragraphs: list[dict[str, Any]],
    current_classifications: dict[str, str],
) -> dict[str, Any]:
    """Aggregate file-level signals that bias paragraph-level
    classification on subsequent recursion passes."""
    total = len(paragraphs)
    if not total:
        return {"chat_leaning": False, "chat_pct": 0.0,
                "has_markers": False, "has_emoji_markers": False}
    chat_count = sum(
        1 for p in paragraphs
        if current_classifications.get(p["id"]) == "chat_turn"
    )
    has_markers = False
    has_emoji_markers = False
    for p in paragraphs:
        content = (p.get("meta") or {}).get("content", "") or ""
        lc = content.lower()
        if not has_markers and any(m in lc for m in _CHAT_HEADER_PHRASES):
            has_markers = True
        if not has_emoji_markers and any(m in content for m in _EMOJI_MARKERS):
            has_emoji_markers = True
        if has_markers and has_emoji_markers:
            break
    chat_pct = chat_count / total
    chat_leaning = chat_pct > 0.05 or has_markers or has_emoji_markers
    return {
        "chat_leaning": chat_leaning,
        "chat_pct": chat_pct,
        "has_markers": has_markers,
        "has_emoji_markers": has_emoji_markers,
    }


def classify_paragraph_with_context(content: str, file_ctx: dict[str, Any]) -> str:
    """Pass-2 classifier: respects specific type assignments from pass-1
    (code_block / sequence_step / dictionary_entry / section_header /
    formal_doctrine), only upgrades 'prose' to 'chat_turn' when the
    enclosing file is chat-leaning AND the paragraph is medium-length."""
    base = classify_paragraph(content)
    if base != "prose":
        return base
    if not file_ctx.get("chat_leaning"):
        return "prose"
    alnum = sum(1 for c in content if c.isalnum())
    if 40 <= alnum < 400:
        return "chat_turn"
    return "prose"


def classify_paragraph_with_neighbor_bias(
    paragraph: dict[str, Any],
    sorted_paragraphs: list[dict[str, Any]],
    idx_in_sorted: int,
    current_classifications: dict[str, str],
) -> str:
    """Pass-3 classifier: a 'prose' paragraph wedged between chat_turn
    neighbors in the same source_file is almost certainly mid-conversation.
    Long paragraphs (>=600 alnum) stay prose regardless of neighbors."""
    current = current_classifications.get(paragraph["id"], "prose")
    if current != "prose":
        return current
    content = (paragraph.get("meta") or {}).get("content", "") or ""
    alnum = sum(1 for c in content if c.isalnum())
    if alnum >= 600:
        return current
    sf = (paragraph.get("meta") or {}).get("source_file")
    neighbors_chat = 0
    for offset in (-1, +1):
        ni = idx_in_sorted + offset
        if 0 <= ni < len(sorted_paragraphs):
            neighbor = sorted_paragraphs[ni]
            if (neighbor.get("meta") or {}).get("source_file") != sf:
                continue
            if current_classifications.get(neighbor["id"]) == "chat_turn":
                neighbors_chat += 1
    if neighbors_chat >= 1:
        return "chat_turn"
    return current


def identify_conversation_segments(
    paragraph_nodes: list[dict[str, Any]],
    min_segment_size: int = 3,
) -> dict[str, str]:
    """Find runs of 3+ consecutive chat_turn paragraphs within the same
    source_file. Returns a dict mapping node_id -> segment_id. Segment
    ids are stable identifiers of form '<source_file>::seg<N>'."""
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in paragraph_nodes:
        meta = n.get("meta") or {}
        sf = meta.get("source_file")
        if sf:
            by_source[sf].append(n)

    result: dict[str, str] = {}
    for source_file, paras in by_source.items():
        # Sort by paragraph_index to walk in source order
        paras_sorted = sorted(
            paras, key=lambda n: (n.get("meta") or {}).get("paragraph_index", 0)
        )
        segment_counter = 0
        current_run: list[str] = []
        for n in paras_sorted:
            ctype = (n.get("meta") or {}).get("content_type")
            if ctype == "chat_turn":
                current_run.append(n["id"])
            else:
                if len(current_run) >= min_segment_size:
                    segment_counter += 1
                    seg_id = f"{source_file}::seg{segment_counter}"
                    for nid in current_run:
                        result[nid] = seg_id
                current_run = []
        # Trailing run
        if len(current_run) >= min_segment_size:
            segment_counter += 1
            seg_id = f"{source_file}::seg{segment_counter}"
            for nid in current_run:
                result[nid] = seg_id

    return result


def enrich_lattice(
    nodes: list[dict[str, Any]],
    max_passes: int = 3,
) -> dict[str, Any]:
    """Recursive Stage 2 enrichment: mutate node metas in place.

    Three-pass design with convergence:
      - Pass 1: baseline per-paragraph classification (no context)
      - Pass 2: file-context bias — promote 'prose' to 'chat_turn' in
        chat-leaning files (detected via pass-1 chat_pct + chat markers
        + emoji markers)
      - Pass 3: neighbor bias — 'prose' wedged between 'chat_turn' on
        both sides re-classifies as 'chat_turn'

    Each pass uses ONLY information from prior passes — no external
    training data. The "learning" is heuristic ratcheting: per-paragraph
    signal → file-level inference → neighbor-context refinement.
    Convergence (changes-per-pass dropping) indicates saturation.

    After classification converges:
      - file nodes get `meta.file_type` aggregated from final paragraph
        classifications
      - chat_turn paragraphs in 3+ consecutive runs get
        `meta.conversation_segment_id`
      - each paragraph gets `meta.content_type` (final pass result)

    Returns stats dict for caller logging (per-pass counts, file_type
    distribution, segment count).

    Idempotent — re-running re-classifies based on current content +
    re-runs the same convergence process.
    """
    text_bearing_kinds = {"paragraph", "section_anchor", "finding", "observation"}
    text_nodes: list[dict[str, Any]] = []
    for n in nodes:
        meta = n.get("meta") or {}
        if meta.get("kind") in text_bearing_kinds and meta.get("content"):
            text_nodes.append(n)

    # Track classification state across passes
    classifications: dict[str, str] = {}
    pass_stats: list[dict[str, Any]] = []

    # ---------- Pass 1: baseline ----------
    for n in text_nodes:
        content = (n.get("meta") or {}).get("content") or ""
        classifications[n["id"]] = classify_paragraph(content)
    pass_stats.append({
        "pass": 1,
        "changes_from_prev": 0,
        "counts": dict(Counter(classifications.values())),
    })

    if max_passes >= 2:
        # ---------- Pass 2: file-context bias ----------
        # Only meaningful for paragraph + section_anchor kinds that share
        # a source_file. findings/observations don't have files; skip.
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for n in text_nodes:
            meta = n.get("meta") or {}
            sf = meta.get("source_file")
            if sf:
                by_source[sf].append(n)

        prev_classifications = dict(classifications)
        for sf, source_paras in by_source.items():
            ctx = _build_file_context(source_paras, prev_classifications)
            for p in source_paras:
                content = (p.get("meta") or {}).get("content") or ""
                classifications[p["id"]] = classify_paragraph_with_context(content, ctx)
        changes_2 = sum(
            1 for nid in prev_classifications
            if classifications.get(nid) != prev_classifications[nid]
        )
        pass_stats.append({
            "pass": 2,
            "changes_from_prev": changes_2,
            "counts": dict(Counter(classifications.values())),
        })

        if max_passes >= 3 and changes_2 > 0:
            # ---------- Pass 3: neighbor bias ----------
            prev_classifications = dict(classifications)
            for sf, source_paras in by_source.items():
                sorted_paras = sorted(
                    source_paras,
                    key=lambda p: (p.get("meta") or {}).get("paragraph_index", 0),
                )
                for i, p in enumerate(sorted_paras):
                    classifications[p["id"]] = classify_paragraph_with_neighbor_bias(
                        p, sorted_paras, i, prev_classifications
                    )
            changes_3 = sum(
                1 for nid in prev_classifications
                if classifications.get(nid) != prev_classifications[nid]
            )
            pass_stats.append({
                "pass": 3,
                "changes_from_prev": changes_3,
                "counts": dict(Counter(classifications.values())),
            })

    # Apply final classifications to node meta
    for n in text_nodes:
        (n.get("meta") or {})["content_type"] = classifications[n["id"]]

    # ---------- File-type aggregation from final classifications ----------
    by_source_types: dict[str, list[str]] = defaultdict(list)
    paragraph_class_nodes: list[dict[str, Any]] = []
    for n in text_nodes:
        meta = n.get("meta") or {}
        if meta.get("kind") in ("paragraph", "section_anchor"):
            paragraph_class_nodes.append(n)
            sf = meta.get("source_file")
            ctype = meta.get("content_type")
            if sf and ctype:
                by_source_types[sf].append(ctype)

    file_type_by_source: dict[str, str] = {}
    for sf, types in by_source_types.items():
        file_type_by_source[sf] = classify_file(types)

    file_type_counts: Counter[str] = Counter()
    for n in nodes:
        meta = n.get("meta") or {}
        if meta.get("kind") != "file":
            continue
        path = meta.get("path", "")
        ftype = file_type_by_source.get(path)
        if ftype:
            meta["file_type"] = ftype
            file_type_counts[ftype] += 1

    # ---------- Conversation segments from final classifications ----------
    seg_map = identify_conversation_segments(paragraph_class_nodes)
    for n in paragraph_class_nodes:
        seg_id = seg_map.get(n["id"])
        if seg_id:
            (n.get("meta") or {})["conversation_segment_id"] = seg_id

    final_counts = dict(Counter(classifications.values()))
    return {
        "passes": pass_stats,
        "final_paragraph_counts": final_counts,
        "file_type_counts": dict(file_type_counts),
        "distinct_conversation_segments": len(set(seg_map.values())),
        "chat_turns_in_segments": sum(1 for _ in seg_map),
        "converged": (
            len(pass_stats) >= 2
            and pass_stats[-1]["changes_from_prev"]
                < max(pass_stats[-2]["changes_from_prev"] // 2, 10)
        ),
    }


_SEG_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def emit_conversation_segment_anchors(
    nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Promote each distinct `conversation_segment_id` (set by the
    classifier on chat_turn paragraphs) to a first-class node.

    For each segment:
      - Emit one `conversation_segment` anchor node (kind="conversation_segment")
      - Emit `contains` edges from anchor → each member chat_turn
      - Emit a `contains` edge from the parent file_anchor → this segment

    Existing file_anchor → chat_turn `contains` edges are preserved
    (parallel containment — file is the structural container; segment
    is the semantic grouping). Visual layer chooses which to show at
    which zoom tier.

    Returns (new_nodes, new_edges). Caller appends them to the merge
    payload after enrich_lattice has classified + segmented.
    """
    # Group chat_turn paragraphs by their conversation_segment_id
    by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        meta = n.get("meta") or {}
        seg_id = meta.get("conversation_segment_id")
        if seg_id:
            by_segment[seg_id].append(n)

    # File-anchor lookup by path
    file_anchor_by_path: dict[str, dict[str, Any]] = {}
    for n in nodes:
        meta = n.get("meta") or {}
        if meta.get("kind") == "file":
            path = meta.get("path", "")
            if path:
                file_anchor_by_path[path] = n

    new_nodes: list[dict[str, Any]] = []
    new_edges: list[dict[str, Any]] = []

    for seg_id, members in by_segment.items():
        if not members:
            continue
        members_sorted = sorted(
            members,
            key=lambda n: (n.get("meta") or {}).get("paragraph_index", 0),
        )
        first = members_sorted[0]
        last = members_sorted[-1]
        first_meta = first.get("meta") or {}
        source_file = first_meta.get("source_file", "") or ""
        cluster = first.get("cluster", "uncategorized")

        # Preview content: first turn's full content + first lines of next turns
        preview_chunks = []
        for m in members_sorted[: min(5, len(members_sorted))]:
            content = (m.get("meta") or {}).get("content", "") or ""
            preview_chunks.append(content.strip())
        combined_preview = "\n\n".join(preview_chunks)
        if len(members_sorted) > 5:
            combined_preview += f"\n\n[... {len(members_sorted) - 5} more turns]"

        # Short label = first turn's content one-liner
        first_content = first_meta.get("content", "") or ""
        first_line = " ".join(first_content.split())
        if len(first_line) > 70:
            first_line = first_line[:69] + "…"
        seg_label_suffix = seg_id.split("::")[-1] if "::" in seg_id else seg_id

        # Stable id from segment_id (already file-path-prefixed + numbered)
        safe_id = _SEG_ID_SAFE_RE.sub("_", seg_id.replace("::", "."))
        anchor_id = f"fs.segment.{safe_id}"

        # File-stem tag groups segments visually with their parent file
        file_stem = source_file.rsplit("/", 1)[-1] if "/" in source_file else source_file
        if file_stem.endswith(".txt") or file_stem.endswith(".md"):
            file_stem = file_stem.rsplit(".", 1)[0]

        # Weight scales gently with member count
        weight = min(2.4, 1.0 + (len(members_sorted) ** 0.4) * 0.15)

        anchor_node = {
            "id": anchor_id,
            "label": f"{seg_label_suffix} — {first_line}" if first_line else f"Conversation segment {seg_label_suffix}",
            "cluster": cluster,
            "tags": ["filesystem", "conversation_segment", file_stem],
            "weight": round(weight, 2),
            "meta": {
                "kind": "conversation_segment",
                "segment_id": seg_id,
                "source_file": source_file,
                "member_count": len(members_sorted),
                "start_paragraph_index": first_meta.get("paragraph_index"),
                "end_paragraph_index": (last.get("meta") or {}).get("paragraph_index"),
                "content": combined_preview,  # First 5 turns + count of remainder; node page renders as readable preview
            },
        }
        new_nodes.append(anchor_node)

        # Containment: segment_anchor -> each member chat_turn
        for m in members_sorted:
            new_edges.append({
                "source": anchor_id,
                "target": m["id"],
                "kind": "contains",
                "weight": 1.0,
            })

        # Containment: file_anchor -> this segment_anchor (so file pages
        # surface segments as direct children alongside loose paragraphs)
        file_anchor = file_anchor_by_path.get(source_file)
        if file_anchor:
            new_edges.append({
                "source": file_anchor["id"],
                "target": anchor_id,
                "kind": "contains",
                "weight": 1.0,
            })

    return new_nodes, new_edges
