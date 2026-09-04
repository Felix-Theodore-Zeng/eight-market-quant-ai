from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect


BENCHMARKS = {
    "us": "sp500", "china": "csi300", "hong_kong": "hang_seng", "crypto": "btc",
    "energy": "brent", "precious_metals": "gold", "agriculture": "corn", "fx": "dxy",
}
HORIZON_STEPS = {"1d": 1, "1w": 5, "1m": 21, "3m": 63}


def record_analysis_run(db_path: str | Path, analysis_date: str, status: str, *, failure_reason: str | None = None,
                        missing_data: list[dict[str, Any]] | None = None, market_results_dir: str | None = None,
                        cross_result_path: str | None = None, report_path: str | None = None,
                        delivery: dict[str, Any] | None = None) -> dict[str, Any]:
    connection = connect(db_path); now = datetime.now(timezone.utc)
    existing = connection.execute("SELECT started_at_utc FROM daily_analysis_runs WHERE analysis_date=?", [analysis_date]).fetchone()
    started = existing[0] if existing else now
    connection.execute("INSERT OR REPLACE INTO daily_analysis_runs VALUES (?,?,?,?,?,?,?,?,?,?)", [
        analysis_date, started, now if status in {"complete", "failed"} else None, status, failure_reason,
        json.dumps(missing_data or [], ensure_ascii=False), market_results_dir, cross_result_path, report_path,
        json.dumps(delivery or {}, ensure_ascii=False)])
    connection.close()
    return {"ok": True, "analysis_date": analysis_date, "status": status}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _level_snapshot(indicator: dict[str, Any], level_type: str) -> tuple[int | None, float | None]:
    levels = [x for x in ((indicator.get("technical") or {}).get("levels") or []) if x.get("type") == level_type]
    if not levels:
        return None, None
    strongest = max(levels, key=lambda x: (x.get("strength") or 0, x.get("touches") or 0))
    return strongest.get("touches"), strongest.get("strength")


