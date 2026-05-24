# Writing an Interspace adapter

An **adapter** maps some data source (a SQLite DB, a JSON file, an API, a
markdown corpus) to the Interspace JSON shape defined in
[`INPUT_SCHEMA.md`](INPUT_SCHEMA.md). Once your data validates, the renderer
produces the lattice — you never touch templates or JavaScript.

This doc walks through the adapter contract, field-level conventions, the
archive-aware mode, and a generic skeleton you can copy.

## The contract

Every adapter exposes:

1. A function `to_interspace_json(source) -> dict` that returns a payload
   conforming to `INPUT_SCHEMA.md`.
2. A CLI runnable as `python -m interspace.adapters.<name> <source> --output <json>`,
   optionally accepting `--archive <prev.json>` for persistent-catalog mode.

That's it. Adapters live in `interspace/adapters/`, each in its own module.
The shipped `interspace/adapters/` package is **empty** — adapters are
operator-specific by nature (they read your data), so they belong in your
repo, not the framework. Drop your adapter modules into the directory and
they become discoverable as `python -m interspace.adapters.<name>`.

The two-step pipeline keeps adapters independently testable:

```bash
python -m interspace.adapters.<name> <source> -o input.json   # adapter
python -m interspace render input.json -o rendered/            # renderer
```

## You don't always need an adapter

For one-off corpora — a handful of documents, a snapshot of a tree, a
research dataset you'll render once — writing the Interspace JSON by hand
is the simplest path. The schema is permissive enough that hand-writing
JSON for ~10–30 nodes is faster than building an adapter.

Recommended layout: put hand-curated inputs under `local/inputs/<name>.json`
and render to `local/rendered/<name>/`. The `local/` directory is
gitignored, so private corpora never leak into the public repo.

The right time to convert hand-curation into a real adapter is when (a) the
corpus grows large enough that maintaining the JSON by hand becomes the
bottleneck, or (b) the source data updates frequently and you want
one-command refreshes.

## Archive-aware mode

Adapters that want to act as a **persistent catalog** — preserving nodes
that have disappeared from the source — accept an `--archive
<previous.json>` flag:

```bash
python -m interspace.adapters.<name> <source> \
  --archive <archive.json> -o <current.json>
```

On every run:

1. Read the archive file (if it exists) to learn which nodes were known last time.
2. Build the current state from the source as usual.
3. Identify nodes that were in the archive but are no longer in the source —
   mark them `archived=true`, `archived_at=<today>`, `archived_from_cluster=<previous cluster>`.
4. Merge: emit current nodes + archived nodes in one payload.
5. Preserve edges between archived nodes if both endpoints survive, and
   preserve clusters that have no current counterpart but still hold archived nodes.
6. Rewrite the archive file with the merged state, so the next run picks up
   newly-archived nodes.

The renderer styles archived nodes at 0.4 opacity with dashed borders, and
exposes an "Include archived (N)" toggle in the lattice controls bar.
Per-node detail pages show an "Archived on X; last active in Y" banner.

A reference implementation of `merge_archive()` is sketched below — drop it
into your adapter as-is, or factor it into a shared helper if you ship
multiple adapters.

## Field-level conventions

The schema is permissive — most fields are optional — so adapters get to
choose which signal to surface. Conventions that work well in practice:

### `nodes[].id`
Stable, opaque identifier. If your source has its own IDs (DB primary keys,
URLs, file paths), prefix them with a short namespace so they don't collide
when you blend sources later. e.g. `r42` for row 42, `n_abc123` for a
note hash, `p_<project>` for a project.

### `nodes[].label`
What humans read on the lattice and lists. Keep it short — 50–80 chars works
well. If your source has long content (paragraphs, code, transcripts), put
the full body in `node.meta.content` and shorten for the label.

### `nodes[].cluster`
Single string referencing a `clusters[].id`. If a record naturally belongs to
multiple groups (tags, projects, vertices), pick a primary axis for the
cluster and use `tags` for the rest. See **cluster assignment** below for
strategies.

