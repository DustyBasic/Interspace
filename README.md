# Interspace

**The interface layer between your structured data and your eyes.**

Interspace renders graph-shaped data (nodes + edges + clusters) as a navigable
static HTML site: a force-directed lattice view with search / tag / time
filters, per-cluster pages with their own mini-lattices, and a per-node detail
page for every record. No server runtime — the output is plain HTML you open
locally, commit to a repo, or host on any static file server.

## Why

Structured data carries relationship information that flat lists can't show.
SQL gives you slices; graph rendering gives you the gestalt — clusters
forming, isolated nodes, cross-domain bridges, drift patterns. Things you
weren't looking for.

Interspace sits at the **interface** layer: it doesn't store your data, it
doesn't query it, it **renders** it as something a human can navigate.

## v0.1 features

- **Force-directed 3D lattice** of every node and edge, rendered via vendored
  Three.js + 3d-force-graph (~1.8MB JS, SHA256-pinned). Drag to rotate,
  scroll to zoom, right-click + drag to pan, click any sphere to open its
  detail page. Scales cleanly to ~1000+ nodes where 2D cose layout
  collapses
- **Per-cluster pages** with intra-cluster and cross-cluster edge listings
- **Per-node pages** showing incoming/outgoing edges (with quoted citation
  context where the adapter emitted it), tags, weight, source-document
  context (full doc scrollable, current paragraph centered), linked
  continuations (cross-doc jumps via strong edges), adapter metadata
- **Archive-aware adapter mode** — adapters that accept `--archive
  <previous.json>` merge previously-known nodes that have disappeared from
  the source, marking them `archived` (rendered at 0.4 opacity, dashed
  edges) with `archived_at` + `archived_from_cluster` metadata. The archive
  file auto-maintains itself for next run, so the HTML becomes the
  persistent catalog of every node ever observed
- **Local server + auto-open** via `python -m interspace serve <dir>` —
  stdlib-only HTTP server with optional browser launch, for quick previews
  without spinning up a separate `http.server` invocation
- **Live discovery layer** (`--live` flag on `serve`) — three lightweight
  in-server runners (T-cell / REL / NEG-T at Interspace weights, not Pi
  spec depth) scan the rendered lattice for new cross-source relations on
  a periodic cycle and broadcast discoveries via Server-Sent Events. The
  lattice page subscribes and animates each new edge as a runner-colored
  visual event — the constellation reshuffles in front of the operator as
  the substrate accretes. Visual clock (jittered 1-3s per runner) runs
  client-side independent of the server discovery cycle, so the lattice
  always feels alive between actual discoveries
- **Auto-validation** of input JSON against the schema; all errors collected
  and reported together
- **Pluggable input adapters** — write `to_interspace_json(source) -> dict`,
  the renderer handles the rest
- **Vendored, version-pinned JS** with SHA256 provenance — no CDN dependency

## Quick start

```bash
pip install jinja2
python -m interspace render samples/example.json --output rendered/
# open rendered/index.html in a browser
```

For your own data, write an adapter (see `docs/ADAPTERS.md`) and run:

```bash
python -m interspace.adapters.<your_adapter> <source> -o input.json
python -m interspace render input.json -o rendered/
```

## Architecture

```
your data source (SQLite / JSON / CSV / API)
       │
       │  (adapter — see docs/ADAPTERS.md)
       ▼
Interspace JSON  ({meta, nodes, edges, clusters} — see docs/INPUT_SCHEMA.md)
       │
       ▼
   interspace render
       │
       ▼
rendered HTML pages
   ├── index.html                  ← clusters + node list
   ├── lattice.html                ← 3D force-directed view
   ├── clusters/<cluster_id>.html  ← per cluster (node list + intra/cross edges)
   ├── nodes/<node_id>.html        ← per node
   └── static/                     ← vendored JS, CSS
       ├── js/three.min.js         ← 3D renderer (peer dep)
       ├── js/3d-force-graph.min.js
       ├── js/3d-force-graph.version.txt
       ├── js/lattice_3d.js        ← lattice init + zoom controls + nav
       ├── js/theme.js             ← dark mode toggle
       └── css/style.css
```

## Docs

- [`docs/INPUT_SCHEMA.md`](docs/INPUT_SCHEMA.md) — the JSON shape your data
  must validate against
- [`docs/ADAPTERS.md`](docs/ADAPTERS.md) — adapter contract + a generic
  skeleton + archive-aware mode + hand-curation guidance

