#!/usr/bin/env python3
"""Low-rate, read-only probes for candidate aggregate market-data sources."""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


USER_AGENT = "financial-market-source-probe/1.0"

YAHOO_SYMBOLS = {
    "sp500": "^GSPC", "nasdaq100": "^NDX", "dow_jones": "^DJI",
    "russell2000": "^RUT", "vix": "^VIX", "dxy": "DX-Y.NYB",
    "hang_seng": "^HSI", "hang_seng_china_enterprises": "^HSCE",
    "hang_seng_tech": "HSTECH.HK", "hkdusd": "HKD=X",
    "btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD",
    "brent": "BZ=F", "wti": "CL=F", "natural_gas": "NG=F",
    "heating_oil": "HO=F", "rbob_gasoline": "RB=F",
    "gold": "GC=F", "silver": "SI=F", "platinum": "PL=F", "palladium": "PA=F",
    "corn": "ZC=F", "wheat": "ZW=F", "soybeans": "ZS=F", "soybean_oil": "ZL=F",
    "cotton": "CT=F", "coffee": "KC=F", "sugar": "SB=F", "cocoa": "CC=F",
    "eurusd": "EURUSD=X", "usdjpy": "JPY=X", "gbpusd": "GBPUSD=X",
    "usdcnh": "CNH=X", "audusd": "AUDUSD=X", "usdcad": "CAD=X", "usdchf": "CHF=X",
}

EASTMONEY_SYMBOLS = {
    "csi300": "1.000300", "sse_composite": "1.000001",
    "szse_component": "0.399001", "chinext": "0.399006",
    "star50": "1.000688", "csi1000": "1.000852",
}

FRED_SERIES = {
    "us2y": "DGS2", "us10y": "DGS10",
    "us_real_yield": "DFII10", "us_high_yield_spread": "BAMLH0A0HYM2",
}

ROOT = Path(__file__).resolve().parents[1]


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_json_with_headers(url: str, timeout: float, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_text(url: str, timeout: float) -> str:
    command = subprocess.run(
        [
            "curl", "--http1.1", "-L", "--silent", "--show-error", "--fail",
            "--max-time", str(max(1, int(timeout))), "-A", USER_AGENT, url,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout + 2,
    )
    if command.returncode != 0:
        raise RuntimeError(command.stderr.strip() or f"curl exited {command.returncode}")
    return command.stdout


def probe_yahoo(timeout: float, delay: float) -> list[dict[str, Any]]:
    rows = []
    for key, symbol in YAHOO_SYMBOLS.items():
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(symbol, safe='')}?range=1y&interval=1d&events=history"
        )
        try:
            payload = fetch_json(url, timeout)
            result = (payload.get("chart", {}).get("result") or [{}])[0]
            timestamps = result.get("timestamp") or []
            quote = ((result.get("indicators", {}).get("quote") or [{}])[0])
            closes = quote.get("close") or []
            valid = sum(value is not None for value in closes)
            rows.append({
                "source": "yahoo_chart", "key": key, "symbol": symbol,
                "ok": valid >= 200, "row_count": len(timestamps),
                "valid_close_count": valid,
                "timezone": result.get("meta", {}).get("exchangeTimezoneName"),
            })
        except Exception as exc:  # probe must report failures instead of aborting
            rows.append({"source": "yahoo_chart", "key": key, "symbol": symbol, "ok": False, "error": repr(exc)})
        time.sleep(delay)
    return rows


def probe_eastmoney(timeout: float, delay: float) -> list[dict[str, Any]]:
    rows = []
    base = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    for key, secid in EASTMONEY_SYMBOLS.items():
        params = {
            "secid": secid, "klt": "101", "fqt": "0", "lmt": "400", "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        try:
            payload = fetch_json(f"{base}?{urllib.parse.urlencode(params)}", timeout)
            data = payload.get("data") or {}
            klines = data.get("klines") or []
            rows.append({
                "source": "eastmoney", "key": key, "symbol": secid,
                "ok": len(klines) >= 200, "row_count": len(klines),
                "first_date": klines[0].split(",")[0] if klines else None,
                "last_date": klines[-1].split(",")[0] if klines else None,
                "returned_name": data.get("name"),
            })
        except Exception as exc:
            rows.append({"source": "eastmoney", "key": key, "symbol": secid, "ok": False, "error": repr(exc)})
        time.sleep(delay)
    return rows


def probe_fred(timeout: float, delay: float) -> list[dict[str, Any]]:
    rows = []
    for key, series_id in FRED_SERIES.items():
        try:
            text = fetch_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout)
            parsed = list(csv.DictReader(io.StringIO(text)))
            value_key = next((name for name in (parsed[0].keys() if parsed else []) if name != "observation_date"), None)
            valid = [row for row in parsed if value_key and row.get(value_key) not in (None, "", ".")]
            rows.append({
                "source": "fred_csv", "key": key, "symbol": series_id,
                "ok": len(valid) >= 252, "row_count": len(valid),
                "first_date": valid[0]["observation_date"] if valid else None,
                "last_date": valid[-1]["observation_date"] if valid else None,
            })
        except Exception as exc:
            rows.append({"source": "fred_csv", "key": key, "symbol": series_id, "ok": False, "error": repr(exc)})
        time.sleep(delay)
    return rows


