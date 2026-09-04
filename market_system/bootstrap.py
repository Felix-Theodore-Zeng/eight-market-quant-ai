from __future__ import annotations

import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import collectors
from .catalog import entries, load_catalog
from .db import connect, sync_catalog, upsert_observations


ROOT = Path(__file__).resolve().parents[1]
CRYPTO_YAHOO = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD"}


def collect_item(market: str, item: dict[str, Any], delay: float) -> tuple[list[dict[str, Any]], str]:
    series_id = f"{market}.{item['key']}"
    if series_id == "china.a_share_turnover":
        return [], "derived_later"
    source, symbol = item.get("primary"), item.get("symbol")
    if item.get("kind") == "reference" or "ref" in item:
        return [], "reference"
    if item["key"] == "margin_balance":
        return collectors.eastmoney_margin_balance(series_id), "primary"
    if item["key"] == "spot_etf_net_flow":
        return collectors.bykaranteli_etf_flows(series_id), "primary"
    if source == "eia_bulk" and item["key"] == "opec_plus_output":
        return collectors.eia_opec_plus_output(series_id), "primary"
    if item.get("kind") == "derived" and source == "tradingview" and symbol:
        return collectors.tradingview_history(f"fx._input_{item['key']}", symbol), "derived_input"
    if item.get("kind") == "derived":
        return [], "derived_later"
    if source == "yahoo_chart" and symbol:
        return collectors.yahoo_history(series_id, symbol), "primary"
    if source == "coinmarketcap" and item["key"] in CRYPTO_YAHOO:
        return collectors.yahoo_history(series_id, CRYPTO_YAHOO[item["key"]]), "fallback"
    if source == "fred_csv" and symbol:
        return collectors.fred_history(series_id, symbol), "primary"
    if source == "eastmoney" and symbol:
        try:
            return collectors.eastmoney_history(series_id, symbol), "primary"
        except Exception:
            fallback = str(item.get("fallback") or "")
            if fallback.startswith("yahoo_chart:"):
                return collectors.yahoo_history(series_id, fallback.split(":", 1)[1]), "fallback"
            raise
    if source == "eastmoney" and item["key"] == "southbound_flow":
        return collectors.eastmoney_southbound(series_id), "primary"
    if source == "tradingview" and symbol:
        return collectors.tradingview_history(series_id, symbol), "primary"
    if source == "deanfi_data":
        return collectors.deanfi_breadth(series_id), "primary"
    if source == "hkma_api":
        return collectors.hkma_hibor(series_id), "primary"
    if source == "aastocks_hk_market":
        metric = "turnover" if item["key"] == "hk_turnover" else "ratio"
        return collectors.aastocks_market(series_id, metric), "primary"
    if source == "straits_live":
        return collectors.straits_history(series_id), "primary"
    if source == "cftc_public":
        return collectors.cftc_metals_positioning(series_id), "primary"
    if source == "imf_portwatch":
        return collectors.portwatch_black_sea(series_id), "primary"
    if source == "eia_bulk" and item["key"] == "us_crude_inventory":
        return collectors.eia_petroleum_series(series_id, symbol or "PET.WCESTUS1.W"), "primary"
    if item["key"] == "liquidations":
        return collectors.bykaranteli_liquidations(series_id), "primary"
    if source == "world_gold_council":
        return collectors.world_gold_council_fund_flow(series_id), "primary"
    if source == "coinglass" and item["key"] == "open_interest":
        return collectors.binance_open_interest(series_id), "fallback"
    if source == "usda_esmis":
        return collectors.usda_crop_condition(series_id), "primary"
    return [], "adapter_pending"


def collect_funding(delay: float) -> list[dict[str, Any]]:
    command = [os.environ.get("PYTHON_BINARY") or os.sys.executable, str(ROOT / "scripts" / "funding_history.py"),
               "--days", "370", "--delay", str(max(delay, 0.75))]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    payload = json.loads(completed.stdout or completed.stderr)
    if completed.returncode:
        raise RuntimeError(f"funding history failed: {payload.get('errors')}")
    by_date: dict[str, dict[str, float]] = defaultdict(dict)
    for row in payload.get("daily") or []:
        by_date[row["date"]][row["asset"]] = float(row["funding_rate_8h_median"])
    rows = []
    for day, values in sorted(by_date.items()):
        if not values:
            continue
        composite = sum(values.values()) / len(values)
        rows.append({"series_id": "crypto.perpetual_funding", "observed_date": day,
                     "value": composite, "close": composite, "source": "exchange_funding_apis",
                     "source_priority": 10, "quality_status": "ok", "method_version": payload["method_version"],
                     "available_at_utc": f"{day}T23:59:59+00:00", "metadata": values})
    return rows


