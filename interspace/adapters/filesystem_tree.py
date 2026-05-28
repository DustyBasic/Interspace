"""Adapter: filesystem tree -> Interspace JSON.

Walks a directory tree and emits one node per file, with **density-aware
aggregation**: files matching the same naming pattern (timestamped series,
versioned series, CURRENT-prefixed snapshots, numbered series) within a
folder collapse into a single composite node when their count meets the
density threshold. Files that don't match a pattern, or whose pattern group
is below threshold, stay as individual nodes.

This avoids drowning the lattice in serial / versioned files (logs,
snapshots, ledger appends) while preserving every unique standalone artifact.

CLI:
    python -m interspace.adapters.filesystem_tree <root> -o <output.json> \\
        [--density-threshold N] [--max-depth N] [--cluster-depth N] \\
        [--exclude <glob>] ... [--title "..."] [--description "..."]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cross_refs import (
    SECTION_ANCHOR_RE,
    extract_cross_references,
    fnv1a_128_hex,
    try_repair_mojibake,
)


_DEFAULT_EXCLUDES = (".*", "*.pyc", "__pycache__", "node_modules", "*.bak")
_DEFAULT_DENSITY = 3
_DEFAULT_CLUSTER_DEPTH = 1

# Text-bearing extensions whose content gets paragraph-exploded (when the file
# is below `--explode-size` and not part of a density-collapsed group).
_TEXT_EXTS = frozenset({".txt", ".md", ".markdown", ".rst", ".org"})
_DEFAULT_EXPLODE_SIZE = 500_000  # text files up to this get paragraph-extracted
_MIN_PARAGRAPH_CHARS = 40        # paragraphs below this (alnum count) get dropped as noise (ruler lines, micro-fragments)
_LABEL_MAX = 80
# Atomic-file content embedding: read body for non-binary files up to this size.
# Caps at 500KB to keep the .html page reasonable while covering most preserved
# artifacts. Files above stay anchor-only — true outliers that need their own
# treatment (chunked viewer / external link).
_ATOMIC_CONTENT_MAX_BYTES = 500_000

_TIMESTAMP_RE = re.compile(r"^(.+?)_\d{4,8}[_-]?\d{0,6}[A-Z]?$")
_VERSION_RE = re.compile(r"^(.+?)[._-]v\d+(\.\d+)*$")
_NUMBERED_RE = re.compile(r"^(\d{2,4})[._-](.+)$")
_CURRENT_RE = re.compile(r"^CURRENT[_-](.+)$", re.IGNORECASE)
_CHECKSUM_RE = re.compile(r"^(CHECKSUMS?_[A-Z0-9]+)_.*$")

# A divider line of repeated `=`, `-`, or `_` (3+). Co-occurs with the
# numbered section pattern in older-style title blocks — distinguishes them
# from inline numbered list items.
_DIVIDER_LINE_RE = re.compile(r"^[=\-_]{3,}\s*$", re.MULTILINE)
# Markdown ATX headers (1-6 hashes followed by a space + non-whitespace)
_MARKDOWN_HEADER_RE = re.compile(r"^\s*#{1,6}\s+\S")
# Verbose section prefixes — "Part 3", "Chapter 6", "Section 1.2", "Phase 4"
_VERBOSE_HEADER_RE = re.compile(
    r"^\s*(Part|Section|Chapter|Phase|Step|Topic|Appendix)\s+\d+\b",
    re.IGNORECASE,
)


def _is_section_anchor(para: str) -> bool:
    """Section-anchor detection across the common header conventions found in
    the archive (markdown, governance docs, GPT-output prose, classic
    decorated titles). A paragraph qualifies if its first non-empty line
    matches one of:
      - Markdown ATX header: "# Title", "## 6A. Title", "### Subsection"
      - Verbose prefix: "Part 3:", "Chapter 6", "Phase 1 — Intake"
      - Numbered + divider: "6C. IRON LAW (...)" co-occurring with ===/---

    Headers always survive paragraph-noise filtering downstream so they
    persist as structural landmarks for the renderer's section pass.
    """
    if not para:
        return False
    first_line = para.lstrip().split("\n", 1)[0]
    if len(first_line) > 200:
        return False
    if _MARKDOWN_HEADER_RE.match(first_line):
        return True
    if _VERBOSE_HEADER_RE.match(first_line):
        return True
    if SECTION_ANCHOR_RE.search(first_line) and _DIVIDER_LINE_RE.search(para):
        return True
    return False

_CLUSTER_PALETTE = [
    "#4f7cac", "#c97b63", "#7aa974", "#9b7aa9",
    "#d4a55a", "#5a9aa8", "#b56576", "#6d6875",
    "#8a9a5b", "#a26769", "#4a6c6f", "#7d5a50",
]

# Node ids become filenames (`<id>.html`) — restrict to chars safe on every OS.
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_id_part(s: str) -> str:
    s = _ID_SAFE_RE.sub("_", s.strip("_"))
    return s or "_"


def to_interspace_json(
    root: Path,
    density_threshold: int = _DEFAULT_DENSITY,
    max_depth: int | None = None,
    cluster_depth: int = _DEFAULT_CLUSTER_DEPTH,
    excludes: tuple[str, ...] = _DEFAULT_EXCLUDES,
    title: str | None = None,
    description: str | None = None,
    explode_text: bool = True,
    explode_size: int = _DEFAULT_EXPLODE_SIZE,
) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"root directory not found: {root}")

    files = _walk(root, max_depth=max_depth, excludes=excludes)

    grouped_per_folder = _group_by_folder_and_pattern(files, root)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    cluster_ids: list[str] = []
    cluster_palette_idx = 0
    cluster_map: dict[str, int] = {}

    for (folder_rel, pattern_key), group_files in grouped_per_folder.items():
        cluster_id = _cluster_id_for(folder_rel, cluster_depth)
        if cluster_id not in cluster_map:
            cluster_map[cluster_id] = cluster_palette_idx
            cluster_palette_idx += 1
            cluster_ids.append(cluster_id)

        cluster_id_safe = _safe_id_part(cluster_id)
        if len(group_files) >= density_threshold and pattern_key.startswith(("ts:", "v:", "num:", "current:", "checksum:")):
            nodes.append(_composite_node(group_files, folder_rel, pattern_key, cluster_id_safe))
        else:
            for f in group_files:
                ext = _file_ext(f.name)
                size = f.stat().st_size
                if (
                    explode_text
                    and ext in _TEXT_EXTS
                    and size <= explode_size
                ):
                    paragraph_nodes = _paragraph_nodes(f, folder_rel, cluster_id_safe)
                    if paragraph_nodes:
                        anchor = _doc_anchor_from_paragraphs(
                            f, folder_rel, cluster_id_safe, len(paragraph_nodes)
                        )
                        nodes.append(anchor)
                        nodes.extend(paragraph_nodes)
                        # Containment (Pi shell): anchor -> each paragraph child.
                        for p in paragraph_nodes:
                            edges.append({
                                "source": anchor["id"],
                                "target": p["id"],
                                "kind": "contains",
                                "weight": 1.0,
                            })
                        # Sequence (Pi local pointer N): paragraph[i] -> paragraph[i+1].
                        for i in range(len(paragraph_nodes) - 1):
                            edges.append({
                                "source": paragraph_nodes[i]["id"],
                                "target": paragraph_nodes[i + 1]["id"],
                                "kind": "sequence",
                                "weight": 1.0,
                            })
                        continue
                # Fallback: atomic file node
                nodes.append(_file_node(f, folder_rel, cluster_id_safe))

    # Folder hierarchy (Pi shell tier above file anchors). Emit folder anchor
    # per distinct folder + parent→child + folder→file containment edges.
    # Runs BEFORE cross-ref extraction so folder anchors can be ref targets.
    _emit_folder_hierarchy(nodes, edges, cluster_ids, root.name)

    # Cross-reference extraction (R1, R3, R4, R6) — second pass over nodes.
    # Shared with any other adapter that emits text-bearing nodes.
    edges.extend(extract_cross_references(nodes))

    clusters = [
        {
            "id": _safe_id_part(cid),
            "label": cid,
            "color": _CLUSTER_PALETTE[i % len(_CLUSTER_PALETTE)],
        }
        for i, cid in enumerate(cluster_ids)
    ]

    payload_meta: dict[str, Any] = {
        "title": title or f"{root.name} filesystem",
        "description": description
        or f"Density-aware filesystem walk of {root}: {len(files)} files, {len(nodes)} nodes, {len(clusters)} clusters.",
        "source": "interspace.adapters.filesystem_tree@0.1",
    }

    return {
        "meta": payload_meta,
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
    }


def _walk(
    root: Path, *, max_depth: int | None, excludes: tuple[str, ...]
) -> list[Path]:
    out: list[Path] = []
    root_len = len(root.parts)
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        depth = len(rel.parts) - 1
        if max_depth is not None and depth > max_depth:
            continue
        if _is_excluded(rel, excludes):
            continue
        out.append(p)
    return out


def _is_excluded(rel: Path, excludes: tuple[str, ...]) -> bool:
    for part in rel.parts:
        for pattern in excludes:
            if fnmatch.fnmatch(part, pattern):
                return True
    name = rel.name
    for pattern in excludes:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def _cluster_id_for(folder_rel: str, cluster_depth: int) -> str:
    if not folder_rel:
        return "<root>"
    parts = Path(folder_rel).parts
    return "/".join(parts[:cluster_depth]) or "<root>"


def _group_by_folder_and_pattern(
    files: list[Path], root: Path
) -> dict[tuple[str, str], list[Path]]:
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for f in files:
        folder_rel = str(f.parent.relative_to(root)).replace("\\", "/")
        if folder_rel == ".":
            folder_rel = ""
        key = _pattern_key(f.name)
        groups[(folder_rel, key)].append(f)
    return groups


def _pattern_key(filename: str) -> str:
    """Return a grouping key for this filename.

    Composite-eligible patterns are prefixed `ts:` / `v:` / `num:` /
    `current:` / `checksum:`. Files without a detected pattern get a unique
    `u:<basename>` key (one per file) so they always stay individual.
    """
    stem, _, ext = filename.rpartition(".")
    if not stem:
        stem = filename
        ext = ""
    ext = ("." + ext.lower()) if ext else ""

    if _CHECKSUM_RE.match(stem):
        return f"checksum:{_CHECKSUM_RE.match(stem).group(1)}{ext}"
    if _CURRENT_RE.match(stem):
        return f"current:{ext}"
    m = _TIMESTAMP_RE.match(stem)
    if m:
        return f"ts:{m.group(1)}{ext}"
    m = _VERSION_RE.match(stem)
    if m:
        return f"v:{m.group(1)}{ext}"
    m = _NUMBERED_RE.match(stem)
    if m:
        return f"num:{m.group(2)}{ext}"
    return f"u:{filename}"


def _composite_node(
    files: list[Path], folder_rel: str, pattern_key: str, cluster_id: str
) -> dict[str, Any]:
    kind_prefix, _, rest = pattern_key.partition(":")
    base_label = rest or pattern_key
    total_bytes = sum(f.stat().st_size for f in files)
    earliest_mtime = min(f.stat().st_mtime for f in files)
    latest_mtime = max(f.stat().st_mtime for f in files)
    sample_names = sorted(f.name for f in files)
    label_prefix = {
        "ts": "Timestamped series",
        "v": "Versioned series",
        "num": "Numbered series",
        "current": "CURRENT_ snapshots",
        "checksum": "Checksum series",
    }.get(kind_prefix, "Series")
    label = f"{label_prefix}: {base_label} ({len(files)} files)"
    node_id = (
        f"fs.composite.{_safe_id_part(folder_rel or 'root')}."
        f"{_safe_id_part(pattern_key)}"
    )
    weight = min(3.0, 1.0 + math.log10(len(files)))
    return {
        "id": node_id,
        "label": label,
        "cluster": cluster_id,
        "tags": ["filesystem", "composite", kind_prefix, _ext_or_mixed(files)],
        "weight": round(weight, 2),
        "meta": {
            "kind": "composite",
            "folder": folder_rel or "/",
            "pattern": pattern_key,
            "file_count": len(files),
            "total_size_bytes": total_bytes,
            "created_at": datetime.fromtimestamp(earliest_mtime, tz=timezone.utc).isoformat(),
            "latest_mtime": datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat(),
            "sample_files": sample_names[:10],
            "extensions": sorted({_file_ext(n) for n in sample_names}),
        },
    }


def _paragraph_nodes(
    f: Path, folder_rel: str, cluster_id: str
) -> list[dict[str, Any]]:
    """Split a text file into paragraph nodes. Returns [] if file is unreadable
    or contains no paragraphs above _MIN_PARAGRAPH_CHARS."""
    try:
        content = try_repair_mojibake(
            f.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return []
    rel_path = f"{folder_rel}/{f.name}" if folder_rel else f.name
    file_stem = f.stem
    file_mtime_iso = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()
    safe_rel = _safe_id_part(rel_path)
    paragraphs = _split_paragraphs(content)
    out: list[dict[str, Any]] = []
    for idx, para in enumerate(paragraphs):
        # Section-head paragraphs (e.g. "6C. IRON LAW (...)") get their own kind
        # so cross-refs of the shape "see §6B" can target them precisely.
        kind = "section_anchor" if _is_section_anchor(para) else "paragraph"
        out.append({
            "id": f"fs.para.{safe_rel}.{idx:03d}",
            "label": _shorten(para, _LABEL_MAX),
            "cluster": cluster_id,
            "tags": ["filesystem", "paragraph", _file_ext(f.name), file_stem],
            "weight": round(min(2.0, 0.6 + math.log10(max(1.0, len(para) / 200.0)) * 0.5), 2),
            "meta": {
                "kind": kind,
                "source_file": rel_path,
                "paragraph_index": idx,
                "paragraphs_in_file": len(paragraphs),
                "content": para,
                "sig128": fnv1a_128_hex(para),
                "char_count": len(para),
                "created_at": file_mtime_iso,
            },
        })
    return out


_QUOTE_PREFIX_RE = re.compile(r"^\s*>")  # any line starting with `>` (incl. bare `>>` separators inside the paste)


def _is_quoted_block(s: str) -> bool:
    """All non-empty lines start with `>>` or `>` (a quoted/pasted block —
    typically GPT/chat responses someone cut-and-pasted into a .txt). These
    pastes often have internal blank lines separating logical sections of
    the same paste, which over-shred under blank-line splitting."""
    lines = [l for l in s.split("\n") if l.strip()]
    if len(lines) < 1:
        return False
    return all(_QUOTE_PREFIX_RE.match(l) for l in lines)


def _split_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs (one node per blank-line block) with
    content-type self-identification for paste-block coalescing.

    Dictionary-grain design: each paragraph is its own addressable atom.
    Dense archives (chat transcripts, documentation trees, governance
    docs) carry singular value per paragraph — a chat turn, a definition,
    an axiom, a sticky — so we preserve that granularity rather than
    coalescing.

    Type-aware coalescing: consecutive `>`/`>>`-quoted paragraphs (GPT
    cut-and-paste responses where internal blank lines separated logical
    sections of one cohesive paste) get merged into one chunk. Prose
    between paste blocks stays standalone. Extensible: future patterns
    (fenced code ```, uniform-indent code) plug in via additional
    type-detection predicates.

    Filters: section anchors always survive (structural landmarks); chunks
    below _MIN_PARAGRAPH_CHARS alnum count get dropped as noise (ruler
    lines of `===` / `---`, single-word labels, blank-ish fragments).
    """
    raw = re.split(r"\n\s*\n+", text)
    out: list[str] = []
    pending_quoted: list[str] = []

    def flush_quoted() -> None:
        if not pending_quoted:
            return
        combined = "\n\n".join(pending_quoted)
        # Apply same noise-filter to the coalesced chunk
        alnum = sum(1 for c in combined if c.isalnum())
        if alnum >= _MIN_PARAGRAPH_CHARS:
            out.append(combined)
        pending_quoted.clear()

    for chunk in raw:
        s = chunk.strip()
        if not s:
            continue
        # Section-header paragraphs (e.g. "===\n6C. IRON LAW (...)\n===")
        # always break paste-coalescing and stand alone.
        if _is_section_anchor(s):
            flush_quoted()
            out.append(s)
            continue
        # Paste-block detection — accumulate consecutive quoted blocks.
        if _is_quoted_block(s):
            pending_quoted.append(s)
            continue
        # Regular prose: flush any pending paste-block first, then handle.
        flush_quoted()
        alnum = sum(1 for c in s if c.isalnum())
        if alnum < _MIN_PARAGRAPH_CHARS:
            continue
        out.append(s)

    flush_quoted()
    return out


