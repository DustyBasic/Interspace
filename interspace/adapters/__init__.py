"""Adapters that read a particular data source and emit Interspace JSON.

Each adapter is a Python module that exposes:

  1. A function `to_interspace_json(source) -> dict` returning a payload that
     validates against `docs/INPUT_SCHEMA.md`.
  2. A CLI runnable as `python -m interspace.adapters.<name> <source>
     --output <json>`, with an optional `--archive <prev.json>` for
     persistent-catalog mode.

The bundled set is intentionally small. One generic adapter ships:

  - filesystem_tree -- walks any directory and emits density-aware nodes,
    collapsing versioned/timestamped/numbered file families into composite
    nodes while preserving unique standalone files individually.

Adapters that read private or operator-specific schemas should live in your
own repo or in `local/adapters/`, not in the framework. See
`docs/ADAPTERS.md` for the contract and a generic skeleton.

Pipeline:

    python -m interspace.adapters.<name> <source> -o input.json
    python -m interspace render input.json -o rendered/
"""
