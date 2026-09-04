from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import load_catalog


ROOT = Path(__file__).resolve().parents[1]


def _round(value: Any, digits: int = 8):
    return round(float(value), digits) if value is not None else None


def _series_id(market: str, item: dict[str, Any]) -> str:
    return item.get("ref") or f"{market}.{item['key']}"


def _indicator_payload(connection, market: str, item: dict[str, Any], as_of_date: str, core: bool) -> dict[str, Any]:
    series_id = _series_id(market, item)
    latest = connection.execute("""
      SELECT observed_date, COALESCE(close,value), source, quality_status
      FROM canonical_observations WHERE series_id=? AND observed_date<=?
      ORDER BY observed_date DESC LIMIT 1
    """, [series_id, as_of_date]).fetchone()
    payload: dict[str, Any] = {"key": item["key"], "label": item["label"], "series_id": series_id,
                               "status": "ok" if latest else "unavailable"}
    if latest:
        payload["latest"] = {"date": latest[0].isoformat(), "value": _round(latest[1]),
                             "source": latest[2], "quality": latest[3]}
    features = connection.execute("""
      SELECT window_id, observations, period_return, high_value, high_date, low_value, low_date,
             realized_volatility, empirical_percentile, max_drawdown, log_slope_annualized, slope_r2
      FROM statistical_features WHERE series_id=? AND as_of_date=?
        AND window_id IN ('1w','20d','1m','60d','3m','1y')
    """, [series_id, as_of_date]).fetchall()
    all_statistics = {row[0]: {"n": row[1], "return": _round(row[2]),
      "high": _round(row[3]), "high_date": row[4].isoformat() if row[4] else None,
      "low": _round(row[5]), "low_date": row[6].isoformat() if row[6] else None,
      "realized_volatility": _round(row[7]), "percentile": _round(row[8]),
      "max_drawdown": _round(row[9]), "log_slope_annualized": _round(row[10]),
      "slope_r2": _round(row[11])} for row in features}
    # 20d/60d duplicate the month/quarter observation windows. Expose only their
    # calibration value and keep the four decision horizons fully described.
    payload["statistics"] = {key: all_statistics[key] for key in ("1w", "1m", "3m", "1y")
                             if key in all_statistics}
    payload["calibration"] = {
        "realized_volatility_20d": (all_statistics.get("20d") or {}).get("realized_volatility"),
        "realized_volatility_60d": (all_statistics.get("60d") or {}).get("realized_volatility"),
    }
    if core:
        levels = connection.execute("""SELECT level_type,rank,price,touch_count,last_touch_date,distance_percent,strength
          FROM technical_levels WHERE series_id=? AND as_of_date=? ORDER BY level_type,rank""",
          [series_id, as_of_date]).fetchall()
        wave = connection.execute("""SELECT phase,direction,confidence,invalidation_price,pivots_json,evidence_json
          FROM wave_assessments WHERE series_id=? AND as_of_date=?""", [series_id, as_of_date]).fetchone()
        rule = connection.execute("""SELECT direction,anchor_low,anchor_high,target_1,target_2,target_3,invalidation_price
          FROM rule7_targets WHERE series_id=? AND as_of_date=?""", [series_id, as_of_date]).fetchone()
        fibonacci = connection.execute("""SELECT direction,swing_start_date,swing_start_price,swing_end_date,
          swing_end_price,current_price,retracements_json,extensions_json,nearest_below,nearest_above
          FROM fibonacci_assessments WHERE series_id=? AND as_of_date=?""", [series_id, as_of_date]).fetchone()
        payload["technical"] = {
          "levels": [{"type": x[0], "rank": x[1], "price": _round(x[2]), "touches": x[3],
                      "last_touch": x[4].isoformat() if x[4] else None, "distance": _round(x[5]),
                      "strength": _round(x[6], 2)} for x in levels],
          "wave": ({"phase": wave[0], "direction": wave[1], "confidence": _round(wave[2], 3),
                    "invalidation": _round(wave[3]), "pivots": json.loads(wave[4]),
                    "evidence": json.loads(wave[5])} if wave else None),
          "rule_of_7": ({"direction": rule[0], "anchor_low": _round(rule[1]), "anchor_high": _round(rule[2]),
                         "targets": [_round(rule[3]), _round(rule[4]), _round(rule[5])],
                         "invalidation": _round(rule[6])} if rule else None),
          "fibonacci": ({"direction": fibonacci[0],
                          "swing_start": {"date": fibonacci[1].isoformat(), "price": _round(fibonacci[2])},
                          "swing_end": {"date": fibonacci[3].isoformat(), "price": _round(fibonacci[4])},
                          "retracements": {key: _round(value) for key, value in json.loads(fibonacci[6]).items()},
                          "extensions": {key: _round(value) for key, value in json.loads(fibonacci[7]).items()},
                          "nearest_below": _round(fibonacci[8]), "nearest_above": _round(fibonacci[9])}
                         if fibonacci else None),
        }
    return payload


