from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np

from . import METHOD_VERSION
from .analytics import load_series


def _price(row: dict[str, Any]) -> float:
    return float(row["close"] if row["close"] is not None else row["value"])


def _atr(rows: list[dict[str, Any]], period: int = 14) -> float:
    trs = []
    for index, row in enumerate(rows[-(period + 1):]):
        close = _price(row)
        high = float(row["high"] if row["high"] is not None else close)
        low = float(row["low"] if row["low"] is not None else close)
        previous = _price(rows[-(period + 1) + index - 1]) if index else close
        trs.append(max(high - low, abs(high - previous), abs(low - previous)))
    return float(np.mean(trs)) if trs else 0.0


def support_resistance(rows: list[dict[str, Any]], radius: int = 3, max_each: int = 4) -> dict[str, list[dict[str, Any]]]:
    if len(rows) < radius * 2 + 5:
        return {"support": [], "resistance": []}
    current = _price(rows[-1])
    atr = _atr(rows)
    tolerance = max(atr * 0.75, abs(current) * 0.006)
    pivots = []
    for index in range(radius, len(rows) - radius):
        center = _price(rows[index])
        surrounding = [_price(row) for row in rows[index-radius:index+radius+1]]
        if center == min(surrounding):
            pivots.append(("support", center, rows[index]["date"]))
        if center == max(surrounding):
            pivots.append(("resistance", center, rows[index]["date"]))
    output = {"support": [], "resistance": []}
    for level_type in output:
        clusters: list[list[tuple[str, float, Any]]] = []
        for pivot in [item for item in pivots if item[0] == level_type]:
            target = next((cluster for cluster in clusters if abs(np.mean([x[1] for x in cluster]) - pivot[1]) <= tolerance), None)
            if target is None:
                clusters.append([pivot])
            else:
                target.append(pivot)
        levels = []
        for cluster in clusters:
            price = float(np.mean([x[1] for x in cluster]))
            if level_type == "support" and price > current + tolerance:
                continue
            if level_type == "resistance" and price < current - tolerance:
                continue
            touches = sum(abs(_price(row) - price) <= tolerance for row in rows)
            last_touch = max((row["date"] for row in rows if abs(_price(row) - price) <= tolerance), default=None)
            recency = next((i for i, row in enumerate(reversed(rows)) if abs(_price(row) - price) <= tolerance), len(rows))
            strength = min(100.0, touches * 12.0 + max(0.0, 30.0 * (1 - recency / max(len(rows), 1))))
            levels.append({"price": price, "touch_count": touches, "last_touch_date": last_touch,
                           "distance_percent": price / current - 1 if current else None, "strength": strength})
        levels.sort(key=lambda x: (-x["strength"], abs(x["distance_percent"] or 0)))
        output[level_type] = levels[:max_each]
    return output


def zigzag(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rows) < 20:
        return []
    threshold = max((_atr(rows) / abs(_price(rows[-1]))) * 1.5 if _price(rows[-1]) else 0.0, 0.02)
    pivots = [{"date": rows[0]["date"], "price": _price(rows[0]), "type": "anchor", "confirmed": False}]
    direction = 0
    extreme_index = 0
    for index in range(1, len(rows)):
        price = _price(rows[index])
        extreme = _price(rows[extreme_index])
        change = price / extreme - 1 if extreme else 0
        if direction == 0:
            anchor = _price(rows[0])
            initial_change = price / anchor - 1 if anchor else 0
            if initial_change >= threshold:
                direction = 1
                extreme_index = index
            elif initial_change <= -threshold:
                direction = -1
                extreme_index = index
            continue
        if direction > 0:
            if price >= extreme:
                extreme_index = index
            elif change <= -threshold:
                pivots.append({"date": rows[extreme_index]["date"], "price": extreme, "type": "high", "confirmed": True})
                direction = -1; extreme_index = index
        elif direction < 0:
            extreme = _price(rows[extreme_index]); change = price / extreme - 1 if extreme else 0
            if price <= extreme:
                extreme_index = index
            elif change >= threshold:
                pivots.append({"date": rows[extreme_index]["date"], "price": extreme, "type": "low", "confirmed": True})
                direction = 1; extreme_index = index
    pivots.append({"date": rows[extreme_index]["date"], "price": _price(rows[extreme_index]),
                   "type": "high" if direction >= 0 else "low", "confirmed": False})
    return pivots[-9:]


def wave_assessment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pivots = zigzag(rows)
    if len(pivots) < 4:
        return {"phase": "evidence_insufficient", "direction": "unknown", "confidence": 0.0,
                "invalidation_price": None, "pivots": pivots,
                "evidence": ["mechanical_zigzag", "fewer_than_4_confirmed_swings"]}
    last = pivots[-1]
    direction = "up" if last["type"] == "high" else "down"
    sequence = pivots[-6:]
    alternating = all(sequence[i]["type"] != sequence[i-1]["type"] for i in range(1, len(sequence)))
    if len(sequence) >= 6 and alternating:
        phase = "impulse_candidate"
    elif len(sequence) >= 4 and alternating:
        phase = "correction_or_range"
    else:
        phase = "unresolved"
    confidence = min(0.8, 0.25 + 0.08 * len(sequence)) if alternating else 0.25
    opposite = [p["price"] for p in reversed(pivots[:-1]) if p["type"] != last["type"]]
    return {"phase": phase, "direction": direction, "confidence": confidence,
            "invalidation_price": opposite[0] if opposite else None, "pivots": pivots,
            "evidence": ["mechanical_zigzag", "not_discretionary_elliott_label"]}