### `nodes[].tags`
Array of short strings. These power the tag-chip filter on the lattice and
the optional "color by tag" mode. Singletons are fine (the search input still
matches them) but extremely granular tags (per-record UUIDs) will get
collapsed by the top-N chip cap.

### `nodes[].weight`
Use it to encode importance, recency, vote count, confidence — anything
that should make a node visually larger. The renderer maps `0..3` to node
sizes `18..60` px.

### `nodes[].meta`
Adapter-specific passthrough. The renderer ignores it for layout but renders
it as JSON on the per-node detail page. Use it for the full content, source
URLs, timestamps, scores — anything you'd want to see when you click into a
node.

### `nodes[].meta.created_at`
ISO-8601 timestamp. If set, the renderer collects min/max across all nodes
and shows a **time slider** on the lattice — drag to filter to a point in
time. Especially good for ledger-style data.

### `edges[].source` / `edges[].target`
Must reference existing `nodes[].id`. The validator fails the render if you
emit dangling edges. Always check that both endpoints exist before pushing
the edge.

### `edges[].kind`
Short semantic label rendered on the edge in the lattice. Examples:
`derives`, `instantiates`, `adjacent`, `basis`, `inverse`, `references`,
`replies_to`. Long human prose belongs in `edge.meta.note`, not `kind`.

### `clusters[].color`
Optional CSS color. If you don't supply one, the renderer assigns from a
12-color palette by cluster order. Provide colors when you have a brand
or domain mapping (project colors, severity levels, etc.).

## Cluster assignment strategies

When records naturally belong to multiple groups, you have to pick one
axis for `node.cluster`. Common patterns:

- **Most-frequent + strength tiebreak** — count how many times the source
  references the record under each group; highest count wins, summed
  strength breaks ties.
- **First-mentioned** — earliest reference wins. Cheap, but biased toward
  setup order.
- **User-pinned** — your source already has a "primary group" column; just
  use it.
- **Domain-derived** — compute from the record itself (e.g. file path prefix
  becomes the cluster). No source-side group needed.

Whichever you choose, document it in your adapter's docstring so consumers
know what the cluster axis means.

## Timestamps

Interspace expects ISO-8601 strings. The renderer is forgiving and handles:

- `2026-04-23T22:43:06+00:00` (canonical)
- `2026-04-23T22:43:06Z` (UTC short form)
- `2026-04-23T22:43:06+00:00Z` (some sources emit both an offset and a
  trailing `Z` — accepted)

Naive timestamps (no offset, no `Z`) are treated as UTC.

## Generic skeleton

A minimal adapter that reads some source, emits valid Interspace JSON, and
supports `--archive` looks like this:

