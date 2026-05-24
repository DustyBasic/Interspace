"""Schema validation for Interspace JSON inputs.

The public entry point is `validate_input(data)`, which returns the pair
`(errors, normalized)`:

- `errors` is a list of human-readable strings. Empty list means valid.
- `normalized` is the input dict with defaults applied (see
  docs/INPUT_SCHEMA.md), or `None` if validation failed.

The validator collects every issue it finds before returning rather than
short-circuiting on the first problem, so authors of input adapters see the
full picture from one run.
"""

from __future__ import annotations

from typing import Any


def validate_input(data: Any) -> tuple[list[str], dict[str, Any] | None]:
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["top-level must be a JSON object"], None

    nodes = data.get("nodes")
    edges = data.get("edges")

    if nodes is None:
        errors.append("missing required field 'nodes'")
    elif not isinstance(nodes, list):
        errors.append("'nodes' must be an array")
    elif not nodes:
        errors.append("'nodes' must be non-empty")

    if edges is None:
        errors.append("missing required field 'edges'")
    elif not isinstance(edges, list):
        errors.append("'edges' must be an array")

    if errors:
        return errors, None

    node_ids: set[str] = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{i}] must be an object")
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append(f"nodes[{i}].id is required and must be a non-empty string")
        elif nid in node_ids:
            errors.append(f"nodes[{i}].id {nid!r} is duplicated")
        else:
            node_ids.add(nid)
        if "weight" in node and not _is_non_negative_number(node["weight"]):
            errors.append(f"nodes[{i}].weight must be a non-negative number")
        if "tags" in node and not _is_string_list(node["tags"]):
            errors.append(f"nodes[{i}].tags must be an array of strings")
        if "archived" in node and not isinstance(node["archived"], bool):
            errors.append(f"nodes[{i}].archived must be a boolean")
        if "archived_at" in node and not isinstance(node["archived_at"], str):
            errors.append(f"nodes[{i}].archived_at must be a string")
        if "archived_from_cluster" in node and not isinstance(node["archived_from_cluster"], str):
            errors.append(f"nodes[{i}].archived_from_cluster must be a string")
        if "phase" in node and node["phase"] not in ("current", "foundation", "archived"):
            errors.append(
                f"nodes[{i}].phase must be one of 'current', 'foundation', 'archived'"
            )

    cluster_ids: set[str] = set()
    raw_clusters = data.get("clusters")
    if raw_clusters is not None:
        if not isinstance(raw_clusters, list):
            errors.append("'clusters' must be an array if present")
            raw_clusters = []
        else:
            for i, cluster in enumerate(raw_clusters):
                if not isinstance(cluster, dict):
                    errors.append(f"clusters[{i}] must be an object")
                    continue
                cid = cluster.get("id")
                if not isinstance(cid, str) or not cid:
                    errors.append(
                        f"clusters[{i}].id is required and must be a non-empty string"
                    )
                elif cid in cluster_ids:
                    errors.append(f"clusters[{i}].id {cid!r} is duplicated")
                else:
                    cluster_ids.add(cid)

    if raw_clusters is not None and cluster_ids:
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            ncluster = node.get("cluster")
            if ncluster is not None and ncluster not in cluster_ids:
                errors.append(
                    f"nodes[{i}].cluster {ncluster!r} does not match any cluster.id"
                )

    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"edges[{i}] must be an object")
            continue
        src = edge.get("source")
        tgt = edge.get("target")
        if not isinstance(src, str) or not src:
            errors.append(
                f"edges[{i}].source is required and must be a non-empty string"
            )
        elif src not in node_ids:
            errors.append(f"edges[{i}].source {src!r} does not match any node.id")
        if not isinstance(tgt, str) or not tgt:
            errors.append(
                f"edges[{i}].target is required and must be a non-empty string"
            )
        elif tgt not in node_ids:
            errors.append(f"edges[{i}].target {tgt!r} does not match any node.id")
        if "weight" in edge and not _is_non_negative_number(edge["weight"]):
            errors.append(f"edges[{i}].weight must be a non-negative number")

    if errors:
        return errors, None

    return [], _apply_defaults(data)


def _is_non_negative_number(x: Any) -> bool:
    # bool is a subclass of int in Python; exclude it explicitly.
    if isinstance(x, bool):
        return False
    return isinstance(x, (int, float)) and x >= 0


def _is_string_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(s, str) for s in x)


def _apply_defaults(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["meta"] = dict(data.get("meta") or {})

    normalized_nodes: list[dict[str, Any]] = []
    for node in data["nodes"]:
        n = dict(node)
        n.setdefault("label", n["id"])
        n.setdefault("cluster", "uncategorized")
        n.setdefault("tags", [])
        n.setdefault("weight", 1.0)
        n.setdefault("meta", {})
        n.setdefault("archived", False)
        # phase defaults to "archived" if archived=True, else "current"
        if "phase" not in n:
            n["phase"] = "archived" if n.get("archived") else "current"
        normalized_nodes.append(n)
    out["nodes"] = normalized_nodes

    normalized_edges: list[dict[str, Any]] = []
    for edge in data["edges"]:
        e = dict(edge)
        e.setdefault("kind", "related")
        e.setdefault("weight", 1.0)
        e.setdefault("meta", {})
        normalized_edges.append(e)
    out["edges"] = normalized_edges

    raw_clusters = list(data.get("clusters") or [])
    declared_ids = {c["id"] for c in raw_clusters if isinstance(c, dict) and "id" in c}
    if (
        any(n["cluster"] == "uncategorized" for n in normalized_nodes)
        and "uncategorized" not in declared_ids
    ):
        raw_clusters.append({"id": "uncategorized", "label": "Uncategorized"})

    normalized_clusters: list[dict[str, Any]] = []
    for cluster in raw_clusters:
        c = dict(cluster)
        c.setdefault("label", c["id"])
        c.setdefault("meta", {})
        normalized_clusters.append(c)
    out["clusters"] = normalized_clusters

    return out
