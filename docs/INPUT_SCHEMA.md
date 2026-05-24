# Interspace Input Schema (v0.1)

The Interspace renderer reads a single JSON file describing a graph of
**nodes** and **edges**, optionally grouped into **clusters**, and emits a
set of navigable static HTML pages. This document defines the schema input
files must conform to.

Adapters (one per data source — a SQLite DB, a markdown corpus, a filesystem
tree, etc.) are responsible for emitting this shape. Once an input validates
against this spec, the renderer guarantees it can produce a lattice from it.

## Top-level structure

```json
{
  "meta":     { ... },   // optional
  "nodes":    [ ... ],   // required, non-empty
  "edges":    [ ... ],   // required (may be empty)
  "clusters": [ ... ]    // optional
}
```

Unknown top-level keys are ignored. By convention, hand-edited inputs may
use a leading underscore (`_comment`, `_notes`) for human-only annotations.

## `meta` (optional object)

Presentation and provenance metadata. Every field is optional.

| Field         | Type   | Notes                                                       |
|---------------|--------|-------------------------------------------------------------|
| `title`       | string | Page title and `<h1>`. Default: input filename without extension. |
| `description` | string | Short blurb shown on the index page.                        |
| `epoch`       | string | ISO-8601 timestamp of when the source data was captured. Used by the v0.2 time slider. |
| `source`      | string | Adapter identifier with version, e.g. `"my_adapter@0.1"`.   |

## `nodes` (required, non-empty array)

Each entry is an object:

| Field                    | Type    | Req | Default          | Notes                                                |
|--------------------------|---------|-----|------------------|------------------------------------------------------|
| `id`                     | string  | Y   | —                | Unique, stable identifier. Referenced by edges.      |
| `label`                  | string  | N   | same as `id`     | Display name.                                        |
| `cluster`                | string  | N   | `"uncategorized"`| Must match a `clusters[].id` if `clusters` is given. |
| `tags`                   | array   | N   | `[]`             | List of strings. Used for filter chips / secondary color. |
| `weight`                 | number  | N   | `1.0`            | Render size multiplier. Must be `>= 0`.              |
| `meta`                   | object  | N   | `{}`             | Adapter-specific passthrough. Not interpreted by the renderer. |
| `archived`               | boolean | N   | `false`          | If true, node is rendered at lower opacity and tagged as archived. |
| `archived_at`            | string  | N   | —                | ISO-8601 date the node was archived. Shown on the node detail page. |
| `archived_from_cluster`  | string  | N   | —                | The cluster the node was last seen in before being archived. |

## `edges` (required array; may be empty)

Each entry is an object. Edges are directed (`source` → `target`). Self-loops
(`source == target`) are allowed.

| Field    | Type   | Req | Default     | Notes                                                |
|----------|--------|-----|-------------|------------------------------------------------------|
| `source` | string | Y   | —           | Must match a `nodes[].id`.                           |
| `target` | string | Y   | —           | Must match a `nodes[].id`.                           |
| `kind`   | string | N   | `"related"` | Semantic edge type. Free-form; renderer may color by `kind`. |
| `weight` | number | N   | `1.0`       | Render thickness multiplier. Must be `>= 0`.         |
| `meta`   | object | N   | `{}`        | Adapter-specific passthrough.                        |

## `clusters` (optional array)

If omitted, every node is grouped under an implicit `"uncategorized"` cluster.
If present, every `nodes[].cluster` reference must match a `clusters[].id`
(or be absent, defaulting to `"uncategorized"` — which the renderer auto-adds
when needed).

| Field   | Type   | Req | Default       | Notes                                                |
|---------|--------|-----|---------------|------------------------------------------------------|
| `id`    | string | Y   | —             | Unique, stable identifier. Referenced by `nodes[].cluster`. |
| `label` | string | N   | same as `id`  | Display name.                                        |
| `color` | string | N   | from palette  | CSS color (`"#aabbcc"`, `"steelblue"`, `"rgb(...)"`). |
| `meta`  | object | N   | `{}`          | Adapter-specific passthrough.                        |

## Validation

The renderer collects **all** errors before exiting; it does not stop at the
first failure. On any error, the renderer prints `error: N schema validation
issue(s)...` followed by one bullet per problem and exits with code `3`.

| Rule                                                                | Example error                                                       |
|---------------------------------------------------------------------|---------------------------------------------------------------------|
| Top-level is a JSON object                                          | `top-level must be a JSON object`                                   |
| `nodes` present, is an array, non-empty                             | `'nodes' must be non-empty`                                         |
| `edges` present, is an array                                        | `missing required field 'edges'`                                    |
| Every node has a non-empty string `id`                              | `nodes[3].id is required and must be a non-empty string`            |
| Node ids are unique                                                 | `nodes[5].id 'phi' is duplicated`                                   |
| `node.weight` is a non-negative number when present                 | `nodes[2].weight must be a non-negative number`                     |
| `node.tags` is an array of strings when present                     | `nodes[2].tags must be an array of strings`                         |
| Every edge has `source` and `target` strings                        | `edges[1].source is required and must be a non-empty string`        |
| Edge `source`/`target` references resolve to a node id              | `edges[4].target 'ghost' does not match any node.id`                |
| `edge.weight` is a non-negative number when present                 | `edges[0].weight must be a non-negative number`                     |
| `clusters` (if present) is an array of objects with unique string ids | `clusters[2].id 'geometric' is duplicated`                        |
| `node.cluster` resolves to a cluster id (when `clusters` is present) | `nodes[3].cluster 'mystery' does not match any cluster.id`         |

Exit codes from the `render` command:

| Code | Meaning                                                    |
|------|------------------------------------------------------------|
| `0`  | Render succeeded.                                          |
| `2`  | Input file missing, unreadable, or not valid JSON.         |
| `3`  | Input parsed as JSON but failed schema validation.         |

## Defaults & normalization

After validation, the renderer applies defaults before passing to templates:

- `node.label` ← `node.id` when missing.
- `node.cluster` ← `"uncategorized"` when missing.
- `node.tags` ← `[]` when missing.
- `node.weight` ← `1.0` when missing.
- `edge.kind` ← `"related"` when missing.
- `edge.weight` ← `1.0` when missing.
- `cluster.label` ← `cluster.id` when missing.
- An `"uncategorized"` cluster is auto-added if any node falls back to it
  and no explicit one was declared.

## Minimal example

See [`samples/example.json`](../samples/example.json) for a working input that
exercises every field group. Adapter authors should treat that file as the
reference for the shape they need to emit.
