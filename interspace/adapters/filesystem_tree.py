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


_DEFAULT_EXCLUDES = (".*", "*.pyc", "__pycache__", "node_modules", "*.bak")
_DEFAULT_DENSITY = 3
_DEFAULT_CLUSTER_DEPTH = 1

_TIMESTAMP_RE = re.compile(r"^(.+?)_\d{4,8}[_-]?\d{0,6}[A-Z]?$")
_VERSION_RE = re.compile(r"^(.+?)[._-]v\d+(\.\d+)*$")
_NUMBERED_RE = re.compile(r"^(\d{2,4})[._-](.+)$")
_CURRENT_RE = re.compile(r"^CURRENT[_-](.+)$", re.IGNORECASE)
_CHECKSUM_RE = re.compile(r"^(CHECKSUMS?_[A-Z0-9]+)_.*$")

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
) -> dict[str, Any]:
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"root directory not found: {root}")

    files = _walk(root, max_depth=max_depth, excludes=excludes)

    grouped_per_folder = _group_by_folder_and_pattern(files, root)

    nodes: list[dict[str, Any]] = []
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
                nodes.append(_file_node(f, folder_rel, cluster_id_safe))

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
        "edges": [],
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


def _file_node(f: Path, folder_rel: str, cluster_id: str) -> dict[str, Any]:
    rel_path = f"{folder_rel}/{f.name}" if folder_rel else f.name
    size = f.stat().st_size
    ext = _file_ext(f.name)
    weight = min(2.0, 0.6 + math.log10(max(1.0, size / 1024.0)) * 0.4)
    return {
        "id": f"fs.file.{_safe_id_part(rel_path)}",
        "label": f.name,
        "cluster": cluster_id,
        "tags": ["filesystem", "file"] + ([ext] if ext else []),
        "weight": round(max(0.6, weight), 2),
        "meta": {
            "kind": "file",
            "path": rel_path,
            "size_bytes": size,
            "extension": ext,
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
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    composites = sum(1 for n in payload["nodes"] if n.get("meta", {}).get("kind") == "composite")
    print(
        f"wrote {args.output}: {len(payload['nodes'])} nodes "
        f"({composites} composite, {len(payload['nodes']) - composites} individual), "
        f"{len(payload['clusters'])} clusters",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