def probe_coinmarketcap(timeout: float) -> list[dict[str, Any]]:
    api_key = os.environ.get("CMC_API_KEY")
    if not api_key:
        return [{"source": "coinmarketcap", "key": "credential", "ok": None, "status": "skipped_no_credential"}]
    rows = []
    requests = {
        "crypto_quotes": "https://pro-api.coinmarketcap.com/v3/cryptocurrency/quotes/latest?symbol=BTC,ETH,SOL,XRP",
        "global_metrics": "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest",
        "btc_derivatives": "https://pro-api.coinmarketcap.com/v5/cryptocurrency/derivatives/market-pairs/list/latest?id=1&limit=100",
        "eth_derivatives": "https://pro-api.coinmarketcap.com/v5/cryptocurrency/derivatives/market-pairs/list/latest?id=1027&limit=100",
    }
    for key, url in requests.items():
        try:
            payload = fetch_json_with_headers(url, timeout, {"X-CMC_PRO_API_KEY": api_key})
            status = payload.get("status") or {}
            rows.append({
                "source": "coinmarketcap", "key": key,
                "ok": status.get("error_code") == 0,
                "credit_count": status.get("credit_count"),
            })
        except Exception as exc:
            rows.append({"source": "coinmarketcap", "key": key, "ok": False, "error": type(exc).__name__})
    return rows


def probe_exchange_funding(timeout: float, delay: float) -> list[dict[str, Any]]:
    endpoints = {
        "binance": "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=3",
        "okx": "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=3",
        "bybit": "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=3",
    }
    rows = []
    for source, url in endpoints.items():
        try:
            payload = fetch_json(url, timeout)
            if source == "binance":
                records = payload if isinstance(payload, list) else []
                required = {"symbol", "fundingTime", "fundingRate"}
            elif source == "okx":
                records = payload.get("data") or []
                required = {"instId", "fundingTime", "fundingRate"}
            else:
                records = (payload.get("result") or {}).get("list") or []
                required = {"symbol", "fundingRateTimestamp", "fundingRate"}
            rows.append({
                "source": "exchange_funding_apis", "key": source,
                "ok": bool(records) and required <= set(records[0]),
                "row_count": len(records), "fields": sorted(records[0]) if records else [],
            })
        except Exception as exc:
            rows.append({"source": "exchange_funding_apis", "key": source, "ok": False, "error": type(exc).__name__})
        time.sleep(delay)
    return rows


def probe_news(timeout: float) -> list[dict[str, Any]]:
    rows = []
    marketaux_token = os.environ.get("MARKETAUX_API_TOKEN")
    if marketaux_token:
        params = urllib.parse.urlencode({
            "api_token": marketaux_token, "language": "en", "limit": "3",
            "search": "global financial markets",
        })
        try:
            payload = fetch_json(f"https://api.marketaux.com/v1/news/all?{params}", timeout)
            articles = payload.get("data") or []
            rows.append({"source": "marketaux", "key": "daily_financial_news", "ok": bool(articles), "article_count": len(articles)})
        except Exception as exc:
            rows.append({"source": "marketaux", "key": "daily_financial_news", "ok": False, "error": type(exc).__name__})
    else:
        rows.append({"source": "marketaux", "key": "credential", "ok": None, "status": "skipped_no_credential"})

    alpha_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if alpha_key:
        params = urllib.parse.urlencode({
            "function": "NEWS_SENTIMENT", "topics": "financial_markets",
            "sort": "LATEST", "limit": "50", "apikey": alpha_key,
        })
        try:
            payload = fetch_json(f"https://www.alphavantage.co/query?{params}", timeout)
            articles = payload.get("feed") or []
            rows.append({"source": "alpha_vantage_news", "key": "historical_financial_news", "ok": bool(articles), "article_count": len(articles)})
        except Exception as exc:
            rows.append({"source": "alpha_vantage_news", "key": "historical_financial_news", "ok": False, "error": type(exc).__name__})
    else:
        rows.append({"source": "alpha_vantage_news", "key": "credential", "ok": None, "status": "skipped_no_credential"})
    return rows