def rule7(rows: list[dict[str, Any]], levels: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    if len(rows) < 20:
        return None
    current = _price(rows[-1])
    x = np.arange(min(63, len(rows)), dtype=float)
    values = np.array([_price(row) for row in rows[-len(x):]], dtype=float)
    direction = "up" if np.polyfit(x, values, 1)[0] >= 0 else "down"
    low = min((x["price"] for x in levels["support"]), default=float(np.min(values)))
    high = max((x["price"] for x in levels["resistance"]), default=float(np.max(values)))
    width = high - low
    if width <= 0:
        return None
    multipliers = (1.75, 2.33, 3.5)
    targets = [low + width * m for m in multipliers] if direction == "up" else [high - width * m for m in multipliers]
    invalidation = low if direction == "up" else high
    return {"direction": direction, "anchor_low": low, "anchor_high": high,
            "targets": targets, "invalidation_price": invalidation}


def fibonacci_assessment(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Calculate retracement and extension levels from the latest completed swing."""
    confirmed = [pivot for pivot in zigzag(rows)
                 if pivot.get("confirmed") and pivot.get("type") in {"high", "low"}]
    pair = next((confirmed[index - 1:index + 1] for index in range(len(confirmed) - 1, 0, -1)
                 if confirmed[index - 1]["type"] != confirmed[index]["type"]), None)
    if not pair:
        return None
    start, end = pair
    width = abs(float(end["price"]) - float(start["price"]))
    if width <= 0:
        return None
    direction = "up" if end["type"] == "high" else "down"
    retracement_ratios = (0.382, 0.5, 0.618, 0.786)
    extension_ratios = (1.272, 1.618, 2.0, 2.618)
    if direction == "up":
        retracements = {str(ratio): float(end["price"]) - width * ratio for ratio in retracement_ratios}
        extensions = {str(ratio): float(start["price"]) + width * ratio for ratio in extension_ratios}
    else:
        retracements = {str(ratio): float(end["price"]) + width * ratio for ratio in retracement_ratios}
        extensions = {str(ratio): float(start["price"]) - width * ratio for ratio in extension_ratios}
    current = _price(rows[-1])
    candidates = sorted({float(start["price"]), float(end["price"]), *retracements.values(), *extensions.values()})
    below = [price for price in candidates if price <= current]
    above = [price for price in candidates if price >= current]
    return {
        "direction": direction,
        "swing_start": {"date": start["date"], "price": float(start["price"]), "type": start["type"]},
        "swing_end": {"date": end["date"], "price": float(end["price"]), "type": end["type"]},
        "current_price": current,
        "retracements": retracements,
        "extensions": extensions,
        "nearest_below": max(below) if below else None,
        "nearest_above": min(above) if above else None,
        "evidence": "latest_completed_mechanical_zigzag_swing",
    }


def compute_technicals(connection, as_of_date: str) -> dict[str, int]:
    price_series = [row[0] for row in connection.execute("""
      SELECT DISTINCT o.series_id FROM canonical_observations o
      JOIN series_registry r USING(series_id) WHERE r.kind IN ('ohlcv','derived')
    """).fetchall()]
    level_count = wave_count = rule_count = fibonacci_count = 0
    for series_id in price_series:
        rows = load_series(connection, series_id, as_of_date, 600)
        if len(rows) < 10:
            continue
        levels = support_resistance(rows)
        for level_type, items in levels.items():
            for rank, item in enumerate(items, 1):
                connection.execute("""INSERT OR REPLACE INTO technical_levels VALUES
                  (?,?,?,?,?,?,?,?,?,?,?)""", [as_of_date, series_id, level_type, rank, item["price"],
                  item["touch_count"], item["last_touch_date"], item["distance_percent"], item["strength"],
                  METHOD_VERSION, json.dumps({"tolerance_method": "max(0.75*ATR14,0.6% price)"})])
                level_count += 1
        wave = wave_assessment(rows)
        connection.execute("""INSERT OR REPLACE INTO wave_assessments VALUES
          (?,?,?,?,?,?,?,?,?,?)""", [as_of_date, series_id, wave["phase"], wave["direction"], wave["confidence"],
          wave["invalidation_price"], len(wave["pivots"]), json.dumps(wave["pivots"], default=str),
          json.dumps(wave["evidence"]), METHOD_VERSION])
        wave_count += 1
        r7 = rule7(rows, levels)
        if r7:
            connection.execute("""INSERT OR REPLACE INTO rule7_targets VALUES
              (?,?,?,?,?,?,?,?,?,?)""", [as_of_date, series_id, r7["direction"], r7["anchor_low"],
              r7["anchor_high"], *r7["targets"], r7["invalidation_price"], METHOD_VERSION])
            rule_count += 1
        fibonacci = fibonacci_assessment(rows)
        if fibonacci:
            connection.execute("""INSERT OR REPLACE INTO fibonacci_assessments VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?)""", [as_of_date, series_id, fibonacci["direction"],
              fibonacci["swing_start"]["date"], fibonacci["swing_start"]["price"],
              fibonacci["swing_end"]["date"], fibonacci["swing_end"]["price"],
              fibonacci["current_price"], json.dumps(fibonacci["retracements"]),
              json.dumps(fibonacci["extensions"]), fibonacci["nearest_below"],
              fibonacci["nearest_above"], METHOD_VERSION])
            fibonacci_count += 1
    return {"levels": level_count, "waves": wave_count, "rule7": rule_count,
            "fibonacci": fibonacci_count}