def _shorten(text: str, n: int) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= n:
        return one_line
    return one_line[: n - 1].rstrip() + "…"


def _file_node(f: Path, folder_rel: str, cluster_id: str) -> dict[str, Any]:
    rel_path = f"{folder_rel}/{f.name}" if folder_rel else f.name
    size = f.stat().st_size
    ext = _file_ext(f.name)
    weight = min(2.0, 0.6 + math.log10(max(1.0, size / 1024.0)) * 0.4)
    meta: dict[str, Any] = {
        "kind": "file",
        "path": rel_path,
        "size_bytes": size,
        "extension": ext,
        "created_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
    }
    # Embed text body for non-binary atomic files under the size cap. Larger
    # files stay anchor-only; binaries (.pdf/.docx/.zip/...) fall through via
    # UnicodeDecodeError. Mojibake repair runs on read to fix the cp1252
    # round-trip corruption Windows editors bake into UTF-8 files.
    if size <= _ATOMIC_CONTENT_MAX_BYTES:
        try:
            text = f.read_text(encoding="utf-8-sig", errors="strict")
            meta["content"] = try_repair_mojibake(text)
        except (UnicodeDecodeError, OSError):
            pass  # binary or unreadable — anchor only
    return {
        "id": f"fs.file.{_safe_id_part(rel_path)}",
        "label": f.name,
        "cluster": cluster_id,
        "tags": ["filesystem", "file"] + ([ext] if ext else []),
        "weight": round(max(0.6, weight), 2),
        "meta": meta,
    }


