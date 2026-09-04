from __future__ import annotations

import csv
import ast
import io
import json
import math
import os
import shutil
import subprocess
import time
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "financial-data-scraper-nextgen/1.0"


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_day(value: Any) -> str:
    """Normalize common provider date formats without changing the represented day."""
    raw = str(value or "").strip()[:10]
    for date_format in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, date_format).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unsupported date format: {raw!r}")


def fetch_json(url: str, timeout: float = 30, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last_error  # type: ignore[misc]


def fetch_text(url: str, timeout: float = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def yahoo_history(series_id: str, symbol: str, timeout: float = 30) -> list[dict[str, Any]]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="")
    period2 = int(datetime.now(timezone.utc).timestamp()) + 86_400
    payload = fetch_json(url + f"?period1=0&period2={period2}&interval=1d&events=history", timeout)
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"Yahoo returned no chart for {symbol}")
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adjusted = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for index, ts in enumerate(timestamps):
        close = _finite(adjusted[index] if index < len(adjusted) else None)
        if close is None:
            close = _finite((quote.get("close") or [None] * len(timestamps))[index])
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        rows.append({
            "series_id": series_id, "observed_date": day,
            "open": _finite((quote.get("open") or [None] * len(timestamps))[index]),
            "high": _finite((quote.get("high") or [None] * len(timestamps))[index]),
            "low": _finite((quote.get("low") or [None] * len(timestamps))[index]),
            "close": close, "value": close,
            "volume": _finite((quote.get("volume") or [None] * len(timestamps))[index]),
            "source": "yahoo_chart", "source_priority": 20,
            "available_at_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "quality_status": "ok", "method_version": "yahoo-chart-v1",
            "metadata": {"symbol": symbol, "timezone": result.get("meta", {}).get("exchangeTimezoneName")},
        })
    return rows


def fred_history(series_id: str, symbol: str, timeout: float = 30) -> list[dict[str, Any]]:
    text = fetch_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(symbol)}", timeout)
    rows = []
    for item in csv.DictReader(io.StringIO(text)):
        value = _finite(item.get(symbol))
        if value is None:
            continue
        day = item["observation_date"]
        rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                     "source": "fred_csv", "source_priority": 10, "quality_status": "ok",
                     "available_at_utc": f"{day}T23:59:59+00:00", "method_version": "fred-csv-v1",
                     "metadata": {"symbol": symbol}})
    return rows


def eastmoney_history(series_id: str, secid: str, timeout: float = 30) -> list[dict[str, Any]]:
    params = {"secid": secid, "klt": "101", "fqt": "0", "lmt": "10000", "end": "20500101",
              "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
    payload = fetch_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params), timeout)
    data = payload.get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        fields = line.split(",")
        if len(fields) < 7 or _finite(fields[2]) is None:
            continue
        rows.append({"series_id": series_id, "observed_date": fields[0], "open": _finite(fields[1]),
                     "close": _finite(fields[2]), "value": _finite(fields[2]), "high": _finite(fields[3]),
                     "low": _finite(fields[4]), "volume": _finite(fields[5]), "source": "eastmoney",
                     "source_priority": 10, "quality_status": "ok", "method_version": "eastmoney-kline-v1",
                     "available_at_utc": f"{fields[0]}T08:30:00+00:00",
                     "metadata": {"secid": secid, "name": data.get("name"), "amount": _finite(fields[6])}})
    return rows


def tradingview_history(series_id: str, symbol: str, rows: int = 5000, timeout: float = 90) -> list[dict[str, Any]]:
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        raise RuntimeError("Node runtime not found; set NODE_BINARY")
    completed = subprocess.run([node, str(ROOT / "scripts" / "tradingview_history.js"), "--symbol", symbol,
                                "--range", str(rows), "--timeout-ms", str(int(timeout * 1000))],
                               capture_output=True, text=True, timeout=timeout + 5, check=False)
    payload = json.loads(completed.stdout or completed.stderr)
    if completed.returncode or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"TradingView exited {completed.returncode}")
    result = []
    for bar in payload.get("bars") or []:
        ts = int(bar["timestamp"])
        if ts < 10_000_000_000:
            ts *= 1000
        day = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()
        close = _finite(bar.get("close"))
        if close is None:
            continue
        result.append({"series_id": series_id, "observed_date": day, "open": _finite(bar.get("open")),
                       "high": _finite(bar.get("high")), "low": _finite(bar.get("low")), "close": close,
                       "value": close, "volume": _finite(bar.get("volume")), "source": "tradingview",
                       "source_priority": 30, "quality_status": "ok", "method_version": "tradingview-ws-v1",
                       "available_at_utc": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(),
                       "metadata": {"symbol": symbol}})
    return result