def probe_hkma(timeout: float) -> list[dict[str, Any]]:
    url = (
        "https://api.hkma.gov.hk/public/market-data-and-statistics/"
        "monthly-statistical-bulletin/er-ir/hk-interbank-ir-daily"
        "?segment=hibor.fixing"
    )
    try:
        payload = fetch_json(url, timeout)
        records = (payload.get("result") or {}).get("records") or []
        required = {"end_of_day", "ir_overnight", "ir_1w", "ir_1m", "ir_3m"}
        fields = set(records[0]) if records else set()
        return [{
            "source": "hkma_api", "key": "hibor", "ok": bool(records) and required <= fields,
            "row_count": len(records), "fields": sorted(fields),
        }]
    except Exception as exc:
        return [{"source": "hkma_api", "key": "hibor", "ok": False, "error": type(exc).__name__}]


def probe_straits(timeout: float) -> list[dict[str, Any]]:
    try:
        payload = fetch_json("https://straits.live/api/v1/transits?history=1&limit=5", timeout)
        latest = payload.get("latest") or {}
        history = payload.get("chokepointTransitsHistory") or payload.get("history") or []
        required = {"date", "nTotal", "nTanker", "nCargo", "capacity"}
        return [{
            "source": "straits_live", "key": "hormuz_shipping",
            "ok": required <= set(latest) and bool(history),
            "latest_date": latest.get("date"), "history_rows": len(history),
            "fields": sorted(latest),
        }]
    except Exception as exc:
        return [{"source": "straits_live", "key": "hormuz_shipping", "ok": False, "error": type(exc).__name__}]


def probe_deanfi(timeout: float) -> list[dict[str, Any]]:
    urls = [
        "https://r2.deanfi.com/advance-decline/ad_line_historical.json",
        "https://raw.githubusercontent.com/DeanFinancials/deanfi-data/main/advance-decline/ad_line_historical.json",
    ]
    rows = []
    for url in urls:
        try:
            payload = fetch_json(url, timeout)
            data = payload.get("data") if isinstance(payload, dict) else payload
            data = data or []
            rows.append({
                "source": "deanfi_data", "key": "us_market_breadth",
                "endpoint": urllib.parse.urlparse(url).netloc,
                "ok": len(data) >= 250, "row_count": len(data),
            })
        except Exception as exc:
            rows.append({"source": "deanfi_data", "key": "us_market_breadth", "endpoint": url, "ok": False, "error": type(exc).__name__})
    return rows


def probe_aastocks(timeout: float) -> list[dict[str, Any]]:
    try:
        text = fetch_text("https://www.aastocks.com/en/stocks/market/shortselling/securities-eligible.aspx", timeout)
        series = {}
        for number in (1, 2, 3):
            match = re.search(rf"var Series{number}Data = (\[.*?\]);", text)
            series[number] = ast.literal_eval(match.group(1)) if match else []
        dates_match = bool(series[1]) and [row[2] for row in series[1]] == [row[2] for row in series[2]] == [row[2] for row in series[3]]
        return [{
            "source": "aastocks_hk_market", "key": "hk_turnover_and_short_ratio",
            "ok": dates_match and len(series[1]) >= 10,
            "row_count": len(series[1]), "last_date": series[1][-1][2] if series[1] else None,
        }]
    except Exception as exc:
        return [{"source": "aastocks_hk_market", "key": "hk_turnover_and_short_ratio", "ok": False, "error": type(exc).__name__}]


def probe_usda(timeout: float) -> list[dict[str, Any]]:
    try:
        payload = fetch_json("https://esmis.nal.usda.gov/api/v1/release/findByIdentifier/CropProg?page=0", timeout)
        results = payload.get("results") or []
        text_files = [url for row in results for url in row.get("files", []) if url.endswith(".txt")]
        return [{
            "source": "usda_esmis", "key": "usda_crop_condition",
            "ok": len(results) >= 20 and bool(text_files),
            "page_rows": len(results), "total_releases": (payload.get("pager") or {}).get("total_results"),
            "latest_text_url": text_files[0] if text_files else None,
        }]
    except Exception as exc:
        return [{"source": "usda_esmis", "key": "usda_crop_condition", "ok": False, "error": type(exc).__name__}]


