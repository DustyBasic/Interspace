"""Hub generator: scan a directory of rendered Interspace outputs and emit
an index page linking them all.

Each render directory must contain a `_meta.json` written by the renderer.
Optionally, the base directory may contain a `_hub.json` to override the
hub title, description, and grouping of datasets.

Usage:
    python -m interspace hub <base_dir>

Produces `<base_dir>/index.html` linking to each subdir's `index.html`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent
_TEMPLATES_DIR = _PROJECT_DIR / "templates"
_STATIC_DIR = _PROJECT_DIR / "static"


def build_hub(base_dir: Path) -> int:
    """Scan `base_dir` for renders and emit `base_dir/index.html` indexing them.

    Returns 0 on success, 2 if no renders found.
    """
    if not base_dir.exists() or not base_dir.is_dir():
        print(f"error: base directory not found: {base_dir}", file=sys.stderr)
        return 2

    datasets = _discover_datasets(base_dir)
    if not datasets:
        print(
            f"error: no renders found under {base_dir} "
            "(each subdir must contain a _meta.json)",
            file=sys.stderr,
        )
        return 2

    hub_config = _load_hub_config(base_dir)
    hub_title = hub_config.get("title", "Interspace")
    hub_description = hub_config.get("description")
    groups = _organize_into_groups(datasets, hub_config)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("hub.html").render(
        asset_prefix="",
        hub_title=hub_title,
        hub_description=hub_description,
        groups=groups,
        dataset_count=len(datasets),
    )
    (base_dir / "index.html").write_text(html, encoding="utf-8")

    _copy_static_assets(base_dir / "static")

    print(
        f"hub: {len(datasets)} dataset(s) indexed -> {base_dir / 'index.html'}",
        file=sys.stderr,
    )
    return 0


def _discover_datasets(base_dir: Path) -> list[dict[str, Any]]:
    out = []
    for sub in sorted(base_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue
        meta_path = sub / "_meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        meta["slug"] = sub.name
        out.append(meta)
    return out


def _load_hub_config(base_dir: Path) -> dict[str, Any]:
    """Read optional `_hub.json` for overrides (title, description, groups).

    Schema:
        {
            "title": "...",
            "description": "...",
            "groups": [
                {"label": "...", "description": "...", "datasets": ["slug", ...]},
                ...
            ]
        }
    Any datasets not listed in groups land in a trailing "Other" group.
    """
    config_path = base_dir / "_hub.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _organize_into_groups(
    datasets: list[dict[str, Any]], hub_config: dict[str, Any]
) -> list[dict[str, Any]]:
    by_slug = {d["slug"]: d for d in datasets}
    used: set[str] = set()
    out: list[dict[str, Any]] = []

    for group in hub_config.get("groups", []):
        group_datasets = []
        for slug in group.get("datasets", []):
            if slug in by_slug and slug not in used:
                group_datasets.append(by_slug[slug])
                used.add(slug)
        if group_datasets:
            out.append(
                {
                    "label": group.get("label", ""),
                    "description": group.get("description"),
                    "datasets": group_datasets,
                }
            )

    leftover = [d for d in datasets if d["slug"] not in used]
    if leftover and out:
        out.append({"label": "Other", "description": None, "datasets": leftover})
    elif leftover:
        out.append({"label": "", "description": None, "datasets": leftover})

    return out


def _copy_static_assets(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(_STATIC_DIR, target)