def deanfi_breadth(series_id: str, timeout: float = 30) -> list[dict[str, Any]]:
    payload = fetch_json("https://r2.deanfi.com/advance-decline/ad_line_historical.json", timeout)
    data = payload.get("data") if isinstance(payload, dict) else payload
    rows = []
    for item in data or []:
        day = str(item.get("date") or item.get("Date") or "")[:10]
        value = _finite(item.get("ad_line") or item.get("adLine") or item.get("cumulative"))
        if day and value is not None:
            rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                         "source": "deanfi_data", "source_priority": 20, "quality_status": "ok",
                         "available_at_utc": f"{day}T23:59:59+00:00", "method_version": "deanfi-ad-v1",
                         "metadata": item})
    return rows


def hkma_hibor(series_id: str, timeout: float = 30) -> list[dict[str, Any]]:
    url = ("https://api.hkma.gov.hk/public/market-data-and-statistics/monthly-statistical-bulletin/"
           "er-ir/hk-interbank-ir-daily?segment=hibor.fixing&pagesize=1000")
    payload = fetch_json(url, timeout)
    records = (payload.get("result") or {}).get("records") or []
    rows = []
    for item in records:
        day = str(item.get("end_of_day") or "")[:10]
        value = _finite(item.get("ir_overnight"))
        if day and value is not None:
            rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                         "source": "hkma_api", "source_priority": 10, "quality_status": "ok",
                         "available_at_utc": f"{day}T23:59:59+00:00", "method_version": "hkma-hibor-v1",
                         "metadata": {k: item.get(k) for k in ("ir_overnight", "ir_1w", "ir_1m", "ir_3m")}})
    return rows


def straits_history(series_id: str, timeout: float = 30) -> list[dict[str, Any]]:
    payload = fetch_json("https://straits.live/api/v1/transits?history=1&limit=2000", timeout)
    records = payload.get("chokepointTransitsHistory") or payload.get("history") or []
    rows = []
    for item in records:
        day = str(item.get("date") or "")[:10]
        value = _finite(item.get("nTotal"))
        if day and value is not None:
            rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                         "source": "straits_live", "source_priority": 20, "quality_status": "ok",
                         "available_at_utc": f"{day}T23:59:59+00:00", "method_version": "straits-live-v1",
                         "metadata": item})
    return rows


def eastmoney_southbound(series_id: str, timeout: float = 30) -> list[dict[str, Any]]:
    params = {"reportName": "RPT_MUTUAL_DEAL_HISTORY", "columns": "ALL", "pageNumber": "1",
              "pageSize": "500", "sortTypes": "-1", "sortColumns": "TRADE_DATE",
              "source": "WEB", "client": "WEB", "filter": '(MUTUAL_TYPE="006")'}
    payload = fetch_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params), timeout)
    rows = []
    for item in (payload.get("result") or {}).get("data") or []:
        day = str(item.get("TRADE_DATE") or "")[:10]
        net = _finite(item.get("NET_DEAL_AMT"))
        if day and net is not None:
            rows.append({"series_id": series_id, "observed_date": day, "value": net / 100.0,
                         "close": net / 100.0, "source": "eastmoney", "source_priority": 10,
                         "quality_status": "ok", "method_version": "eastmoney-southbound-v1",
                         "available_at_utc": f"{day}T10:00:00+00:00",
                         "metadata": {"unit": "亿元", "deal_amount_yi": (_finite(item.get("DEAL_AMT")) or 0) / 100.0,
                                      "buy_amount_yi": (_finite(item.get("BUY_AMT")) or 0) / 100.0,
                                      "sell_amount_yi": (_finite(item.get("SELL_AMT")) or 0) / 100.0}})
    return rows