def derive_series(connection) -> list[dict[str, Any]]:
    definitions = {
        "energy.brent_wti_spread": ("energy.brent", "energy.wti", lambda a, b: a - b, "spread-v1"),
        "precious_metals.gold_silver_ratio": ("precious_metals.gold", "precious_metals.silver", lambda a, b: a / b if b else None, "ratio-v1"),
        "fx.us2y_germany2y_spread": ("fx.us2y", "fx._input_us2y_germany2y_spread", lambda a, b: a - b, "yield-spread-v1"),
        "fx.us2y_japan2y_spread": ("fx.us2y", "fx._input_us2y_japan2y_spread", lambda a, b: a - b, "yield-spread-v1"),
        "fx.us2y_china2y_spread": ("fx.us2y", "fx._input_us2y_china2y_spread", lambda a, b: a - b, "yield-spread-v1"),
    }
    rows = []
    for output, (left, right, formula, method) in definitions.items():
        values = connection.execute("""
          SELECT a.observed_date, COALESCE(a.close,a.value), COALESCE(b.close,b.value)
          FROM canonical_observations a JOIN canonical_observations b USING(observed_date)
          WHERE a.series_id=? AND b.series_id=? ORDER BY a.observed_date
        """, [left, right]).fetchall()
        for day, a, b in values:
            value = formula(a, b)
            if value is not None:
                rows.append({"series_id": output, "observed_date": day.isoformat(), "value": value, "close": value,
                             "source": "derived", "source_priority": 1, "quality_status": "ok",
                             "method_version": method, "available_at_utc": f"{day.isoformat()}T23:59:59+00:00",
                             "metadata": {"inputs": [left, right]}})
    # 3-2-1 crack spread: gasoline and heating oil are cents/gallon in Yahoo; normalize to USD/gallon.
    values = connection.execute("""
      SELECT w.observed_date, COALESCE(w.close,w.value), COALESCE(r.close,r.value), COALESCE(h.close,h.value)
      FROM canonical_observations w JOIN canonical_observations r USING(observed_date)
      JOIN canonical_observations h USING(observed_date)
      WHERE w.series_id='energy.wti' AND r.series_id='energy.rbob_gasoline'
        AND h.series_id='energy.heating_oil' ORDER BY w.observed_date
    """).fetchall()
    for day, wti, rbob, heating in values:
        value = (2 * rbob * 42 + heating * 42 - 3 * wti) / 3
        rows.append({"series_id": "energy.crack_spread", "observed_date": day.isoformat(), "value": value,
                     "close": value, "source": "derived", "source_priority": 1, "quality_status": "ok",
                     "method_version": "cme-321-v1", "available_at_utc": f"{day.isoformat()}T23:59:59+00:00",
                     "metadata": {"inputs": ["energy.wti", "energy.rbob_gasoline", "energy.heating_oil"]}})
    values = connection.execute("""
      SELECT s.observed_date,
             TRY_CAST(json_extract_string(s.metadata_json, '$.amount') AS DOUBLE),
             TRY_CAST(json_extract_string(z.metadata_json, '$.amount') AS DOUBLE)
      FROM canonical_observations s JOIN canonical_observations z USING(observed_date)
      WHERE s.series_id='china.sse_composite' AND z.series_id='china.szse_component'
        AND TRY_CAST(json_extract_string(s.metadata_json, '$.amount') AS DOUBLE) IS NOT NULL
        AND TRY_CAST(json_extract_string(z.metadata_json, '$.amount') AS DOUBLE) IS NOT NULL
      ORDER BY s.observed_date
    """).fetchall()
    for day, sse_amount, szse_amount in values:
        value = (sse_amount + szse_amount) / 100_000_000.0
        rows.append({"series_id": "china.a_share_turnover", "observed_date": day.isoformat(), "value": value,
                     "close": value, "source": "derived_eastmoney", "source_priority": 1, "quality_status": "ok",
                     "method_version": "sse-szse-turnover-v1", "available_at_utc": f"{day.isoformat()}T09:00:00+00:00",
                     "metadata": {"unit": "亿元", "sse_amount_raw": sse_amount, "szse_amount_raw": szse_amount}})
    return rows


