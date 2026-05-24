"""HTML page generator for Interspace.

Reads an Interspace JSON input file, validates it against the schema in
docs/INPUT_SCHEMA.md, and renders a set of static HTML pages plus copies the
JS/CSS asset bundle. Output layout:

    output_dir/
      index.html
      lattice.html
      clusters/{cluster_id}.html      one per cluster
      nodes/{node_id}.html            one per node
      static/
        js/cytoscape.min.js
        js/lattice.js
        css/style.css

Templates live in `<package>/../templates/`, assets in `<package>/../static/`.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .validator import validate_input


MAX_TAG_CHIPS = 30


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

    print(
        f"rendered: {counts['pages']} pages "
        f"({counts['nodes']} nodes, {counts['edges']} edges, "
        f"{counts['clusters']} clusters) -> {output_dir}",
        file=sys.stderr,
    )
    return 0


def _write_index(
    env: Environment,
    output_dir: Path,
    meta: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> int:
    html = env.get_template("index.html").render(
        asset_prefix="",
        meta=meta,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
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
    epoch_min_ms, epoch_max_ms = _node_epoch_range(nodes)
    # Defensive: escape any `</` so the JSON can't terminate the surrounding
    # <script type="application/json"> block.
    lattice_json = (
        json.dumps(lattice_payload, separators=(",", ":"))
        .replace("</", "<\\/")
    )
    tag_counts = Counter(t for n in nodes for t in n["tags"])
    # Cap chips to top-N by frequency to keep the controls bar usable at scale;
    # the search input still matches any tag substring.
    all_tags = [
        t for t, _ in sorted(
            tag_counts.most_common(MAX_TAG_CHIPS),
            key=lambda kv: (-kv[1], kv[0]),
        )
    ]
    archived_count = sum(1 for n in nodes if n.get("archived"))
    html = env.get_template("lattice.html").render(
        asset_prefix="",
        meta=meta,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
        all_tags=all_tags,
        lattice_data_json=lattice_json,
        epoch_min_ms=epoch_min_ms,
        epoch_max_ms=epoch_max_ms,
        epoch_min_iso=_ms_to_date_str(epoch_min_ms),
        epoch_max_iso=_ms_to_date_str(epoch_max_ms),
        has_archived=archived_count > 0,
        archived_count=archived_count,
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
    return out


def _cluster_lattice_node(n: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": n["id"],
        "label": n["label"],
        "weight": n["weight"],
    }
    ts = _node_created_ms(n)
    if ts is not None:
        out["ts"] = ts
    if n.get("archived"):
        out["archived"] = True
        if n.get("archived_at"):
            out["archived_at"] = n["archived_at"]
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

        cluster_lattice_payload = {
            "cluster_id": cid,
            "cluster_color": cluster.get("color"),
            "nodes": [_cluster_lattice_node(n) for n in cluster_nodes],
            "edges": [
                {
                    "source": e["source"],
                    "target": e["target"],
                    "kind": e["kind"],
                    "weight": e["weight"],
                }
                for e in intra_edges
            ],
        }
        cluster_lattice_json = (
            json.dumps(cluster_lattice_payload, separators=(",", ":"))
            .replace("</", "<\\/")
        )

        cluster_epoch_min_ms, cluster_epoch_max_ms = _node_epoch_range(cluster_nodes)

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
            cluster_lattice_data_json=cluster_lattice_json,
            cluster_epoch_min_ms=cluster_epoch_min_ms,
            cluster_epoch_max_ms=cluster_epoch_max_ms,
            cluster_epoch_min_iso=_ms_to_date_str(cluster_epoch_min_ms),
            cluster_epoch_max_iso=_ms_to_date_str(cluster_epoch_max_ms),
        )
        (cluster_dir / f"{cid}.html").write_text(html, encoding="utf-8")
        written += 1

    return written


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
        )
        (node_dir / f"{nid}.html").write_text(html, encoding="utf-8")
        written += 1

    return written


def _copy_static_assets(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(_STATIC_DIR, target)
