#!/usr/bin/env python3
"""Fetch browser-free BTC/ETH perpetual funding history from public exchange APIs.

The adapter preserves venue observations and produces a consistent 8-hour-equivalent
cross-venue median. It prints JSON only; database ingestion owns persistence.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any


USER_AGENT = "financial-data-scraper-nextgen/1.0"
ASSETS = ("BTC", "ETH")
VENUES = ("binance", "okx", "bybit")
METHOD_VERSION = "cross_venue_median_8h_v1"


def fetch_json(url: str, timeout: float) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def fetch_venue(venue: str, asset: str, start_ms: int, end_ms: int, timeout: float, delay: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = start_ms if venue == "binance" else end_ms
    while True:
        if venue == "binance":
            page_limit = 1000
            params = {"symbol": f"{asset}USDT", "startTime": cursor, "endTime": end_ms, "limit": 1000}
            payload = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate?" + urllib.parse.urlencode(params), timeout)
            raw = payload
            batch = [{"timestamp_ms": int(x["fundingTime"]), "rate": float(x["fundingRate"])} for x in raw]
        elif venue == "okx":
            page_limit = 400
            params = {"instId": f"{asset}-USDT-SWAP", "after": cursor, "limit": 400}
            payload = fetch_json("https://www.okx.com/api/v5/public/funding-rate-history?" + urllib.parse.urlencode(params), timeout)
            raw = payload.get("data") or []
            batch = [{"timestamp_ms": int(x["fundingTime"]), "rate": float(x.get("realizedRate") or x["fundingRate"])} for x in raw]
            batch = [x for x in batch if start_ms <= x["timestamp_ms"] <= end_ms]
        else:
            page_limit = 200
            params = {"category": "linear", "symbol": f"{asset}USDT", "endTime": cursor, "limit": 200}
            payload = fetch_json("https://api.bybit.com/v5/market/funding/history?" + urllib.parse.urlencode(params), timeout)
            raw = (payload.get("result") or {}).get("list") or []
            batch = [{"timestamp_ms": int(x["fundingRateTimestamp"]), "rate": float(x["fundingRate"])} for x in raw]
            batch = [x for x in batch if start_ms <= x["timestamp_ms"] <= end_ms]
        if not batch:
            break
        rows.extend(batch)
        if len(raw) < page_limit:
            break
        if venue == "binance":
            next_cursor = max(x["timestamp_ms"] for x in batch) + 1
            if next_cursor <= cursor or next_cursor > end_ms:
                break
        else:
            raw_times = [int(x["fundingTime"] if venue == "okx" else x["fundingRateTimestamp"]) for x in raw]
            if min(raw_times) < start_ms:
                break
            next_cursor = min(x["timestamp_ms"] for x in batch) - 1
            if next_cursor >= cursor or next_cursor < start_ms:
                break
        cursor = next_cursor
        if start_ms <= cursor <= end_ms:
            time.sleep(delay)
    deduped = {row["timestamp_ms"]: row for row in rows}
    ordered = [deduped[key] for key in sorted(deduped)]
    for index, row in enumerate(ordered):
        interval_hours = 8.0
        if index:
            delta = (row["timestamp_ms"] - ordered[index - 1]["timestamp_ms"]) / 3_600_000
            if 0 < delta <= 24:
                interval_hours = delta
        row.update({
            "venue": venue,
            "asset": asset,
            "timestamp_utc": datetime.fromtimestamp(row["timestamp_ms"] / 1000, timezone.utc).isoformat(),
            "interval_hours": interval_hours,
            "rate_8h": row["rate"] * 8.0 / interval_hours,
        })
    return ordered


def aggregate_daily(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        date = row["timestamp_utc"][:10]
        buckets[(row["asset"], date)].append(row)
    daily = []
    for (asset, date), rows in sorted(buckets.items()):
        venue_daily: dict[str, float] = {}
        for venue in VENUES:
            rates = [row["rate_8h"] for row in rows if row["venue"] == venue]
            if rates:
                venue_daily[venue] = statistics.fmean(rates)
        if venue_daily:
            daily.append({
                "date": date, "asset": asset,
                "funding_rate_8h_median": statistics.median(venue_daily.values()),
                "venue_count": len(venue_daily), "venue_rates_8h": venue_daily,
                "method_version": METHOD_VERSION,
            })
    return daily


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--asset", choices=["BTC", "ETH", "all"], default="all")
    parser.add_argument("--venue", choices=[*VENUES, "all"], default="all")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    if not 1 <= args.days <= 370:
        parser.error("--days must be between 1 and 370")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    assets = ASSETS if args.asset == "all" else (args.asset,)
    venues = VENUES if args.venue == "all" else (args.venue,)
    observations = []
    errors = []
    for asset in assets:
        for venue in venues:
            try:
                observations.extend(fetch_venue(venue, asset, int(start.timestamp() * 1000), int(end.timestamp() * 1000), args.timeout, max(args.delay, 0.75)))
            except Exception as exc:
                errors.append({"asset": asset, "venue": venue, "error": type(exc).__name__})
            time.sleep(max(args.delay, 0.75))
    result = {
        "schema_version": 1, "method_version": METHOD_VERSION,
        "requested_days": args.days, "observation_count": len(observations),
        "daily_count": len(aggregate_daily(observations)), "errors": errors,
        "observations": observations, "daily": aggregate_daily(observations),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
