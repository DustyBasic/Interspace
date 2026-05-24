"""CLI entry point for Interspace.

Usage:
    python -m interspace render <input.json> --output <dir>
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

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