```python
"""Adapter: <your source> -> Interspace JSON."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def to_interspace_json(source: Path) -> dict[str, Any]:
    """Read `source` and return an Interspace-schema payload."""
    # ... your source-specific logic here ...
    return {
        "meta": {"title": "...", "source": "your-adapter@0.1"},
        "nodes": [
            # {"id": "...", "label": "...", "cluster": "...", "tags": [...],
            #  "weight": 1.0, "meta": {"created_at": "...", ...}}
        ],
        "edges": [
            # {"source": "...", "target": "...", "kind": "...", "weight": 1.0}
        ],
        "clusters": [
            # {"id": "...", "label": "...", "color": "#hexcode"}
        ],
    }


def merge_archive(
    payload: dict[str, Any], archive_path: Path, today_iso: str
) -> tuple[dict[str, Any], int, int]:
    """Merge archived nodes/edges/clusters from a previous run into payload.

    Returns (merged_payload, newly_archived_count, already_archived_count).
    If archive_path doesn't exist, returns payload unchanged with (0, 0).
    """
    if not archive_path.exists():
        return payload, 0, 0

    prev = json.loads(archive_path.read_text(encoding="utf-8"))
    prev_nodes_by_id = {n["id"]: n for n in prev.get("nodes", []) if "id" in n}
    current_ids: set[str] = {n["id"] for n in payload["nodes"]}

    archived_nodes: list[dict[str, Any]] = []
    newly = already = 0
    for prev_id, prev_node in prev_nodes_by_id.items():
        if prev_id in current_ids:
            continue
        if prev_node.get("archived"):
            archived_nodes.append(prev_node)
            already += 1
        else:
            n = dict(prev_node)
            n["archived"] = True
            n["archived_at"] = today_iso
            if "cluster" in n:
                n["archived_from_cluster"] = n["cluster"]
            archived_nodes.append(n)
            newly += 1

    payload["nodes"].extend(archived_nodes)

    merged_ids = current_ids | {n["id"] for n in archived_nodes}
    existing_edges = {(e["source"], e["target"], e["kind"]) for e in payload["edges"]}
    for e in prev.get("edges", []):
        key = (e.get("source"), e.get("target"), e.get("kind"))
        if None in key or key in existing_edges:
            continue
        if e["source"] in merged_ids and e["target"] in merged_ids:
            payload["edges"].append(e)

    current_cluster_ids = {c["id"] for c in payload["clusters"]}
    needed = {n.get("cluster") for n in archived_nodes}
    needed.discard(None)
    for prev_c in prev.get("clusters", []):
        cid = prev_c.get("id")
        if not cid or cid in current_cluster_ids:
            continue
        if cid in needed:
            payload["clusters"].append(dict(prev_c))

    return payload, newly, already


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--archive", type=Path, default=None)
    args = parser.parse_args(argv)

    payload = to_interspace_json(args.source)

    if args.archive is not None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload, _, _ = merge_archive(payload, args.archive, today)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if args.archive is not None:
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        args.archive.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## Merging multiple sources into one lattice

When several inputs should display in a single lattice — typically a live
data source plus historical/foundational sources that pre-date it — the
`interspace merge` subcommand combines them:

```bash
python -m interspace merge merge_config.json -o combined.json
python -m interspace render combined.json -o rendered/
```

The merge config declares each source with a prefix (to namespace its ids
and avoid collisions) and a phase (`current`, `foundation`, or `archived`):

```json
{
  "meta": {
    "title": "Combined view",
    "description": "Live source + foundations"
  },
  "sources": [
    {"path": "live.json",        "prefix": "lv", "phase": "current"},
    {"path": "foundation_a.json","prefix": "fa", "phase": "foundation"},
    {"path": "foundation_b.json","prefix": "fb", "phase": "foundation"}
  ]
}
```

Each node in the output is tagged with its source's phase. The renderer
shades by phase: `current` nodes display normally, `foundation` nodes get
dotted borders at 0.7 opacity, `archived` nodes get dashed borders at 0.4
opacity. Edges between mixed-phase endpoints take the more-faded styling.

Cross-source edges aren't currently supported — only edges within a single
source survive the merge. Add edges between sources by hand-editing the
combined JSON after running merge, or by writing them into one of the
sources before merging.

## Suggested adapter shapes

The model fits best where there's **structure that's hard to see in a flat
list** — clusters forming, unexpected bridges, isolated nodes, temporal
drift. Some shapes that work well:

- **markdown_corpus** — one node per `.md` file; edges from explicit links;
  cluster by top-level folder; tags from frontmatter.
- **github_repo** — nodes for files / issues / PRs; edges from
  references; cluster by directory.
- **slack_archive** — nodes for messages or threads; edges for replies and
  cross-references; cluster by channel; weight by reaction count;
  `created_at` for the time slider.
- **bibliography** — nodes for papers; edges for citations; cluster by
  research area; weight by citation count.
- **filesystem_tree** — one node per file under a root; edges from filename
  references inside file contents; cluster by top-level folder.
- **sqlite_schema** — for any DB with a relational structure: pick a table
  as the node-source, foreign-key columns as edges, derived attribute (or
  user-pinned column) as cluster.