def _folder_anchor_node(
    folder_rel: str,
    root_name: str,
    child_file_count: int,
    child_folder_count: int,
    cluster_id: str,
) -> dict[str, Any]:
    """Folder-anchor node (kind='folder'). Sits one tier above file anchors:
    the genre / super-genre Pi shell. Pulls its child file anchors and child
    folder anchors toward itself via 'contains' edges, producing nested
    radial structure in the lattice (Protocols ball, Docs ball, all hanging
    off a Gov_Alignment center)."""
    if folder_rel:
        leaf = folder_rel.rsplit("/", 1)[-1]
        label = f"{leaf}/"
        safe_id = _safe_id_part(folder_rel)
        path_display = folder_rel + "/"
    else:
        leaf = root_name
        label = f"{root_name}/"
        safe_id = "_root"
        path_display = ""
    weight = min(3.0, 1.2 + math.log10(max(1.0, child_file_count + child_folder_count + 1)) * 0.5)
    return {
        "id": f"fs.folder.{safe_id}",
        "label": label,
        "cluster": cluster_id,
        "tags": ["filesystem", "folder", leaf],
        "weight": round(weight, 2),
        "meta": {
            "kind": "folder",
            "path": path_display,
            "child_file_count": child_file_count,
            "child_folder_count": child_folder_count,
        },
    }