def run_bootstrap(db_path: str | Path, *, delay: float = 0.8, refresh: bool = False,
                  revision_lookback_days: int = 10) -> dict[str, Any]:
    catalog = load_catalog()
    connection = connect(db_path)
    sync_catalog(connection, catalog)
    started = datetime.now(timezone.utc)
    connection.execute("""UPDATE bootstrap_runs SET status='interrupted',completed_at_utc=?
      WHERE status='running'""", [started])
    run_id = connection.execute("""INSERT INTO bootstrap_runs
      (started_at_utc,mode,status,requested_start,requested_end) VALUES (?,?,?,?,?) RETURNING run_id""",
      [started, "daily_refresh" if refresh else "all_available_history", "running",
       date.today() - timedelta(days=revision_lookback_days) if refresh else date(1900, 1, 1),
       date.today()]).fetchone()[0]
    results = []
    for market, item in entries(catalog):
        series_id = f"{market}.{item['key']}"
        if series_id == "crypto.perpetual_funding":
            continue
        checkpoint = connection.execute("""SELECT status,last_observed_date FROM source_checkpoints
          WHERE series_id=? AND status='ok' ORDER BY last_success_at_utc DESC LIMIT 1""", [series_id]).fetchone()
        if checkpoint and item.get("kind") == "ohlcv":
            recent_count = connection.execute("""SELECT COUNT(*) FROM market_observations
              WHERE series_id=? AND observed_date >= (
                SELECT MAX(observed_date) - INTERVAL 400 DAY FROM market_observations WHERE series_id=?
              )""", [series_id, series_id]).fetchone()[0]
            if recent_count < 200:
                checkpoint = None
        if checkpoint and not refresh:
            row_count = connection.execute("SELECT COUNT(*) FROM market_observations WHERE series_id=?", [series_id]).fetchone()[0]
            results.append({"series_id": series_id, "status": "checkpoint_existing", "rows": row_count})
            continue
        try:
            rows, mode = collect_item(market, item, delay)
            if refresh and checkpoint and checkpoint[1] and rows:
                floor = checkpoint[1] - timedelta(days=revision_lookback_days)
                rows = [row for row in rows if date.fromisoformat(row["observed_date"]) >= floor]
            count = upsert_observations(connection, rows) if rows else 0
            status = "success" if count else mode
            results.append({"series_id": series_id, "status": status, "rows": count})
            if count:
                last = max(row["observed_date"] for row in rows)
                connection.execute("INSERT OR REPLACE INTO source_checkpoints VALUES (?,?,?,?,?,?)",
                                   [item.get("primary") or mode, series_id, last, datetime.now(timezone.utc), "ok", mode])
        except Exception as exc:
            results.append({"series_id": series_id, "status": "failed", "rows": 0,
                            "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
        time.sleep(max(delay, 0.75))
    funding_checkpoint = connection.execute("SELECT status FROM source_checkpoints WHERE series_id='crypto.perpetual_funding'").fetchone()
    if funding_checkpoint and funding_checkpoint[0] == "ok" and not refresh:
        count = connection.execute("SELECT COUNT(*) FROM market_observations WHERE series_id='crypto.perpetual_funding'").fetchone()[0]
        results.append({"series_id": "crypto.perpetual_funding", "status": "checkpoint_existing", "rows": count})
    else:
        try:
            funding = collect_funding(delay)
            if refresh and funding_checkpoint and funding:
                last_funding = connection.execute("SELECT MAX(observed_date) FROM market_observations WHERE series_id='crypto.perpetual_funding'").fetchone()[0]
                if last_funding:
                    floor = last_funding - timedelta(days=revision_lookback_days)
                    funding = [row for row in funding if date.fromisoformat(row["observed_date"]) >= floor]
            count = upsert_observations(connection, funding)
            results.append({"series_id": "crypto.perpetual_funding", "status": "success", "rows": count})
            if funding:
                connection.execute("INSERT OR REPLACE INTO source_checkpoints VALUES (?,?,?,?,?,?)",
                  ["exchange_funding_apis", "crypto.perpetual_funding", max(x["observed_date"] for x in funding),
                   datetime.now(timezone.utc), "ok", "bootstrap"])
        except Exception as exc:
            results.append({"series_id": "crypto.perpetual_funding", "status": "failed", "rows": 0,
                            "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
    derived = derive_series(connection)
    upsert_observations(connection, derived)
    for result in results:
        if result["status"] != "derived_later":
            continue
        produced = connection.execute("SELECT COUNT(*) FROM market_observations WHERE series_id=?",
                                      [result["series_id"]]).fetchone()[0]
        result["rows"] = produced
        result["status"] = "success" if produced else "adapter_pending"
    results.append({"series_id": "derived_series", "status": "success", "rows": len(derived)})
    # References and derived series are intentional catalog relationships, not missing data.
    # Only a source that still lacks an adapter makes the bootstrap partial.
    counts = {"success": sum(row["status"] in {"success", "checkpoint_existing", "reference", "derived_later"} for row in results),
              "partial": sum(row["status"] == "adapter_pending" for row in results),
              "failed": sum(row["status"] == "failed" for row in results)}
    status = "complete" if counts["failed"] == 0 and counts["partial"] == 0 else "partial"
    connection.execute("""UPDATE bootstrap_runs SET completed_at_utc=?,status=?,success_count=?,partial_count=?,
      failed_count=?,details_json=? WHERE run_id=?""", [datetime.now(timezone.utc), status, counts["success"],
      counts["partial"], counts["failed"], json.dumps(results, ensure_ascii=False), run_id])
    total_rows = connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]
    connection.close()
    return {"run_id": run_id, "status": status, "counts": counts, "observation_rows": total_rows, "results": results}