def record_predictions(db_path: str | Path, market_result_dir: str | Path, package_dir: str | Path) -> dict[str, Any]:
    connection = connect(db_path); now = datetime.now(timezone.utc); written = 0
    try:
        for market, benchmark_key in BENCHMARKS.items():
            result = _load(Path(market_result_dir) / f"{market}.json")
            package = _load(Path(package_dir) / f"{market}.json")
            indicator = next(x for x in package["indicators"] if x["key"] == benchmark_key)
            latest = indicator.get("latest") or {}; stats = indicator.get("statistics") or {}; calibration = indicator.get("calibration") or {}
            annual = stats.get("1y") or {}; support = _level_snapshot(indicator, "support"); resistance = _level_snapshot(indicator, "resistance")
            for horizon, steps in HORIZON_STEPS.items():
                forecast = result["horizons"][horizon]
                connection.execute("""INSERT OR REPLACE INTO prediction_ledger VALUES
                  (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
                    result["as_of_date"], market, horizon, result["stage"], forecast["direction"], forecast["confidence"],
                    forecast.get("scenario"), forecast.get("trigger"), forecast.get("invalidation"), indicator["series_id"],
                    latest.get("value"), calibration.get("realized_volatility_20d"), calibration.get("realized_volatility_60d"),
                    annual.get("percentile"), annual.get("max_drawdown"), annual.get("log_slope_annualized"), annual.get("slope_r2"),
                    support[0], support[1], resistance[0], resistance[1], steps, None, None, None, None, None, None,
                    json.dumps(forecast, ensure_ascii=False), now])
                written += 1
    finally:
        connection.close()
    return {"ok": True, "predictions_written": written}


def evaluate_matured_predictions(db_path: str | Path) -> dict[str, Any]:
    connection = connect(db_path); evaluated = 0; now = datetime.now(timezone.utc)
    try:
        pending = connection.execute("""SELECT analysis_date,market,horizon,forecast_direction,benchmark_series,
          decision_value,maturity_observation_number,realized_volatility_20d FROM prediction_ledger
          WHERE evaluated_at_utc IS NULL AND decision_value IS NOT NULL ORDER BY analysis_date""").fetchall()
        for analysis_date, market, horizon, forecast, series_id, decision_value, steps, rv20 in pending:
            maturity = connection.execute("""SELECT observed_date,COALESCE(close,value) FROM canonical_observations
              WHERE series_id=? AND observed_date>? ORDER BY observed_date LIMIT 1 OFFSET ?""",
              [series_id, analysis_date, steps - 1]).fetchone()
            if not maturity or not maturity[1]:
                continue
            actual_return = float(maturity[1]) / float(decision_value) - 1.0
            range_limit = 0.5 * float(rv20 or 0) * math.sqrt(steps / 252.0)
            outcome = "range" if abs(actual_return) <= range_limit else ("up" if actual_return > 0 else "down")
            correct = None if forecast == "uncertain" else forecast == outcome
            connection.execute("""UPDATE prediction_ledger SET maturity_date=?,maturity_value=?,actual_return=?,
              outcome_direction=?,direction_correct=?,evaluated_at_utc=? WHERE analysis_date=? AND market=? AND horizon=?""",
              [maturity[0], maturity[1], actual_return, outcome, correct, now, analysis_date, market, horizon])
            evaluated += 1
    finally:
        connection.close()
    return {"ok": True, "evaluated": evaluated}


def _factor_summary(rows: list[dict[str, Any]], fields: list[str]) -> dict[str, Any]:
    output = {}
    for field in fields:
        valid = [row for row in rows if row[field] is not None and row["correct"] is not None]
        correct = [row[field] for row in valid if row["correct"]]
        wrong = [row[field] for row in valid if not row["correct"]]
        output[field] = {"n": len(valid), "mean_when_correct": sum(correct) / len(correct) if correct else None,
                         "mean_when_wrong": sum(wrong) / len(wrong) if wrong else None}
    return output


def build_monthly_calibration(db_path: str | Path, month: str, output_path: str | Path) -> dict[str, Any]:
    connection = connect(db_path)
    columns = ["market", "horizon", "confidence", "correct", "rv20", "rv60", "percentile", "mdd", "slope", "r2",
               "support_touches", "support_strength", "resistance_touches", "resistance_strength"]
    raw = connection.execute("""SELECT market,horizon,confidence,direction_correct,realized_volatility_20d,
      realized_volatility_60d,empirical_percentile,max_drawdown,log_price_slope,slope_r2,support_touch_count,
      support_strength,resistance_touch_count,resistance_strength FROM prediction_ledger
      WHERE strftime(maturity_date,'%Y-%m')=? AND direction_correct IS NOT NULL""", [month]).fetchall()
    rows = [dict(zip(columns, row)) for row in raw]
    by_market = {}
    for market in sorted({row["market"] for row in rows}):
        subset = [row for row in rows if row["market"] == market]
        by_market[market] = {"n": len(subset), "accuracy": sum(bool(x["correct"]) for x in subset) / len(subset)}
    accuracy = sum(bool(row["correct"]) for row in rows) / len(rows) if rows else None
    mean_confidence = sum(row["confidence"] for row in rows) / len(rows) if rows else None
    brier = sum((row["confidence"] - float(row["correct"])) ** 2 for row in rows) / len(rows) if rows else None
    factors = _factor_summary(rows, ["rv20", "rv60", "percentile", "mdd", "slope", "r2",
                                      "support_touches", "support_strength", "resistance_touches", "resistance_strength"])
    payload = {"schema_version": 1, "calibration_month": month, "matured_predictions": len(rows),
               "direction_accuracy": accuracy, "mean_confidence": mean_confidence, "brier_score": brier,
               "by_market": by_market, "five_factor_diagnostics": {
                   "realized_volatility_20d_60d": {"rv20": factors["rv20"], "rv60": factors["rv60"]},
                   "empirical_percentile": factors["percentile"], "max_drawdown": factors["mdd"],
                   "ols_log_price_slope_r2": {"slope": factors["slope"], "r2": factors["r2"]},
                   "support_resistance_touches_strength": {k: factors[k] for k in ("support_touches", "support_strength", "resistance_touches", "resistance_strength")}},
               "ai_task": "判断是否需要调整分析设置；无充分重复证据时明确建议不调整。任何建议必须小步、可回滚。"}
    target = Path(output_path); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    connection.execute("INSERT OR REPLACE INTO monthly_calibration_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       [month, datetime.now(timezone.utc), "package_ready", len(rows), accuracy, mean_confidence, brier,
                        json.dumps(by_market), json.dumps(payload["five_factor_diagnostics"]), None, str(target), None])
    connection.close()
    return payload


def complete_monthly_calibration(db_path: str | Path, month: str, recommendation_path: str | Path,
                                 report_path: str | Path) -> dict[str, Any]:
    recommendation = _load(Path(recommendation_path))
    connection = connect(db_path)
    connection.execute("""UPDATE monthly_calibration_runs SET status='complete',ai_recommendation_json=?,report_path=?
      WHERE calibration_month=?""", [json.dumps(recommendation, ensure_ascii=False), str(report_path), month])
    connection.close()
    return {"ok": True, "calibration_month": month, "status": "complete"}