def _emit_folder_hierarchy(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    cluster_ids: list[str],
    root_name: str,
) -> int:
    """Post-pass: emit folder-anchor nodes + parent→child folder containment
    + folder→file containment. Pi shell tier above file anchors.

    Walks all currently-emitted file / composite nodes to collect their
    folder paths (+ all ancestor paths), then emits one anchor per distinct
    folder. Registers any new cluster ids (e.g. '<root>' for the top
    anchor) into the passed cluster_ids list so they appear in the
    clusters array. Returns the number of folder anchors added."""
    files_by_folder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    composites_by_folder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        m = n.get("meta") or {}
        kind = m.get("kind")
        if kind == "file":
            path = m.get("path", "")
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            files_by_folder[folder].append(n)
        elif kind == "composite":
            folder = m.get("folder", "")
            if folder == "/":
                folder = ""
            composites_by_folder[folder].append(n)

    # All folder paths that ever held a child (+ their ancestors).
    all_folders: set[str] = set()
    for folder in list(files_by_folder.keys()) + list(composites_by_folder.keys()):
        if not folder:
            all_folders.add("")
            continue
        parts = folder.split("/")
        for i in range(len(parts) + 1):
            all_folders.add("/".join(parts[:i]))

    # Count direct children per folder for weight/meta.
    direct_files_per_folder: dict[str, int] = defaultdict(int)
    for folder, fa_list in files_by_folder.items():
        direct_files_per_folder[folder] += len(fa_list)
    for folder, ca_list in composites_by_folder.items():
        direct_files_per_folder[folder] += len(ca_list)

    direct_child_folders_per_folder: dict[str, int] = defaultdict(int)
    for folder in all_folders:
        if not folder:
            continue
        parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
        if parent in all_folders:
            direct_child_folders_per_folder[parent] += 1

    # Cluster id for each folder: use existing _cluster_id_for convention.
    # Register any new cluster ids (e.g. '<root>' for the top anchor) so they
    # appear in the final clusters array — otherwise schema validation rejects.
    known_clusters = set(cluster_ids)
    anchor_by_path: dict[str, dict[str, Any]] = {}
    for folder in sorted(all_folders):
        raw_cluster = _cluster_id_for(folder, _DEFAULT_CLUSTER_DEPTH)
        if raw_cluster not in known_clusters:
            cluster_ids.append(raw_cluster)
            known_clusters.add(raw_cluster)
        cluster_id = _safe_id_part(raw_cluster)
        anchor = _folder_anchor_node(
            folder,
            root_name,
            direct_files_per_folder.get(folder, 0),
            direct_child_folders_per_folder.get(folder, 0),
            cluster_id,
        )
        anchor_by_path[folder] = anchor
        nodes.append(anchor)

    # Containment: parent folder anchor -> child folder anchor.
    for folder in anchor_by_path:
        if not folder:
            continue
        parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
        if parent in anchor_by_path:
            edges.append({
                "source": anchor_by_path[parent]["id"],
                "target": anchor_by_path[folder]["id"],
                "kind": "contains",
                "weight": 1.0,
            })

    # Containment: folder anchor -> file / composite child.
    for folder, fa_list in files_by_folder.items():
        if folder in anchor_by_path:
            for fa in fa_list:
                edges.append({
                    "source": anchor_by_path[folder]["id"],
                    "target": fa["id"],
                    "kind": "contains",
                    "weight": 1.0,
                })
    for folder, ca_list in composites_by_folder.items():
        if folder in anchor_by_path:
            for ca in ca_list:
                edges.append({
                    "source": anchor_by_path[folder]["id"],
                    "target": ca["id"],
                    "kind": "contains",
                    "weight": 1.0,
                })

    return len(anchor_by_path)


