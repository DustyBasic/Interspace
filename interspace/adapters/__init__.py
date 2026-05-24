"""Adapters that read a particular data source and emit Interspace JSON.

Each adapter is a Python module that exposes:

  1. A function `to_interspace_json(source) -> dict` returning a payload that
     validates against `docs/INPUT_SCHEMA.md`.
  2. A CLI runnable as `python -m interspace.adapters.<name> <source>
     --output <json>`, with an optional `--archive <prev.json>` for
     persistent-catalog mode.

This package ships empty. Drop your own adapter modules here (or anywhere on
the Python path); see `docs/ADAPTERS.md` for the contract and a generic
skeleton. Adapters never mutate their source data; the pipeline is:

    python -m interspace.adapters.<name> <source> -o input.json
    python -m interspace render input.json -o rendered/
"""
