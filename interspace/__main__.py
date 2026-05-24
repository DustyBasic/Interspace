"""CLI entry point for Interspace.

Usage:
    python -m interspace render <input.json> --output <dir>
    python -m interspace serve <rendered_dir> [--port N] [--no-open]
    python -m interspace --version
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="interspace",
        description="Render graph-shaped data as navigable static HTML pages.",
    )
    p.add_argument("--version", action="version", version=f"interspace {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="Render an input JSON to static HTML pages.")
    render.add_argument("input", type=Path, help="Path to Interspace JSON input file.")
    render.add_argument(
        "--output", "-o", type=Path, required=True, help="Output directory for rendered pages."
    )
    render.add_argument(
        "--title",
        type=str,
        default=None,
        help="Override the title used on the index page (default: derived from input filename).",
    )

    hub = sub.add_parser(
        "hub",
        help="Generate an index.html linking multiple rendered Interspace outputs.",
    )
    hub.add_argument(
        "base",
        type=Path,
        help="Base directory containing rendered subdirs (each with _meta.json).",
    )

    merge = sub.add_parser(
        "merge",
        help="Merge multiple Interspace JSON inputs into one combined payload.",
    )
    merge.add_argument(
        "config",
        type=Path,
        help="Path to a merge config JSON file (see interspace.merger).",
    )
    merge.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Output path for the merged Interspace JSON.",
    )
    merge.add_argument(
        "--patches",
        type=Path,
        default=None,
        help=(
            "Optional cross-source edge patch file (JSON list of edges with "
            "already-prefixed ids; endpoints validated against merged node set)."
        ),
    )

    serve = sub.add_parser(
        "serve",
        help="Start a local HTTP server for a rendered Interspace directory.",
    )
    serve.add_argument(
        "directory", type=Path, help="Path to a rendered output directory."
    )
    serve.add_argument(
        "--port", "-p", type=int, default=8000, help="Port to bind (default 8000)."
    )
    serve.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind (default 127.0.0.1).",
    )
    serve.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Don't auto-launch the default browser.",
    )
    serve.set_defaults(open_browser=True)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "render":
        # Lazy import so --version / --help don't pay the cost.
        from .generator import render_pages

        return render_pages(
            input_path=args.input,
            output_dir=args.output,
            title=args.title,
        )

    if args.command == "hub":
        from .hub import build_hub

        return build_hub(args.base)

    if args.command == "merge":
        from .merger import merge_from_config, apply_patches

        try:
            payload = merge_from_config(args.config)
        except (ValueError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        patches_msg = ""
        if args.patches is not None:
            try:
                payload, applied, dropped = apply_patches(payload, args.patches)
            except (ValueError, FileNotFoundError) as e:
                print(f"error applying patches: {e}", file=sys.stderr)
                return 2
            patches_msg = f" + {applied} patch edges applied ({dropped} dropped)"

        args.output.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        args.output.write_text(
            _json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        phases = {n.get("phase", "current") for n in payload["nodes"]}
        print(
            f"merged {len(payload['nodes'])} nodes, "
            f"{len(payload['edges'])} edges, "
            f"{len(payload['clusters'])} clusters "
            f"(phases: {sorted(phases)}){patches_msg} -> {args.output}",
            file=sys.stderr,
        )
        return 0

    if args.command == "serve":
        from .server import run_server

        return run_server(
            directory=args.directory,
            port=args.port,
            host=args.host,
            open_browser=args.open_browser,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