def _doc_anchor_from_paragraphs(
    f: Path, folder_rel: str, cluster_id: str, paragraph_count: int
) -> dict[str, Any]:
    """File-anchor node for a paragraph-split file. Same kind='file' as atomic
    file nodes, but carries paragraph_count + a file_stem tag that visually
    groups it with its paragraph children (which already carry that tag).
    Container of N paragraph children via 'contains' edges (Pi shell)."""
    rel_path = f"{folder_rel}/{f.name}" if folder_rel else f.name
    size = f.stat().st_size
    ext = _file_ext(f.name)
    file_stem = f.stem
    weight = min(2.5, 1.0 + math.log10(max(1.0, paragraph_count + 1)) * 0.4)
    return {
        "id": f"fs.file.{_safe_id_part(rel_path)}",
        "label": f"{f.name} ({paragraph_count} paragraphs)",
        "cluster": cluster_id,
        "tags": ["filesystem", "file"] + ([ext] if ext else []) + [file_stem],
        "weight": round(max(0.6, weight), 2),
        "meta": {
            "kind": "file",
            "path": rel_path,
            "size_bytes": size,
            "extension": ext,
            "paragraph_count": paragraph_count,
            "created_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
        },
    }


def _file_ext(name: str) -> str:
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[1].lower()


