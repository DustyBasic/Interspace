"""Merge multiple Interspace JSON inputs into one combined payload.

Used to integrate datasets that should display in a single lattice but
have different temporal phases — e.g., a live data source plus several
historical/foundational sources that pre-date it.

Each source contributes prefixed ids (to avoid collisions) and tags every
node it emits with a `phase` string (`current`, `archived`, or `foundation`).
The renderer styles nodes differently per phase.

Configuration file format (JSON):

    {
        "meta": {
            "title": "...",
            "description": "..."
        },
        "sources": [
            {
                "path": "path/to/input.json",
                "prefix": "abc",            // prepended to ids
                "phase": "current"          // current | foundation | archived
            },
            ...
        ]
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .content_classifier import emit_conversation_segment_anchors, enrich_lattice
from .cross_refs import extract_cross_references


VALID_PHASES = {"current", "foundation", "archived"}


def apply_patches(
    payload: dict[str, Any], patches_path: Path
) -> tuple[dict[str, Any], int, int]:
    """Apply a cross-source edge patch file to a merged payload.

    The patch file is either a JSON array of edge objects with already-prefixed
    ids, or an object with `{"version": "1", "edges": [...]}` shape. Each edge
    is validated against the merged node set; edges whose endpoints don't exist
    in the payload are skipped (and counted as `dropped`).

    Returns (payload, applied_count, dropped_count).
    """
    if not patches_path.exists():
        raise FileNotFoundError(f"patches file not found: {patches_path}")

    raw = json.loads(patches_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        edges = raw.get("edges") or []
    elif isinstance(raw, list):
        edges = raw
    else:
        raise ValueError(
            f"patches file must be a list of edges or {{edges: [...]}}, got {type(raw).__name__}"
        )

    node_ids = {n["id"] for n in payload["nodes"] if "id" in n}
    existing_edge_keys = {
        (e.get("source"), e.get("target"), e.get("kind")) for e in payload["edges"]
    }

    applied = 0
    dropped = 0
    for e in edges:
        if not isinstance(e, dict):
            dropped += 1
            continue
        src = e.get("source")
        tgt = e.get("target")
        if not src or not tgt or src not in node_ids or tgt not in node_ids:
            dropped += 1
            continue
        key = (src, tgt, e.get("kind"))
        if key in existing_edge_keys:
            dropped += 1
            continue
        payload["edges"].append(dict(e))
        existing_edge_keys.add(key)
        applied += 1

    return payload, applied, dropped


def merge_inputs(config: dict[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    """Merge inputs declared in `config` into a single Interspace payload.

    `base_dir` is the directory the config was loaded from, used to resolve
    relative source paths. Pass `None` to treat paths as cwd-relative.
    """
    sources = config.get("sources") or []
    if not sources:
        raise ValueError("merge config has no 'sources'")

    out: dict[str, Any] = {
        "meta": dict(config.get("meta") or {}),
        "nodes": [],
        "edges": [],
        "clusters": [],
    }

    seen_cluster_ids: set[str] = set()

    for src in sources:
        path_raw = src.get("path")
        prefix = src.get("prefix", "")
        phase = src.get("phase", "current")
        if phase not in VALID_PHASES:
            raise ValueError(
                f"source phase {phase!r} not in {sorted(VALID_PHASES)}"
            )
        if not path_raw:
            raise ValueError("source missing 'path'")

        src_path = Path(path_raw)
        if base_dir is not None and not src_path.is_absolute():
            src_path = (base_dir / src_path).resolve()

        data = json.loads(src_path.read_text(encoding="utf-8"))

        prefixed_node_ids: set[str] = set()
        for node in data.get("nodes", []):
            if not isinstance(node, dict) or "id" not in node:
                continue
            n = dict(node)
            n["id"] = _prefix(prefix, node["id"])
            prefixed_node_ids.add(n["id"])
            if "cluster" in n:
                n["cluster"] = _prefix(prefix, n["cluster"])
            # phase is set unless the node already declared one
            n.setdefault("phase", phase)
            out["nodes"].append(n)

        for edge in data.get("edges", []):
            if not isinstance(edge, dict):
                continue
            src_id = edge.get("source")
            tgt_id = edge.get("target")
            if not src_id or not tgt_id:
                continue
            new_src = _prefix(prefix, src_id)
            new_tgt = _prefix(prefix, tgt_id)
            if new_src not in prefixed_node_ids or new_tgt not in prefixed_node_ids:
                # Skip edges that reference nodes from other sources we haven't
                # processed yet or that don't exist; cross-source edges aren't
                # supported in this simple merge.
                continue
            e = dict(edge)
            e["source"] = new_src
            e["target"] = new_tgt
            out["edges"].append(e)

        for cluster in data.get("clusters", []):
            if not isinstance(cluster, dict) or "id" not in cluster:
                continue
            c = dict(cluster)
            new_id = _prefix(prefix, cluster["id"])
            if new_id in seen_cluster_ids:
                continue
            c["id"] = new_id
            # Tag the cluster with phase too so the renderer can group visually
            c.setdefault("phase", phase)
            seen_cluster_ids.add(new_id)
            out["clusters"].append(c)

    # Post-merge cross-reference pass — deterministic Stage 1 regex extraction
    # over the combined prefix-namespaced node set. Catches cross-source
    # mentions that no single adapter could resolve on its own (e.g. a node
    # in one source referencing a file that lives in another). Dedupes
    # against edges already emitted by each adapter so intra-source edges
    # aren't doubled.
    existing_edge_keys = {
        (e.get("source"), e.get("target"), e.get("kind")) for e in out["edges"]
    }
    cross_added = 0
    for edge in extract_cross_references(out["nodes"]):
        key = (edge["source"], edge["target"], edge["kind"])
        if key in existing_edge_keys:
            continue
        existing_edge_keys.add(key)
        out["edges"].append(edge)
        cross_added += 1
    out["meta"]["_post_merge_cross_refs_added"] = cross_added

    # Content-type classification (Stage 2 enrichment). Heuristic rule-based;
    # tags each text-bearing node with `meta.content_type`, each file anchor
    # with `meta.file_type`, and consecutive chat_turn runs with a shared
    # `meta.conversation_segment_id`. Idempotent — re-running re-classifies.
    classifier_stats = enrich_lattice(out["nodes"])
    out["meta"]["_content_classification"] = classifier_stats

    # Promote conversation segments to first-class nodes. Each segment
    # gets a `conversation_segment` anchor with `contains` edges to its
    # member chat_turns + a containment edge from the parent file anchor.
    # Pi shell tier — file → segment → turn — so chat-heavy archives
    # become navigable at the scene/exchange level instead of just at
    # line-of-dialogue level.
    seg_nodes, seg_edges = emit_conversation_segment_anchors(out["nodes"])
    out["nodes"].extend(seg_nodes)
    out["edges"].extend(seg_edges)
    out["meta"]["_conversation_segment_anchors"] = {
        "anchors_emitted": len(seg_nodes),
        "containment_edges_emitted": len(seg_edges),
    }

    return out


def _prefix(prefix: str, value: str) -> str:
    if not prefix:
        return value
    # `__` (not `:`) so ids stay safe on Windows NTFS, in URLs, and against
    # Cytoscape selector syntax (which reserves `:` for pseudo-classes).
    return f"{prefix}__{value}"


def merge_from_config(config_path: Path) -> dict[str, Any]:
    """Load a merge config from disk and run the merge."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return merge_inputs(config, base_dir=config_path.parent)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="interspace merge",
        description=(
            "Merge multiple Interspace JSON inputs into one combined payload, "
            "tagging each node with a 'phase' (current / foundation / archived)."
        ),
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to a merge config JSON file.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Output path for the merged Interspace JSON.",
    )
    parser.add_argument(
        "--patches",
        type=Path,
        default=None,
        help=(
            "Optional cross-source edge patch file (JSON list of edges with "
            "already-prefixed ids; endpoints validated against merged node set)."
        ),
    )
    args = parser.parse_args(argv)

    if not args.config.exists():
        print(f"error: config not found: {args.config}", file=sys.stderr)
        return 2

    try:
        payload = merge_from_config(args.config)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    patches_msg = ""
    if args.patches is not None:
        try:
            payload, applied, dropped = apply_patches(payload, args.patches)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
            print(f"error applying patches: {e}", file=sys.stderr)
            return 2
        patches_msg = f" + {applied} patch edges applied ({dropped} dropped)"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    phases = {n.get("phase", "current") for n in payload["nodes"]}
    print(
        f"merged {len(payload['nodes'])} nodes, {len(payload['edges'])} edges, "
        f"{len(payload['clusters'])} clusters (phases: {sorted(phases)}){patches_msg} "
        f"-> {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
