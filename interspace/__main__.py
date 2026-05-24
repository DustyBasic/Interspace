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
