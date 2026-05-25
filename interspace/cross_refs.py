"""Cross-reference extraction (Pi macro-pointer substrate).

Shared by any adapter that emits text-bearing nodes. Scans `meta.content` on
each node and emits typed edges (cites_file, cites_section, full_doctrine_of,
temporal_variant_of) with quoted-context snippets carried on each edge.

Two-tier discovery model (Pi v5 Stage 1 / Stage 2):
  - This module = Stage 1, deterministic regex pass at adapter time. Catches
    literal mentions: filenames, section refs, full-doctrine pointers,
    temporal variants of sibling files.
  - Re-walk routine (external) = Stage 2, T-cell-style semantic pass that
    accretes over time. Emits patches consumed by the merger's --patches flag.

Both write the same edge schema (kind / weight / meta.quote shape) so the
lattice can grow monotonically without translation.

Public entry points:
  - extract_cross_references(nodes) -> list[edge]
  - extract_quote(content, start, end, radius=60) -> str
  - SECTION_ANCHOR_RE, FILE_MENTION_RE, SECTION_REF_RE, FULL_DOCTRINE_RE,
    SECTION_ID_RE (also useful when an adapter wants to classify paragraphs
    against the same vocabulary used here for resolution).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


# Section anchor: any line in the paragraph matching "6C. IRON LAW" or
# "1. AUTHORITY". MULTILINE so it fires on the section-title line even when
# `===` decorator lines are bundled into the same paragraph.
SECTION_ANCHOR_RE = re.compile(r"^\s*\d+[A-Z]?\.\s+[A-Z]", re.MULTILINE)

# R1: file mention. Matches Iron_Law.txt, BUILD.md, package.json, etc.
# Capitalized stem avoids matching all-lowercase fragments like "config.txt"
# in prose. Word-boundary anchored.
FILE_MENTION_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]+\.(?:txt|md|json|js|ps1|py|html|css))\b"
)

# R3: section reference like "§6B", "§ 7", "Section 4", "section 6A".
SECTION_REF_RE = re.compile(r"(?:§\s*|[Ss]ection\s+)(\d+[A-Z]?)\b")

# R4: "Full doctrine:" pointer (strong-weight cross-doc link to canonical
# source). Captures the referenced path.
FULL_DOCTRINE_RE = re.compile(
    r"Full\s+doctrine:\s*([\w\\\/.+-]+\.(?:txt|md|json))",
    re.IGNORECASE,
)

# Section id extractor — from a section_anchor paragraph's matched line, pull
# just the leading number/letter (e.g. "6C. IRON LAW" -> "6C").
SECTION_ID_RE = re.compile(r"^\s*(\d+[A-Z]?)\.")

# R7: meta-field references. Some adapters carry explicit file pointers in
# named meta fields (e.g. graphic_mem.findings.source_report is the re-walk
# report .md that produced the finding). When that string matches a known
# file anchor's basename, emit a typed edge with provenance via the field name.
#
# `meta.source_file` is deliberately NOT included — it duplicates the
# `contains` edges already emitted from file anchors to their paragraphs,
# and the file-anchor page renders the source document inline already.
META_REF_KINDS = {
    "source_report": ("derived_from_report", 0.95),
    "filename":      ("cites_file",         0.70),
    "path":          ("cites_file",         0.70),
}


def try_repair_mojibake(text: str) -> str:
    """Repair the classic UTF-8-read-as-cp1252-then-re-saved-as-UTF-8
    double-encoding (Windows Notepad's signature corruption pattern).
    Example: box-drawing chars '├ ─ │ └' that show as 'â"œ â"€ â", â""'.

    Safe-only-when-confident: pre-checks for characteristic mojibake markers
    (sequences like 'â€' or 'â"' or 'Ã*') and only attempts repair when they
    appear. Falls through unchanged if the round-trip would raise (file is
    clean UTF-8 that can't be re-encoded as cp1252)."""
    if not any(m in text for m in ("â€", "â”", "Ã")):
        return text
    try:
        return text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def extract_quote(content: str, start: int, end: int, radius: int = 60) -> str:
    """Pull a short snippet around a regex match for use as edge.meta.quote.
    Collapses whitespace, prefixes/suffixes ellipses when truncated. Pi shell
    'diluted context' — enough to evaluate relevance without navigating."""
    lo = max(0, start - radius)
    hi = min(len(content), end + radius)
    snippet = re.sub(r"\s+", " ", content[lo:hi].strip())
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(content) else ""
    return prefix + snippet + suffix


def extract_cross_references(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Second-pass cross-reference extraction. Reads each node's `meta.content`
    and fires R1 (cites_file), R3 (cites_section), R4 (full_doctrine_of),
    R6 (temporal_variant_of), emitting typed edges with quoted-context snippets.

    Resolution uses indexes built from the emitted node list:
      - file anchors by basename of `meta.path`
      - section anchors by `meta.source_file` + section_id

    Any node with `meta.content` is scanned (paragraph, section_anchor,
    finding, sticky, etc.) — works across adapters that emit different node
    kinds, as long as the text payload lives in `meta.content`.

    Self-loops are skipped. Duplicate (kind, target) emissions within the
    same source node are deduplicated."""
    file_anchor_by_basename: dict[str, str] = {}
    section_anchors_by_file: dict[str, dict[str, str]] = defaultdict(dict)
    variant_groups: dict[tuple[str, str], list[str]] = defaultdict(list)

    for n in nodes:
        meta = n.get("meta") or {}
        kind = meta.get("kind")
        if kind == "file":
            path = meta.get("path", "")
            basename = path.rsplit("/", 1)[-1]
            file_anchor_by_basename[basename] = n["id"]
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            base_stem = basename.split(".", 1)[0]
            variant_groups[(folder, base_stem)].append(n["id"])
        elif kind == "section_anchor":
            sf = meta.get("source_file")
            content = meta.get("content", "")
            m = SECTION_ANCHOR_RE.search(content)
            if m and sf:
                nl = content.find("\n", m.start())
                line = content[m.start():nl if nl >= 0 else len(content)]
                sm = SECTION_ID_RE.match(line)
                if sm:
                    section_anchors_by_file[sf][sm.group(1)] = n["id"]

    cross_edges: list[dict[str, Any]] = []

    for n in nodes:
        meta = n.get("meta") or {}
        content = meta.get("content", "") or ""
        if not content:
            continue
        source_file = meta.get("source_file", "") or ""
        seen_targets: set[tuple[str, str]] = set()

        # R4 first: full doctrine pointer (strong, takes precedence over R1
        # for the same target).
        for m in FULL_DOCTRINE_RE.finditer(content):
            target_path = m.group(1).strip()
            basename = target_path.replace("\\", "/").rsplit("/", 1)[-1]
            target = file_anchor_by_basename.get(basename)
            if not target or target == n["id"]:
                continue
            key = ("full_doctrine_of", target)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            cross_edges.append({
                "source": n["id"],
                "target": target,
                "kind": "full_doctrine_of",
                "weight": 1.0,
                "meta": {"quote": extract_quote(content, m.start(), m.end())},
            })

        # R1: file mentions (skip ones already covered by R4 for the same target).
        for m in FILE_MENTION_RE.finditer(content):
            basename = m.group(1)
            target = file_anchor_by_basename.get(basename)
            if not target or target == n["id"]:
                continue
            if ("full_doctrine_of", target) in seen_targets:
                continue
            key = ("cites_file", target)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            cross_edges.append({
                "source": n["id"],
                "target": target,
                "kind": "cites_file",
                "weight": 0.6,
                "meta": {"quote": extract_quote(content, m.start(), m.end())},
            })

        # R3: section refs (intra-doc — resolve against same source_file's
        # section index). Only fires when source_file is known.
        if source_file:
            sections = section_anchors_by_file.get(source_file, {})
            for m in SECTION_REF_RE.finditer(content):
                sec_id = m.group(1)
                target = sections.get(sec_id)
                if not target or target == n["id"]:
                    continue
                key = ("cites_section", target)
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                cross_edges.append({
                    "source": n["id"],
                    "target": target,
                    "kind": "cites_section",
                    "weight": 0.8,
                    "meta": {"quote": extract_quote(content, m.start(), m.end())},
                })

    # R6: temporal variants — bidirectional clique within each
    # (folder, base_stem) group.
    for group in variant_groups.values():
        if len(group) < 2:
            continue
        for src in group:
            for tgt in group:
                if src == tgt:
                    continue
                cross_edges.append({
                    "source": src,
                    "target": tgt,
                    "kind": "temporal_variant_of",
                    "weight": 0.9,
                })

    # R7: meta-field references — explicit file pointers in named meta fields
    # (e.g. source_report on graphic_mem findings). Resolves the field's
    # string value to a file anchor by basename. Per-node dedup so each
    # (source, target, kind) only fires once.
    for n in nodes:
        meta = n.get("meta") or {}
        seen_meta_targets: set[tuple[str, str]] = set()
        for field, (edge_kind, weight) in META_REF_KINDS.items():
            v = meta.get(field)
            if not isinstance(v, str) or not v:
                continue
            basename = v.replace("\\", "/").rsplit("/", 1)[-1]
            target = file_anchor_by_basename.get(basename)
            if not target or target == n["id"]:
                continue
            key = (edge_kind, target)
            if key in seen_meta_targets:
                continue
            seen_meta_targets.add(key)
            cross_edges.append({
                "source": n["id"],
                "target": target,
                "kind": edge_kind,
                "weight": weight,
                "meta": {"via": f"meta.{field}", "quote": v},
            })

    return cross_edges
