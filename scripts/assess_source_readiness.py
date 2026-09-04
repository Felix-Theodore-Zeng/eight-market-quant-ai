#!/usr/bin/env python3
"""Produce a deterministic readiness report for the fixed 83-entry indicator catalog."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
READY = {"verified", "existing_production", "derived"}


def assess(catalog: dict) -> dict:
    results = []
    for market, indicators in catalog["markets"].items():
        for item in indicators:
            status = "reference" if "ref" in item else item.get("status", "missing_status")
            ready = status == "reference" or status in READY
            results.append({
                "market": market,
                "key": item["key"],
                "label": item["label"],
                "status": status,
                "ready": ready,
                "primary": item.get("primary"),
                "fallback": item.get("fallback"),
                "definition": item.get("definition"),
            })
    counts = Counter(row["status"] for row in results)
    unresolved = [row for row in results if not row["ready"]]
    return {
        "schema_version": 1,
        "catalog_id": catalog["catalog_id"],
        "entry_count": len(results),
        "ready_count": len(results) - len(unresolved),
        "unresolved_count": len(unresolved),
        "status_counts": dict(sorted(counts.items())),
        "unresolved": unresolved,
        "markets": {
            market: {
                "entries": sum(row["market"] == market for row in results),
                "ready": sum(row["market"] == market and row["ready"] for row in results),
            }
            for market in catalog["markets"]
        },
    }


def main() -> int:
    catalog = json.loads((ROOT / "config" / "source_catalog.json").read_text(encoding="utf-8"))
    print(json.dumps(assess(catalog), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