def eastmoney_margin_balance(series_id: str, timeout: float = 45) -> list[dict[str, Any]]:
    """All-market SSE/SZSE/BSE margin balance from one Eastmoney history request."""
    params = {
        "reportName": "RPT_MARGIN_DATASTATISTICS",
        "columns": "TRADE_DATE,FIN_BALANCE,LOAN_BALANCE,MARGIN_A_RATIO,FIN_BUY_AMT,LOAN_SELL_AMT,MARGIN_A_TRADE",
        "pageNumber": "1", "pageSize": "10000", "sortTypes": "1", "sortColumns": "TRADE_DATE",
        "source": "WEB", "client": "WEB",
    }
    payload = fetch_json("https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params), timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
    rows = []
    for item in (payload.get("result") or {}).get("data") or []:
        day = str(item.get("TRADE_DATE") or "")[:10]
        financing = _finite(item.get("FIN_BALANCE"))
        lending = _finite(item.get("LOAN_BALANCE"))
        if not day or financing is None or lending is None:
            continue
        value_yi = (financing + lending) / 100_000_000.0
        rows.append({"series_id": series_id, "observed_date": day, "value": value_yi, "close": value_yi,
                     "source": "eastmoney_margin", "source_priority": 20, "quality_status": "ok",
                     "method_version": "eastmoney-all-market-margin-v1",
                     "available_at_utc": f"{day}T10:00:00+00:00",
                     "metadata": {"unit": "亿元", "financing_balance_yi": financing / 100_000_000.0,
                                  "securities_lending_balance_yi": lending / 100_000_000.0,
                                  "margin_a_ratio_percent": _finite(item.get("MARGIN_A_RATIO")),
                                  "financing_buy_amount_yi": (_finite(item.get("FIN_BUY_AMT")) or 0) / 100_000_000.0,
                                  "coverage": "SSE+SZSE+BSE aggregate"}})
    return rows


def bykaranteli_etf_flows(series_id: str, timeout: float = 45) -> list[dict[str, Any]]:
    """Combined US spot BTC and ETH ETF flow, retaining asset-level components."""
    payload = fetch_json("https://bykaranteli.com/api/v1/public/datasets/etf-flows.json", timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in payload.get("rows") or []:
        day, asset = str(item.get("date") or "")[:10], str(item.get("asset") or "").upper()
        flow = _finite(item.get("net_inflow_usd"))
        if day and asset in {"BTC", "ETH"} and flow is not None:
            grouped[day][asset] = {
                "net_inflow_usd": flow,
                "net_assets_usd": _finite(item.get("net_assets_usd")),
                "cumulative_inflow_usd": _finite(item.get("cumulative_inflow_usd")),
                "value_traded_usd": _finite(item.get("value_traded_usd")),
            }
    rows = []
    for day, assets in sorted(grouped.items()):
        flow_usd = sum(item["net_inflow_usd"] for item in assets.values())
        rows.append({"series_id": series_id, "observed_date": day, "value": flow_usd / 1_000_000.0,
                     "close": flow_usd / 1_000_000.0, "source": "bykaranteli_public",
                     "source_priority": 10, "quality_status": "ok",
                     "method_version": "bykaranteli-btc-eth-etf-flow-v1",
                     "available_at_utc": f"{day}T23:59:59+00:00",
                     "metadata": {"unit": "USD millions", "assets": assets,
                                  "coverage": "US spot BTC and ETH ETFs"}})
    return rows


def bykaranteli_liquidations(series_id: str, timeout: float = 60) -> list[dict[str, Any]]:
    """Daily liquidation floor aggregated across every published venue and symbol."""
    payload = fetch_json("https://bykaranteli.com/api/v1/public/datasets/liquidations-daily.json", timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
    grouped: dict[str, dict[str, Any]] = {}
    for item in payload.get("rows") or []:
        day = str(item.get("date") or "")[:10]
        long_usd = _finite(item.get("long_liquidations_usd"))
        short_usd = _finite(item.get("short_liquidations_usd"))
        if not day or long_usd is None or short_usd is None:
            continue
        record = grouped.setdefault(day, {"long_usd": 0.0, "short_usd": 0.0, "events": 0,
                                          "max_single_usd": 0.0, "exchanges": set(), "symbols": set()})
        record["long_usd"] += long_usd; record["short_usd"] += short_usd
        record["events"] += int(_finite(item.get("events")) or 0)
        record["max_single_usd"] = max(record["max_single_usd"], _finite(item.get("max_single_liquidation_usd")) or 0.0)
        record["exchanges"].add(str(item.get("exchange") or "unknown"))
        record["symbols"].add(str(item.get("symbol") or "unknown"))
    rows = []
    for day, item in sorted(grouped.items()):
        total = item["long_usd"] + item["short_usd"]
        rows.append({"series_id": series_id, "observed_date": day, "value": total, "close": total,
                     "source": "bykaranteli_public", "source_priority": 10, "quality_status": "ok",
                     "method_version": "bykaranteli-liquidations-floor-v1",
                     "available_at_utc": f"{day}T23:59:59+00:00",
                     "metadata": {"unit": "USD", "long_usd": item["long_usd"], "short_usd": item["short_usd"],
                                  "events": item["events"], "max_single_usd": item["max_single_usd"],
                                  "exchange_count": len(item["exchanges"]), "symbol_count": len(item["symbols"]),
                                  "coverage_note": "public-stream totals are a conservative floor"}})
    return rows


def aastocks_market(series_id: str, metric: str, timeout: float = 30) -> list[dict[str, Any]]:
    text = fetch_text("https://www.aastocks.com/en/stocks/market/shortselling/securities-eligible.aspx", timeout)
    series = {}
    for number in (1, 2, 3):
        match = re.search(rf"var Series{number}Data = (\[.*?\]);", text)
        series[number] = ast.literal_eval(match.group(1)) if match else []
    rows = []
    for short_row, long_row, ratio_row in zip(series[1], series[2], series[3]):
        day = _iso_day(short_row[2])
        short_value, long_value, ratio = _finite(short_row[1]), _finite(long_row[1]), _finite(ratio_row[1])
        if not day or short_value is None or long_value is None or ratio is None:
            continue
        value = short_value + long_value if metric == "turnover" else ratio
        rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                     "source": "aastocks_hk_market", "source_priority": 20, "quality_status": "ok",
                     "method_version": "aastocks-market-v1", "available_at_utc": f"{day}T10:00:00+00:00",
                     "metadata": {"short_turnover": short_value, "long_turnover": long_value,
                                  "short_selling_ratio": ratio}})
    return rows


def cftc_metals_positioning(series_id: str, timeout: float = 45) -> list[dict[str, Any]]:
    codes = {"088691": "gold", "084691": "silver", "076651": "platinum", "075651": "palladium"}
    params = {
        "$select": "contract_market_name,report_date_as_yyyy_mm_dd,open_interest_all,m_money_positions_long_all,m_money_positions_short_all,cftc_contract_market_code",
        "$where": "cftc_contract_market_code in('088691','084691','076651','075651')",
        "$order": "report_date_as_yyyy_mm_dd ASC", "$limit": "50000",
    }
    payload = fetch_json("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?" + urllib.parse.urlencode(params), timeout)
    grouped: dict[str, dict[str, Any]] = {}
    for item in payload:
        day = str(item.get("report_date_as_yyyy_mm_dd") or "")[:10]
        code = str(item.get("cftc_contract_market_code") or "")
        oi = _finite(item.get("open_interest_all")); long = _finite(item.get("m_money_positions_long_all")); short = _finite(item.get("m_money_positions_short_all"))
        if not day or code not in codes or not oi or long is None or short is None:
            continue
        net = long - short
        grouped.setdefault(day, {})[codes[code]] = {"long": long, "short": short, "net": net, "open_interest": oi, "net_percent_oi": 100.0 * net / oi}
    rows = []
    for day, metals in sorted(grouped.items()):
        value = sum(x["net_percent_oi"] for x in metals.values()) / len(metals)
        rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                     "source": "cftc_public", "source_priority": 10, "quality_status": "ok",
                     "method_version": "cftc-metals-net-percent-oi-v1", "available_at_utc": f"{day}T20:30:00+00:00",
                     "metadata": {"metals": metals, "composite": "equal mean of per-metal managed-money net percent of OI"}})
    return rows


