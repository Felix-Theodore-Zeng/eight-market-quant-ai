from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Any

import numpy as np

from . import METHOD_VERSION


WINDOWS = {"1w": 5, "20d": 20, "1m": 21, "60d": 60, "3m": 63, "1y": 252}


def load_series(connection, series_id: str, as_of_date: str | None = None, limit: int = 800) -> list[dict[str, Any]]:
    condition = "AND observed_date<=?" if as_of_date else ""
    params = [series_id, as_of_date, limit] if as_of_date else [series_id, limit]
    rows = connection.execute(f"""
      SELECT observed_date, open, high, low, close, value, volume, source, quality_status
      FROM canonical_observations WHERE series_id=? AND COALESCE(close,value) IS NOT NULL {condition}
      ORDER BY observed_date DESC LIMIT ?
    """, params).fetchall()
    return [dict(zip(("date","open","high","low","close","value","volume","source","quality"), row))
            for row in reversed(rows)]


def _values(rows: list[dict[str, Any]]) -> np.ndarray:
    return np.array([float(row["close"] if row["close"] is not None else row["value"]) for row in rows], dtype=float)


def feature_for_window(rows: list[dict[str, Any]], count: int) -> dict[str, Any] | None:
    window = rows[-count:]
    if len(window) < 2:
        return None
    values = _values(window)
    dates = [row["date"] for row in window]
    returns = np.diff(np.log(np.where(values > 0, values, np.nan)))
    returns = returns[np.isfinite(returns)]
    x = np.arange(len(values), dtype=float)
    positive = values > 0
    slope = r2 = None
    max_drawdown = None
    if positive.all() and len(values) >= 3:
        running_max = np.maximum.accumulate(values)
        max_drawdown = float(np.min(values / running_max - 1.0))
        y = np.log(values)
        coeff = np.polyfit(x, y, 1)
        fitted = np.polyval(coeff, x)
        ss_res = float(np.sum((y - fitted) ** 2))
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        slope = float(math.exp(coeff[0] * 252) - 1)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    current = float(values[-1])
    return {
        "observations": len(values), "current_value": current,
        "period_return": float(current / values[0] - 1) if values[0] else None,
        "high_value": float(np.max(values)), "high_date": dates[int(np.argmax(values))],
        "low_value": float(np.min(values)), "low_date": dates[int(np.argmin(values))],
        "realized_volatility": float(np.std(returns, ddof=1) * math.sqrt(252)) if len(returns) >= 2 else None,
        "empirical_percentile": float(np.mean(values <= current)),
        # MDD and log-price slope are price-level metrics. They are deliberately
        # null for signed/zero-valued flows, spreads and rates.
        "max_drawdown": max_drawdown,
        "log_slope_annualized": slope, "slope_r2": r2,
    }


def compute_statistics(connection, as_of_date: str) -> int:
    series_ids = [row[0] for row in connection.execute("SELECT DISTINCT series_id FROM canonical_observations").fetchall()]
    now = datetime.now(timezone.utc)
    written = 0
    for series_id in series_ids:
        rows = load_series(connection, series_id, as_of_date, 800)
        for window_id, count in WINDOWS.items():
            result = feature_for_window(rows, count)
            if not result:
                continue
            connection.execute("""INSERT OR REPLACE INTO statistical_features VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [as_of_date, series_id, window_id,
              result["observations"], result["current_value"], result["period_return"],
              result["high_value"], result["high_date"], result["low_value"], result["low_date"],
              result["realized_volatility"], result["empirical_percentile"], result["max_drawdown"],
              result["log_slope_annualized"], result["slope_r2"], METHOD_VERSION, now])
            written += 1
    return written
