#!/usr/bin/env node
'use strict';

const TradingView = require('@mathieuc/tradingview');

const symbolPattern = /^[A-Z0-9_.-]+:[A-Z0-9_.!-]+$/i;

function fail(message, code = 2) {
  process.stderr.write(`${message}\n`);
  process.exit(code);
}

function parseArgs(argv) {
  const options = { range: 370, timeframe: '1D', timeoutMs: 30000 };
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!value) fail(`Missing value for ${key}`);
    if (key === '--symbol') options.symbol = value;
    else if (key === '--range') options.range = Number(value);
    else if (key === '--timeframe') options.timeframe = value;
    else if (key === '--timeout-ms') options.timeoutMs = Number(value);
    else fail(`Unknown argument: ${key}`);
  }
  if (!options.symbol || !symbolPattern.test(options.symbol)) fail('A valid EXCHANGE:SYMBOL is required');
  if (!Number.isInteger(options.range) || options.range < 1 || options.range > 5000) fail('range must be 1..5000');
  if (!/^(1|3|5|15|30|45|60|120|180|240|1D|1W|1M)$/.test(options.timeframe)) fail('unsupported timeframe');
  if (!Number.isInteger(options.timeoutMs) || options.timeoutMs < 1000 || options.timeoutMs > 120000) fail('timeout-ms must be 1000..120000');
  return options;
}

const options = parseArgs(process.argv.slice(2));
const credentials = process.env.TRADINGVIEW_SESSION_ID
  ? { token: process.env.TRADINGVIEW_SESSION_ID, signature: process.env.TRADINGVIEW_SESSION_SIGNATURE || '' }
  : undefined;
const client = new TradingView.Client(credentials);
const chart = new client.Session.Chart();
let finished = false;
let quietTimer = null;

function finish(code, payload) {
  if (finished) return;
  finished = true;
  clearTimeout(timer);
  if (quietTimer) clearTimeout(quietTimer);
  client.end();
  const stream = code === 0 ? process.stdout : process.stderr;
  stream.write(`${JSON.stringify(payload)}\n`);
  process.exit(code);
}

const timer = setTimeout(
  () => finish(3, { ok: false, source: 'tradingview_socket', symbol: options.symbol, error: 'timeout' }),
  options.timeoutMs,
);

client.onError((...errors) => finish(4, {
  ok: false,
  source: 'tradingview_socket',
  symbol: options.symbol,
  error: errors.map(String).join('; '),
}));

chart.setMarket(options.symbol, { timeframe: options.timeframe, range: options.range });
chart.onError((...errors) => finish(5, {
  ok: false,
  source: 'tradingview_socket',
  symbol: options.symbol,
  error: errors.map(String).join('; '),
}));
function emitBars() {
  if (!chart.periods.length) return;
  const bars = [...chart.periods]
    .map((bar) => ({
      timestamp: bar.time,
      open: bar.open,
      high: bar.max,
      low: bar.min,
      close: bar.close,
      volume: bar.volume,
    }))
    .sort((left, right) => left.timestamp - right.timestamp);
  finish(0, {
    schema_version: 1,
    ok: true,
    source: 'tradingview_socket',
    symbol: options.symbol,
    timeframe: options.timeframe,
    row_count: bars.length,
    bars,
  });
}

chart.onUpdate(() => {
  if (chart.periods.length >= options.range) return emitBars();
  if (quietTimer) clearTimeout(quietTimer);
  quietTimer = setTimeout(emitBars, 2500);
});