def portwatch_black_sea(series_id: str, timeout: float = 45) -> list[dict[str, Any]]:
    params = {
        "where": "portid in ('port489','port843','port1419')",
        "outFields": "date,portid,portname,portcalls_dry_bulk,export_dry_bulk",
        "orderByFields": "date ASC", "resultRecordCount": "2000",
        "returnGeometry": "false", "f": "json",
    }
    base = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Ports_Data/FeatureServer/0/query?"
    features = []
    for offset in range(0, 50000, 2000):
        params["resultOffset"] = str(offset)
        payload = fetch_json(base + urllib.parse.urlencode(params), timeout)
        page = payload.get("features") or []
        features.extend(page)
        if not payload.get("exceededTransferLimit") or not page:
            break
    grouped: dict[str, dict[str, Any]] = {}
    for feature in features:
        item = feature.get("attributes") or {}
        raw_date = item.get("date")
        if raw_date is None:
            continue
        if isinstance(raw_date, str) and "-" in raw_date:
            day = raw_date[:10]
        else:
            day = datetime.fromtimestamp(float(raw_date) / 1000.0, timezone.utc).date().isoformat()
        export = _finite(item.get("export_dry_bulk")) or 0.0
        calls = _finite(item.get("portcalls_dry_bulk")) or 0.0
        record = grouped.setdefault(day, {"export_dry_bulk": 0.0, "portcalls_dry_bulk": 0.0, "ports": []})
        record["export_dry_bulk"] += export; record["portcalls_dry_bulk"] += calls
        record["ports"].append(item.get("portname") or item.get("portid"))
    return [{"series_id": series_id, "observed_date": day, "value": item["export_dry_bulk"], "close": item["export_dry_bulk"],
             "source": "imf_portwatch", "source_priority": 10, "quality_status": "ok",
             "method_version": "imf-portwatch-black-sea-v1", "available_at_utc": f"{day}T23:59:59+00:00",
             "metadata": item} for day, item in sorted(grouped.items())]


