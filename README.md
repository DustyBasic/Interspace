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

- **Force-directed lattice** of every node and edge (Cytoscape.js, cose layout)
- **Per-cluster pages** with intra-cluster and cross-cluster edge listings
  plus a scoped mini-lattice
- **Per-node pages** showing incoming/outgoing edges, tags, weight, adapter
  metadata
- **Live filters** on the lattice page: text search (label / id / tag),
  tag chips (top-30 by frequency), color-by toggle (cluster or tag),
  time slider over `created_at`
- **Shared time cutoff** across main lattice and cluster mini-lattices
  (sessionStorage; per-tab) so a slider move on one page carries to others
- **Zoom controls** (in / out / fit-to-view) overlaid on every lattice canvas
- **Archive-aware adapter mode** — adapters that accept `--archive
  <previous.json>` merge previously-known nodes that have disappeared from
  the source, marking them `archived` (rendered at 0.4 opacity, dashed
  edges) with `archived_at` + `archived_from_cluster` metadata. The archive
  file auto-maintains itself for next run, so the HTML becomes the
  persistent catalog of every node ever observed
- **Local server + auto-open** via `python -m interspace serve <dir>` —
  stdlib-only HTTP server with optional browser launch, for quick previews
  without spinning up a separate `http.server` invocation
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
   ├── lattice.html                ← full network, filters
   ├── clusters/<cluster_id>.html  ← per cluster
   ├── nodes/<node_id>.html        ← per node
   └── static/                     ← vendored JS, CSS
       ├── js/cytoscape.min.js
       ├── js/cytoscape.version.txt
       ├── js/lattice.js
       ├── js/cluster_lattice.js
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
  input exercising every schema field (6 nodes, 4 clusters, all filter
  controls). Use this to learn the schema or as a regression-check that the
  renderer still produces clean output.

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
- **Vendored JS.** Cytoscape.js bundled at a pinned version with a SHA256 in
  `static/js/cytoscape.version.txt`. Never re-fetched silently.
- **Compose, don't own.** Interspace doesn't replace your storage, query
  layer, or note-taking. It adds the visualization leg.
- **Minimal bundled adapters.** The `interspace/adapters/` package ships
  one generic adapter (`filesystem_tree`) that works on any directory.
  Adapters that read private or operator-specific schemas should live in
  your own repo or in `local/adapters/`, not in the framework.

## Status

**v0.1 — shipped.** Renderer, schema, validator, adapter contract +
archive-aware mode, hand-curated input pattern, search / tag / time filters
with shared cutoff across pages, zoom controls, mini-lattices. One synthetic
reference sample (`example.json`) committed.

**v0.2 — likely next:** `markdown_corpus` adapter (one node per `.md`
file, edges from link references, frontmatter → tags); finer time bucketing
on the slider (auto-adapt step to range); multi-tag composition toggle (AND
vs OR); search highlighting on the canvas; node detail pages showing
inbound/outbound *edge kinds* grouped semantically; per-dataset archive view
(dedicated filtered page).

## License

MIT
