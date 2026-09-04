# Eight-market source audit

Updated 2026-09-03. This audit covers the next-generation 83-entry catalog only; it does not alter the legacy 30-indicator collector.

## Result

- All 83 catalog entries are source-ready across the eight domains.
- FXVL was removed by design on 2026-09-03. The FX domain now references the already verified VIX series in the US domain, so there is no CME CVOL entitlement or publication-delay dependency.
- No source was accepted from a visible value alone. Price history needs at least 200 valid daily observations; lower-frequency series retain their native source date and cadence.

## Grouped source design

- Yahoo Chart supplies the bulk of global daily OHLCV. TradingView's pinned non-browser WebSocket adapter fills missing histories such as HSTECH, USD/CNH and non-US sovereign yields. CDP is only an operational fallback.
- Eastmoney supplies six mainland indices and exchange turnover. Official SSE and SZSE JSON endpoints supply the two margin-balance components.
- FRED supplies US 2Y, US 10Y, US real yield and US high-yield spread through one adapter.
- AAStocks supplies Hong Kong turnover and market short-selling ratio in one response. HKMA supplies HIBOR.
- DeanFi supplies S&P 500 constituent advances, declines, net breadth and A/D line with two machine-readable mirrors.
- EIA bulk files supply US commercial crude inventory and the versioned OPEC+ production aggregation. CFTC Socrata supplies four-metal managed-money positioning. USDA ESMIS supplies Crop Progress history. IMF PortWatch plus Open-Meteo supply separate Black Sea export and weather observations.
- Farside supplies combined US BTC+ETH spot ETF net flow while retaining both components. World Gold Council monthly global physically backed gold ETF flow is the declared precious-metals fund-flow proxy.

## Perpetual funding: browser-free decision

CoinMarketCap's v5 derivatives endpoints now return latest per-market funding rate, open interest, index price and basis. They are suitable for the daily independent cross-check, but the documented derivatives family currently exposes only `latest`, not a historical funding endpoint. It therefore cannot bootstrap the required one-year Funding history by itself.

The production history uses keyless public APIs from Binance, OKX and Bybit for BTC-USDT and ETH-USDT perpetual settled funding. Each venue observation is retained; rates are normalized to an 8-hour equivalent; each asset's daily value is the median of venue daily means. This method is identical for the historical bootstrap and future daily increments. CMC data is not blended into this series, preventing a method change at the bootstrap boundary.

Live acceptance on 2026-09-03:

- All three public endpoint schema probes passed.
- A two-day adapter sample returned 36 venue observations, six asset-date aggregates and no errors.
- The adapter retries transient failures, paginates within each provider's documented limit and applies a minimum 0.75-second API delay. A full-year run was deliberately not started during source qualification.

## News

Marketaux remains the selected daily financial-news API candidate; Alpha Vantage News Sentiment is the historical/backfill candidate. Both normalize into the local event table with publication and availability times. Horizon is not included because an aggregate API route exists. Credentialed plan-depth tests remain a deployment check rather than an indicator-source gap.

## Volatility scope

VIX is the sole requested volatility indicator. It is stored once in the US domain and referenced by the FX domain; the database and AI view must not duplicate the observation or perform a second collection.