def eia_petroleum_series(series_id: str, eia_series: str = "PET.WCESTUS1.W", timeout: float = 180) -> list[dict[str, Any]]:
    """Read one series from EIA's keyless petroleum bulk archive."""
    file_descriptor, archive_path = tempfile.mkstemp(suffix=".zip")
    os.close(file_descriptor)
    try:
        request = urllib.request.Request("https://www.eia.gov/opendata/bulk/PET.zip", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response, open(archive_path, "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
        record = None
        with zipfile.ZipFile(archive_path) as archive:
            member = next(name for name in archive.namelist() if name.lower().endswith(".txt"))
            with archive.open(member) as stream:
                for raw_line in stream:
                    if eia_series.encode("ascii") not in raw_line:
                        continue
                    candidate = json.loads(raw_line)
                    if candidate.get("series_id") == eia_series:
                        record = candidate
                        break
    finally:
        Path(archive_path).unlink(missing_ok=True)
    if not record:
        raise RuntimeError(f"EIA bulk series not found: {eia_series}")
    rows = []
    for raw_day, raw_value in record.get("data") or []:
        value = _finite(raw_value)
        if value is None:
            continue
        day = datetime.strptime(str(raw_day), "%Y%m%d").date().isoformat()
        rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                     "source": "eia_bulk", "source_priority": 10, "quality_status": "ok",
                     "method_version": "eia-petroleum-bulk-v1", "available_at_utc": f"{day}T23:59:59+00:00",
                     "metadata": {"eia_series": eia_series, "unit": record.get("units"), "last_updated": record.get("last_updated")}})
    return rows


OPEC_PLUS_EIA_CODES = {
    "Algeria": "DZA", "Congo-Brazzaville": "COG", "Equatorial Guinea": "GNQ", "Gabon": "GAB",
    "Iran": "IRN", "Iraq": "IRQ", "Kuwait": "KWT", "Libya": "LBY", "Nigeria": "NGA",
    "Saudi Arabia": "SAU", "United Arab Emirates": "ARE", "Venezuela": "VEN",
    "Azerbaijan": "AZE", "Bahrain": "BHR", "Brunei": "BRN", "Kazakhstan": "KAZ",
    "Malaysia": "MYS", "Mexico": "MEX", "Oman": "OMN", "Russia": "RUS",
    "South Sudan": "SSD", "Sudan": "SDN",
}


