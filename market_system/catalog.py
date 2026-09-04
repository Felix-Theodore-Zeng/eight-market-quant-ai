from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else ROOT / "config" / "source_catalog.json"
    return json.loads(target.read_text(encoding="utf-8"))


def entries(catalog: dict[str, Any]):
    for market, items in catalog["markets"].items():
        for item in items:
            yield market, item


def physical_entries(catalog: dict[str, Any]):
    for market, item in entries(catalog):
        if "ref" not in item and item.get("kind") != "derived":
            yield market, item