def probe_cftc(timeout: float) -> list[dict[str, Any]]:
    try:
        params = {
            "$select": "contract_market_name,report_date_as_yyyy_mm_dd,open_interest_all,m_money_positions_long_all,m_money_positions_short_all,cftc_contract_market_code",
            "$where": "cftc_contract_market_code in('088691','084691','076651','075651')",
            "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "8",
        }
        payload = fetch_json("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?" + urllib.parse.urlencode(params), timeout)
        markets = {row.get("cftc_contract_market_code") for row in payload}
        return [{
            "source": "cftc_public", "key": "cftc_positioning",
            "ok": {"088691", "084691", "076651", "075651"} <= markets,
            "row_count": len(payload), "contract_codes": sorted(markets),
        }]
    except Exception as exc:
        return [{"source": "cftc_public", "key": "cftc_positioning", "ok": False, "error": type(exc).__name__}]


def probe_portwatch(timeout: float) -> list[dict[str, Any]]:
    try:
        params = {
            "where": "portid in ('port489','port843','port1419')",
            "outFields": "date,portid,portname,portcalls_dry_bulk,export_dry_bulk",
            "orderByFields": "date DESC", "resultRecordCount": "12",
            "returnGeometry": "false", "f": "json",
        }
        url = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Ports_Data/FeatureServer/0/query?" + urllib.parse.urlencode(params)
        payload = fetch_json(url, timeout)
        records = [item.get("attributes", {}) for item in payload.get("features", [])]
        fields = {"date", "portid", "portname", "portcalls_dry_bulk", "export_dry_bulk"}
        return [{
            "source": "imf_portwatch", "key": "black_sea_export",
            "ok": bool(records) and fields <= set(records[0]), "row_count": len(records),
        }]
    except Exception as exc:
        return [{"source": "imf_portwatch", "key": "black_sea_export", "ok": False, "error": type(exc).__name__}]


def probe_tradingview(timeout: float) -> list[dict[str, Any]]:
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        return [{"source": "tradingview", "key": "runtime", "ok": None, "status": "skipped_no_node_binary"}]
    try:
        command = subprocess.run(
            [node, str(ROOT / "scripts" / "tradingview_history.js"), "--symbol", "TVC:CN10Y", "--range", "370", "--timeout-ms", str(int(timeout * 1000))],
            capture_output=True, text=True, timeout=timeout + 3, check=False,
        )
        payload = json.loads(command.stdout or command.stderr)
        return [{
            "source": "tradingview", "key": "one_year_daily_history",
            "ok": command.returncode == 0 and payload.get("row_count", 0) >= 365,
            "symbol": payload.get("symbol"), "row_count": payload.get("row_count"),
        }]
    except Exception as exc:
        return [{"source": "tradingview", "key": "one_year_daily_history", "ok": False, "error": type(exc).__name__}]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--group",
        choices=["all", "yahoo", "eastmoney", "fred", "cmc", "funding", "news", "hkma", "straits", "deanfi", "aastocks", "usda", "cftc", "portwatch", "tradingview"],
        default="all",
    )
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    delay = max(args.delay, 0.75)
    results: list[dict[str, Any]] = []
    if args.group in ("all", "yahoo"):
        results.extend(probe_yahoo(args.timeout, delay))
    if args.group in ("all", "eastmoney"):
        results.extend(probe_eastmoney(args.timeout, delay))
    if args.group in ("all", "fred"):
        results.extend(probe_fred(args.timeout, delay))
    if args.group in ("all", "cmc"):
        results.extend(probe_coinmarketcap(args.timeout))
    if args.group in ("all", "funding"):
        results.extend(probe_exchange_funding(args.timeout, delay))
    if args.group in ("all", "news"):
        results.extend(probe_news(args.timeout))
    if args.group in ("all", "hkma"):
        results.extend(probe_hkma(args.timeout))
    if args.group in ("all", "straits"):
        results.extend(probe_straits(args.timeout))
    if args.group in ("all", "deanfi"):
        results.extend(probe_deanfi(args.timeout))
    if args.group in ("all", "aastocks"):
        results.extend(probe_aastocks(args.timeout))
    if args.group in ("all", "usda"):
        results.extend(probe_usda(args.timeout))
    if args.group in ("all", "cftc"):
        results.extend(probe_cftc(args.timeout))
    if args.group in ("all", "portwatch"):
        results.extend(probe_portwatch(args.timeout))
    if args.group in ("all", "tradingview"):
        results.extend(probe_tradingview(args.timeout))
    completed = [row for row in results if row.get("ok") is not None]
    skipped = [row for row in results if row.get("ok") is None]
    print(json.dumps({
        "schema_version": 1,
        "probed_at_utc": datetime.now(timezone.utc).isoformat(),
        "result_count": len(results),
        "passed": sum(bool(row.get("ok")) for row in completed),
        "failed": sum(not bool(row.get("ok")) for row in completed),
        "skipped": len(skipped),
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 0 if all(row.get("ok") for row in completed) else 2


if __name__ == "__main__":
    raise SystemExit(main())