def _fit_budget(package: dict[str, Any], max_bytes: int, core_keys: set[str]) -> bytes:
    def encoded() -> bytes:
        return json.dumps(package, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    result = encoded()
    if len(result) <= max_bytes:
        return result
    for indicator in package["indicators"]:
        technical = indicator.get("technical") or {}
        technical["levels"] = (technical.get("levels") or [])[:4]
        if technical.get("wave"):
            technical["wave"]["pivots"] = technical["wave"].get("pivots", [])[-5:]
            technical["wave"]["evidence"] = (technical["wave"].get("evidence") or {})
    result = encoded()
    if len(result) <= max_bytes:
        return result
    for indicator in package["indicators"]:
        if indicator["key"] not in core_keys:
            indicator["statistics"] = {}
            indicator.pop("calibration", None)
    result = encoded()
    if len(result) <= max_bytes:
        return result
    # Final deterministic compaction for markets with many core price series.
    for indicator in package["indicators"]:
        technical = indicator.get("technical") or {}
        technical["levels"] = (technical.get("levels") or [])[:2]
        if technical.get("wave"):
            technical["wave"]["pivots"] = technical["wave"].get("pivots", [])[-3:]
            technical["wave"].pop("evidence", None)
    result = encoded()
    if len(result) > max_bytes:
        raise RuntimeError(f"market package exceeds hard budget after deterministic compaction: {len(result)} > {max_bytes}")
    return result


def build_packages(connection, as_of_date: str, output_root: str | Path | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    policy = json.loads((ROOT / "config" / "package_policy.json").read_text(encoding="utf-8"))
    output = Path(output_root) if output_root else ROOT / "output" / "market-packages" / as_of_date
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "as_of_date": as_of_date,
                "created_at_utc": datetime.now(timezone.utc).isoformat(), "markets": []}
    for market, items in catalog["markets"].items():
        core = set(policy["core_series"][market])
        package = {"schema_version": 1, "market": market, "as_of_date": as_of_date,
                   "decision_time": catalog["decision_time"], "indicator_count": len(items),
                   "methodology": {"statistics": "point-in-time deterministic",
                     "support_resistance": "mechanical pivots clustered by ATR/price tolerance",
                     "wave": "mechanical swing structure; candidate labels only",
                     "rule_of_7": [1.75, 2.33, 3.5],
                     "fibonacci": {"retracement": [0.382, 0.5, 0.618, 0.786],
                                    "extension": [1.272, 1.618, 2.0, 2.618],
                                    "anchor": "latest completed mechanical zigzag swing"}},
                   "indicators": [_indicator_payload(connection, market, item, as_of_date, item["key"] in core)
                                  for item in items]}
        package["data_quality"] = {"ok": sum(x["status"] == "ok" for x in package["indicators"]),
                                   "unavailable": sum(x["status"] != "ok" for x in package["indicators"])}
        body = _fit_budget(package, int(policy["max_bytes_per_market"]), core)
        path = output / f"{market}.json"
        path.write_bytes(body)
        connection.execute("INSERT OR REPLACE INTO analysis_packages VALUES (?,?,?,?,?,?)",
                           [as_of_date, market, 1, len(body), body.decode("utf-8"), datetime.now(timezone.utc)])
        manifest["markets"].append({"market": market, "path": str(path), "bytes": len(body),
                                    "ok": package["data_quality"]["ok"],
                                    "unavailable": package["data_quality"]["unavailable"]})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