def _ext_or_mixed(files: list[Path]) -> str:
    exts = {_file_ext(f.name) for f in files}
    if len(exts) == 1:
        ext = next(iter(exts))
        return ext or "<noext>"
    return "mixed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="interspace.adapters.filesystem_tree",
        description=(
            "Walk a directory tree and emit Interspace JSON with density-aware "
            "aggregation of versioned/timestamped/serial file families."
        ),
    )
    parser.add_argument("root", type=Path, help="Root directory to walk.")
    parser.add_argument(
        "--output", "-o", type=Path, required=True,
        help="Output path for the Interspace JSON file.",
    )
    parser.add_argument(
        "--density-threshold", type=int, default=_DEFAULT_DENSITY,
        help=f"Minimum group size to emit a composite node (default {_DEFAULT_DENSITY}).",
    )
    parser.add_argument(
        "--max-depth", type=int, default=None,
        help="Maximum directory depth to walk (default: unlimited).",
    )
    parser.add_argument(
        "--cluster-depth", type=int, default=_DEFAULT_CLUSTER_DEPTH,
        help=f"Path depth used to derive cluster ids (default {_DEFAULT_CLUSTER_DEPTH} = top-level subdirs).",
    )
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="Glob patterns to skip (matches against any path component or filename). Can be repeated.",
    )
    parser.add_argument(
        "--title", type=str, default=None,
        help="Override meta.title in the output.",
    )
    parser.add_argument(
        "--description", type=str, default=None,
        help="Override meta.description in the output.",
    )
    parser.add_argument(
        "--no-explode", dest="explode_text", action="store_false",
        help="Disable paragraph-explosion of text files (default: ON for .txt/.md/.markdown/.rst/.org).",
    )
    parser.set_defaults(explode_text=True)
    parser.add_argument(
        "--explode-size", type=int, default=_DEFAULT_EXPLODE_SIZE,
        help=f"Max file size (bytes) eligible for paragraph-explosion (default {_DEFAULT_EXPLODE_SIZE}).",
    )
    args = parser.parse_args(argv)

    excludes = tuple(args.exclude) if args.exclude else _DEFAULT_EXCLUDES

    try:
        payload = to_interspace_json(
            root=args.root,
            density_threshold=args.density_threshold,
            max_depth=args.max_depth,
            cluster_depth=args.cluster_depth,
            excludes=excludes,
            title=args.title,
            description=args.description,
            explode_text=args.explode_text,
            explode_size=args.explode_size,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    from collections import Counter as _Counter
    kinds = _Counter(n.get("meta", {}).get("kind", "file") for n in payload["nodes"])
    breakdown = ", ".join(f"{kind}: {n}" for kind, n in sorted(kinds.items()))
    edge_kinds = _Counter(e.get("kind", "?") for e in payload["edges"])
    edge_breakdown = ", ".join(f"{k}: {n}" for k, n in sorted(edge_kinds.items())) or "none"
    print(
        f"wrote {args.output}: {len(payload['nodes'])} nodes ({breakdown}), "
        f"{len(payload['edges'])} edges ({edge_breakdown}), "
        f"{len(payload['clusters'])} clusters",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