def eia_opec_plus_output(series_id: str, timeout: float = 240) -> list[dict[str, Any]]:
    """Sum monthly country crude production for the versioned 22-country DoC set."""
    file_descriptor, archive_path = tempfile.mkstemp(suffix=".zip")
    os.close(file_descriptor)
    wanted = {f"INTL.57-1-{code}-TBPD.M": country for country, code in OPEC_PLUS_EIA_CODES.items()}
    records: dict[str, dict[str, Any]] = {}
    try:
        request = urllib.request.Request("https://www.eia.gov/opendata/bulk/INTL.zip", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response, open(archive_path, "wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
        with zipfile.ZipFile(archive_path) as archive:
            member = next(name for name in archive.namelist() if name.lower().endswith(".txt"))
            with archive.open(member) as stream:
                for raw_line in stream:
                    if b"INTL.57-1-" not in raw_line or b"-TBPD.M" not in raw_line:
                        continue
                    candidate = json.loads(raw_line)
                    if candidate.get("series_id") in wanted:
                        records[candidate["series_id"]] = candidate
    finally:
        Path(archive_path).unlink(missing_ok=True)
    missing = sorted(set(wanted) - set(records))
    if missing:
        raise RuntimeError(f"EIA OPEC+ component series missing: {missing}")
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    updated = []
    for eia_id, record in records.items():
        country = wanted[eia_id]; updated.append(str(record.get("last_updated") or ""))
        for raw_month, raw_value in record.get("data") or []:
            value = _finite(raw_value)
            if value is not None:
                grouped[str(raw_month)][country] = value
    rows = []
    for raw_month, countries in sorted(grouped.items()):
        if len(countries) != len(OPEC_PLUS_EIA_CODES):
            continue
        month_start = datetime.strptime(raw_month, "%Y%m").date()
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        day = (next_month - timedelta(days=1)).isoformat()
        total = sum(countries.values())
        rows.append({"series_id": series_id, "observed_date": day, "value": total, "close": total,
                     "source": "eia_bulk", "source_priority": 10, "quality_status": "ok",
                     "method_version": "eia-opec-plus-doc22-v1",
                     "available_at_utc": max(updated) if updated else f"{day}T23:59:59+00:00",
                     "metadata": {"unit": "thousand barrels per day", "countries": countries,
                                  "membership_version": "DoC-22-2026", "component_count": len(countries)}})
    return rows


def world_gold_council_fund_flow(series_id: str, timeout: float = 60) -> list[dict[str, Any]]:
    """Monthly global physically backed gold ETF flow from WGC's chart JSON."""
    payload = fetch_json("https://fsapi.gold.org/api/v11/charts/etfv2/revised/flows-chart2", timeout,
                         headers={"User-Agent": "Mozilla/5.0"})
    monthly = (((payload.get("chartData") or {}).get("data") or {}).get("Monthly") or {}).get("series") or {}
    usd_series = [item for item in monthly.get("usd") or [] if item.get("name") != "Gold Price (rhs)"]
    tonne_series = [item for item in monthly.get("tonnes") or [] if item.get("name") != "Gold Price (rhs)"]
    by_ts: dict[int, dict[str, Any]] = defaultdict(lambda: {"regions_usd": {}, "regions_tonnes": {}})
    for item in usd_series:
        for timestamp, value in item.get("data") or []:
            by_ts[int(timestamp)]["regions_usd"][item["name"]] = float(value)
    for item in tonne_series:
        for timestamp, value in item.get("data") or []:
            by_ts[int(timestamp)]["regions_tonnes"][item["name"]] = float(value)
    rows = []
    for timestamp, item in sorted(by_ts.items()):
        day = datetime.fromtimestamp(timestamp / 1000.0, timezone.utc).date().isoformat()
        flow_usd = sum(item["regions_usd"].values())
        rows.append({"series_id": series_id, "observed_date": day, "value": flow_usd / 1_000_000.0,
                     "close": flow_usd / 1_000_000.0, "source": "world_gold_council",
                     "source_priority": 10, "quality_status": "ok", "method_version": "wgc-global-gold-etf-monthly-v1",
                     "available_at_utc": str((payload.get("system") or {}).get("request_time") or f"{day}T23:59:59+00:00"),
                     "metadata": {"unit": "USD millions", "regions_usd": item["regions_usd"],
                                  "regions_tonnes": item["regions_tonnes"],
                                  "definition": "global physically backed gold ETF net flow"}})
    return rows


def binance_open_interest(series_id: str, timeout: float = 30) -> list[dict[str, Any]]:
    """Free exchange history; Binance currently retains only a short rolling window."""
    grouped: dict[str, dict[str, float]] = defaultdict(dict)
    for asset in ("BTC", "ETH"):
        params = urllib.parse.urlencode({"symbol": f"{asset}USDT", "period": "1d", "limit": "500"})
        payload = fetch_json("https://fapi.binance.com/futures/data/openInterestHist?" + params, timeout)
        for item in payload if isinstance(payload, list) else []:
            value = _finite(item.get("sumOpenInterestValue"))
            timestamp = item.get("timestamp")
            if value is None or timestamp is None:
                continue
            day = datetime.fromtimestamp(float(timestamp) / 1000.0, timezone.utc).date().isoformat()
            grouped[day][asset] = value
    rows = []
    for day, assets in sorted(grouped.items()):
        value = sum(assets.values())
        rows.append({"series_id": series_id, "observed_date": day, "value": value, "close": value,
                     "source": "binance", "source_priority": 20, "quality_status": "ok",
                     "method_version": "binance-btc-eth-oi-usd-v1", "available_at_utc": f"{day}T23:59:59+00:00",
                     "metadata": {"assets_usd": assets, "coverage": "free rolling exchange window"}})
    return rows


def _parse_usda_crop_condition(text: str) -> tuple[str | None, dict[str, float]]:
    crops: dict[str, float] = {}
    observed_day = None
    pattern = re.compile(r"^(Corn|Soybean|Cotton|Winter Wheat|Spring Wheat) Condition - Selected States.*?Week Ending\s+([A-Za-z]+ \d{1,2}, \d{4})", re.M)
    matches = list(pattern.finditer(text))
    for index, match in enumerate(matches):
        crop = match.group(1).lower().replace(" ", "_")
        try:
            day = datetime.strptime(match.group(2), "%B %d, %Y").date().isoformat()
            observed_day = max(observed_day or day, day)
        except ValueError:
            pass
        section = text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else match.end() + 6000]
        national = re.search(r"(?m)^\s*\d+\s+States\s+\.+:\s+([^\r\n]+)", section)
        if not national:
            continue
        values = [0.0 if token == "-" else float(token) for token in re.findall(r"\d+(?:\.\d+)?|-", national.group(1))]
        if len(values) >= 5:
            crops[crop] = values[-2] + values[-1]
    return observed_day, crops


def usda_crop_condition(series_id: str, timeout: float = 30, max_releases: int = 80) -> list[dict[str, Any]]:
    releases = []
    for page in range((max_releases + 24) // 25):
        payload = fetch_json(f"https://esmis.nal.usda.gov/api/v1/release/findByIdentifier/CropProg?page={page}", timeout)
        releases.extend(payload.get("results") or [])
        if len(releases) >= max_releases or page + 1 >= int((payload.get("pager") or {}).get("total_pages") or 0):
            break
    rows_by_day = {}
    for release in releases[:max_releases]:
        text_url = next((url for url in release.get("files") or [] if str(url).lower().endswith(".txt")), None)
        if not text_url:
            continue
        try:
            text = fetch_text(text_url, timeout)
            observed_day, crops = _parse_usda_crop_condition(text)
        except Exception:
            continue
        if not observed_day or not crops:
            continue
        value = sum(crops.values()) / len(crops)
        release_time = str(release.get("release_datetime") or "")
        rows_by_day[observed_day] = {"series_id": series_id, "observed_date": observed_day, "value": value, "close": value,
          "source": "usda_esmis", "source_priority": 10, "quality_status": "ok",
          "method_version": "usda-crop-good-excellent-v1", "available_at_utc": release_time or f"{observed_day}T23:59:59+00:00",
          "metadata": {"crops_good_excellent_percent": crops, "release_id": release.get("id"), "release_title": release.get("title")}}
    return [rows_by_day[day] for day in sorted(rows_by_day)]
