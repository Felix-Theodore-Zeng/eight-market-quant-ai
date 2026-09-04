from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd


def connect(path: str | Path) -> duckdb.DuckDBPyConnection:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(target))
    initialize(connection)
    return connection


def initialize(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""
    CREATE SEQUENCE IF NOT EXISTS bootstrap_run_seq START 1;
    CREATE TABLE IF NOT EXISTS series_registry (
      series_id VARCHAR PRIMARY KEY, market VARCHAR NOT NULL, indicator_key VARCHAR NOT NULL,
      label VARCHAR NOT NULL, kind VARCHAR NOT NULL, source VARCHAR, symbol VARCHAR,
      unit VARCHAR, cadence VARCHAR, is_reference BOOLEAN DEFAULT FALSE,
      definition VARCHAR, catalog_json JSON, updated_at_utc TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS market_observations (
      series_id VARCHAR NOT NULL, observed_date DATE NOT NULL, observed_at_utc TIMESTAMPTZ,
      available_at_utc TIMESTAMPTZ NOT NULL, open DOUBLE, high DOUBLE, low DOUBLE,
      close DOUBLE, value DOUBLE, volume DOUBLE, source VARCHAR NOT NULL,
      source_priority INTEGER NOT NULL DEFAULT 100, quality_status VARCHAR NOT NULL,
      method_version VARCHAR NOT NULL, metadata_json JSON,
      ingested_at_utc TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(series_id, observed_date, source, method_version)
    );
    CREATE TABLE IF NOT EXISTS bootstrap_runs (
      run_id BIGINT PRIMARY KEY DEFAULT nextval('bootstrap_run_seq'),
      started_at_utc TIMESTAMPTZ NOT NULL, completed_at_utc TIMESTAMPTZ,
      mode VARCHAR NOT NULL, status VARCHAR NOT NULL, requested_start DATE,
      requested_end DATE, success_count INTEGER DEFAULT 0, partial_count INTEGER DEFAULT 0,
      failed_count INTEGER DEFAULT 0, details_json JSON
    );
    CREATE TABLE IF NOT EXISTS source_checkpoints (
      source VARCHAR NOT NULL, series_id VARCHAR NOT NULL, last_observed_date DATE,
      last_success_at_utc TIMESTAMPTZ, status VARCHAR NOT NULL, detail VARCHAR,
      PRIMARY KEY(source, series_id)
    );
    CREATE TABLE IF NOT EXISTS statistical_features (
      as_of_date DATE NOT NULL, series_id VARCHAR NOT NULL, window_id VARCHAR NOT NULL,
      observations INTEGER NOT NULL, current_value DOUBLE, period_return DOUBLE,
      high_value DOUBLE, high_date DATE, low_value DOUBLE, low_date DATE,
      realized_volatility DOUBLE, empirical_percentile DOUBLE, max_drawdown DOUBLE,
      log_slope_annualized DOUBLE, slope_r2 DOUBLE, method_version VARCHAR NOT NULL,
      computed_at_utc TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(as_of_date, series_id, window_id, method_version)
    );
    CREATE TABLE IF NOT EXISTS technical_levels (
      as_of_date DATE NOT NULL, series_id VARCHAR NOT NULL, level_type VARCHAR NOT NULL,
      rank INTEGER NOT NULL, price DOUBLE NOT NULL, touch_count INTEGER NOT NULL,
      last_touch_date DATE, distance_percent DOUBLE, strength DOUBLE,
      method_version VARCHAR NOT NULL, details_json JSON,
      PRIMARY KEY(as_of_date, series_id, level_type, rank, method_version)
    );
    CREATE TABLE IF NOT EXISTS wave_assessments (
      as_of_date DATE NOT NULL, series_id VARCHAR NOT NULL, phase VARCHAR NOT NULL,
      direction VARCHAR NOT NULL, confidence DOUBLE NOT NULL, invalidation_price DOUBLE,
      pivot_count INTEGER NOT NULL, pivots_json JSON, evidence_json JSON,
      method_version VARCHAR NOT NULL,
      PRIMARY KEY(as_of_date, series_id, method_version)
    );
    CREATE TABLE IF NOT EXISTS rule7_targets (
      as_of_date DATE NOT NULL, series_id VARCHAR NOT NULL, direction VARCHAR NOT NULL,
      anchor_low DOUBLE, anchor_high DOUBLE, target_1 DOUBLE, target_2 DOUBLE,
      target_3 DOUBLE, invalidation_price DOUBLE, method_version VARCHAR NOT NULL,
      PRIMARY KEY(as_of_date, series_id, method_version)
    );
    CREATE TABLE IF NOT EXISTS fibonacci_assessments (
      as_of_date DATE NOT NULL, series_id VARCHAR NOT NULL, direction VARCHAR NOT NULL,
      swing_start_date DATE NOT NULL, swing_start_price DOUBLE NOT NULL,
      swing_end_date DATE NOT NULL, swing_end_price DOUBLE NOT NULL,
      current_price DOUBLE NOT NULL, retracements_json JSON NOT NULL,
      extensions_json JSON NOT NULL, nearest_below DOUBLE, nearest_above DOUBLE,
      method_version VARCHAR NOT NULL,
      PRIMARY KEY(as_of_date, series_id, method_version)
    );
    CREATE TABLE IF NOT EXISTS analysis_packages (
      as_of_date DATE NOT NULL, market VARCHAR NOT NULL, schema_version INTEGER NOT NULL,
      byte_length INTEGER NOT NULL, package_json JSON NOT NULL, created_at_utc TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(as_of_date, market, schema_version)
    );
    CREATE TABLE IF NOT EXISTS daily_analysis_runs (
      analysis_date DATE PRIMARY KEY, started_at_utc TIMESTAMPTZ NOT NULL,
      completed_at_utc TIMESTAMPTZ, status VARCHAR NOT NULL, failure_reason VARCHAR,
      missing_data_json JSON, market_results_dir VARCHAR, cross_result_path VARCHAR,
      report_path VARCHAR, delivery_json JSON
    );
    CREATE TABLE IF NOT EXISTS daily_runtime_metrics (
      analysis_date DATE PRIMARY KEY, status VARCHAR NOT NULL,
      refresh_seconds DOUBLE, compute_seconds DOUBLE, package_seconds DOUBLE,
      ai_seconds DOUBLE, validation_seconds DOUBLE, ledger_seconds DOUBLE,
      delivery_document_seconds DOUBLE, delivery_summary_seconds DOUBLE,
      total_seconds DOUBLE, detail_json JSON, recorded_at_utc TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS system_monitoring_runs (
      monitor_date DATE PRIMARY KEY, analysis_date DATE NOT NULL,
      status VARCHAR NOT NULL, payload_json JSON NOT NULL,
      recorded_at_utc TIMESTAMPTZ NOT NULL
    );
    CREATE TABLE IF NOT EXISTS prediction_ledger (
      analysis_date DATE NOT NULL, market VARCHAR NOT NULL, horizon VARCHAR NOT NULL,
      stage VARCHAR NOT NULL, forecast_direction VARCHAR NOT NULL, confidence DOUBLE NOT NULL,
      scenario VARCHAR, trigger_text VARCHAR, invalidation_text VARCHAR,
      benchmark_series VARCHAR NOT NULL, decision_value DOUBLE,
      realized_volatility_20d DOUBLE, realized_volatility_60d DOUBLE,
      empirical_percentile DOUBLE, max_drawdown DOUBLE,
      log_price_slope DOUBLE, slope_r2 DOUBLE,
      support_touch_count INTEGER, support_strength DOUBLE,
      resistance_touch_count INTEGER, resistance_strength DOUBLE,
      maturity_observation_number INTEGER NOT NULL, maturity_date DATE,
      maturity_value DOUBLE, actual_return DOUBLE, outcome_direction VARCHAR,
      direction_correct BOOLEAN, evaluated_at_utc TIMESTAMPTZ,
      source_result_json JSON, created_at_utc TIMESTAMPTZ NOT NULL,
      PRIMARY KEY(analysis_date, market, horizon)
    );
    CREATE TABLE IF NOT EXISTS monthly_calibration_runs (
      calibration_month VARCHAR PRIMARY KEY, created_at_utc TIMESTAMPTZ NOT NULL,
      status VARCHAR NOT NULL, matured_predictions INTEGER NOT NULL,
      direction_accuracy DOUBLE, mean_confidence DOUBLE, brier_score DOUBLE,
      by_market_json JSON, five_factor_diagnostics_json JSON,
      ai_recommendation_json JSON, package_path VARCHAR, report_path VARCHAR
    );
    CREATE INDEX IF NOT EXISTS observations_series_date_idx
      ON market_observations(series_id, observed_date);
    CREATE OR REPLACE VIEW canonical_observations AS
    SELECT * EXCLUDE(source_rank) FROM (
      SELECT *, ROW_NUMBER() OVER (
        PARTITION BY series_id, observed_date
        ORDER BY CASE quality_status WHEN 'ok' THEN 0 WHEN 'partial' THEN 1 ELSE 2 END,
                 source_priority, available_at_utc DESC
      ) source_rank
      FROM market_observations
      WHERE quality_status IN ('ok','partial')
    ) WHERE source_rank=1;
    """)


def sync_catalog(connection: duckdb.DuckDBPyConnection, catalog: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    for market, items in catalog["markets"].items():
        for item in items:
            series_id = f"{market}.{item['key']}"
            connection.execute("""
              INSERT OR REPLACE INTO series_registry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [series_id, market, item["key"], item["label"], item.get("kind", "observation"),
                  item.get("primary"), item.get("symbol"), item.get("unit"), item.get("cadence", "daily"),
                  "ref" in item, item.get("definition"), json.dumps(item, ensure_ascii=False), now])


def upsert_observations(connection: duckdb.DuckDBPyConnection, rows: Iterable[dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc)
    values = []
    for row in rows:
        close = row.get("close")
        value = row.get("value", close)
        values.append({"series_id": row["series_id"], "observed_date": row["observed_date"],
              "observed_at_utc": row.get("observed_at_utc"),
              "available_at_utc": row.get("available_at_utc") or f"{row['observed_date']}T23:59:59+00:00",
              "open": row.get("open"), "high": row.get("high"), "low": row.get("low"), "close": close,
              "value": value, "volume": row.get("volume"), "source": row["source"],
              "source_priority": row.get("source_priority", 100),
              "quality_status": row.get("quality_status", "ok"),
              "method_version": row.get("method_version", "raw-v1"),
              "metadata_json": json.dumps(row.get("metadata", {}), ensure_ascii=False),
              "ingested_at_utc": now.isoformat()})
    if not values:
        return 0
    frame = pd.DataFrame.from_records(values)
    connection.register("_observation_batch", frame)
    try:
        connection.execute("""INSERT OR REPLACE INTO market_observations
          SELECT series_id, CAST(observed_date AS DATE), CAST(observed_at_utc AS TIMESTAMPTZ),
                 CAST(available_at_utc AS TIMESTAMPTZ), open, high, low, close, value, volume,
                 source, source_priority, quality_status, method_version,
                 CAST(metadata_json AS JSON), CAST(ingested_at_utc AS TIMESTAMPTZ)
          FROM _observation_batch""")
    finally:
        connection.unregister("_observation_batch")
    return len(values)