## Samples

One reference render ships with the repo:

- `samples/example.json` + `samples/rendered_example/` — minimal synthetic
  input exercising every schema field (6 nodes, 4 clusters). Use this to
  learn the schema or as a regression-check that the renderer still
  produces clean output.

Open it locally:

```bash
python -m interspace serve samples/rendered_example/
# starts a local HTTP server and opens the default browser
```

Pass `--port <N>` to override the default 8000, or `--no-open` to skip the
auto-browser launch. The `serve` subcommand works on any rendered directory
(including your own renders under `local/rendered/`).

## Operator-private data

Adapters typically read user-specific data, and their outputs (rendered HTML
and any `--archive` files) accumulate state over time. The recommended
convention is to keep all that under a `local/` directory at the repo root
that is `.gitignore`d. Nothing under `local/` is ever committed.

A common layout:

```
local/
├── adapters/                    # your adapter modules (private to you)
├── inputs/                      # adapter outputs + archive files
└── rendered/                    # `interspace render` outputs
```

## Persistent-catalog pattern

The rendered HTML is self-contained — it doesn't connect back to the source
database to display anything. Once written, it's a frozen view of the lattice
at that moment, and it survives unchanged even if the source data evolves
(rows being garbage-collected, projects being completed and removed,
records being archived, etc.).

Two ways to make that into a persistent catalog of state-over-time:

**Dated snapshots** — render to a dated directory and keep each one. Useful
when you want to answer *"what did this look like last month?"* by opening
the snapshot from that month:

```bash
DATE=$(date +%Y-%m-%d)
python -m interspace.adapters.<your_adapter> <source> -o input.json
python -m interspace render input.json -o local/rendered/snapshots/$DATE/
```

**Archive-aware mode** — let the renderer preserve archived nodes alongside
current ones in a single view. The adapter must accept `--archive <path>`;
nodes that disappear from the source on the next run get tagged `archived`
and stay visible in the lattice (rendered at 0.4 opacity, dashed edges),
with an "Include archived (N)" toggle in the controls bar:

```bash
python -m interspace.adapters.<your_adapter> <source> \
  --archive local/inputs/<your_adapter>_archive.json \
  -o local/inputs/<your_adapter>.json
python -m interspace render local/inputs/<your_adapter>.json \
  -o local/rendered/<your_adapter>/
```

The archive file auto-maintains itself across runs and belongs in `local/`
(never committed). See [`docs/ADAPTERS.md`](docs/ADAPTERS.md) for the
contract that adapters must implement to support this mode.

## Design constraints

- **Static-first.** Output is plain HTML. Works offline. Version-controllable.
- **Stdlib-friendly.** Python 3.11+ with `jinja2` as the only required dep.
- **Vendored JS.** Three.js + 3d-force-graph bundled at pinned versions with
  SHA256 in `static/js/3d-force-graph.version.txt`. Never re-fetched silently.
- **Compose, don't own.** Interspace doesn't replace your storage, query
  layer, or note-taking. It adds the visualization leg.
- **Minimal bundled adapters.** The `interspace/adapters/` package ships
  one generic adapter (`filesystem_tree`) that works on any directory.
  Adapters that read private or operator-specific schemas should live in
  your own repo or in `local/adapters/`, not in the framework.

## Status

**v0.1 — shipped.** Renderer, schema, validator, adapter contract +
archive-aware mode, hand-curated input pattern. One synthetic reference
sample (`example.json`) committed.

**v0.4 — 3D lattice is the canonical view.** Vendored Three.js +
3d-force-graph. Force-directed 3D with rotate / zoom / pan /
click-to-navigate. Scales to ~1000+ nodes where 2D cose layout collapsed;
the prior 2D Cytoscape renderer was removed.

**v0.2 — likely next:** `markdown_corpus` adapter (one node per `.md`
file, edges from link references, frontmatter → tags); per-dataset
archive view (dedicated filtered page); node detail pages showing
inbound/outbound *edge kinds* grouped semantically.

**v0.5 — spatial-hierarchy navigation (planned).** Extend the 3D view so
camera zoom level traverses container hierarchy: zooming into a cluster's
volume expands it into its own local force-directed sub-graph; zooming
further expands a document anchor into its paragraphs; zooming out
restores parent context. Each zoom level a fresh force solve over a
filtered slice of the data.

## License

MIT
