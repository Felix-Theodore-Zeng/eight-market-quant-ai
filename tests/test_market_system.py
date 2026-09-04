import json
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from datetime import date, timedelta
from pathlib import Path

from market_system.analytics import compute_statistics
from market_system.ai_pipeline import _write_markdown
from market_system.bootstrap import collect_item
from market_system.calibration import build_monthly_calibration
from market_system.collectors import (_iso_day, opec_momr_latest,
                                      open_meteo_black_sea_weather)
from market_system.catalog import load_catalog
from market_system.db import connect, sync_catalog, upsert_observations
from market_system.packages import build_packages
from market_system.monitoring import alert_text, evaluate_system, record_runtime
from market_system.technical import (compute_technicals, fibonacci_assessment,
                                     support_resistance, wave_assessment)


class MarketSystemTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.duckdb"
        self.connection = connect(self.db_path)
        sync_catalog(self.connection, load_catalog())

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_schema_has_83_registry_entries(self):
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM series_registry").fetchone()[0], 83)

    def test_statistics_technicals_and_eight_bounded_packages(self):
        start = date(2025, 1, 1)
        rows = []
        for index in range(300):
            price = 100 + index * 0.15 + (index % 17 - 8) * 0.4
            day = (start + timedelta(days=index)).isoformat()
            rows.append({"series_id": "us.sp500", "observed_date": day, "open": price - 0.3,
                         "high": price + 1, "low": price - 1, "close": price, "value": price,
                         "volume": 1000 + index, "source": "fixture", "quality_status": "ok",
                         "available_at_utc": f"{day}T23:00:00+00:00", "method_version": "fixture-v1"})
        upsert_observations(self.connection, rows)
        as_of = rows[-1]["observed_date"]
        self.assertGreaterEqual(compute_statistics(self.connection, as_of), 6)
        result = compute_technicals(self.connection, as_of)
        self.assertEqual(result["waves"], 1)
        self.assertEqual(result["fibonacci"], 1)
        manifest = build_packages(self.connection, as_of, Path(self.temp.name) / "packages")
        self.assertEqual(len(manifest["markets"]), 8)
        self.assertTrue(all(item["bytes"] <= 36000 for item in manifest["markets"]))

    def test_mechanical_engines_do_not_claim_certain_elliott_count(self):
        rows = [{"date": date(2025, 1, 1) + timedelta(days=i), "open": None, "high": 101+i,
                 "low": 99+i, "close": 100+i+(-1)**i, "value": None, "volume": None,
                 "source": "fixture", "quality": "ok"} for i in range(80)]
        levels = support_resistance(rows)
        self.assertEqual(set(levels), {"support", "resistance"})
        wave = wave_assessment(rows)
        self.assertIn("mechanical_zigzag", wave["evidence"])

    def test_fibonacci_uses_confirmed_mechanical_swing(self):
        prices = ([100 + i * 2 for i in range(16)] + [130 - i * 2 for i in range(12)] +
                  [108 + i * 2 for i in range(12)] + [130 - i for i in range(10)])
        rows = [{"date": date(2025, 1, 1) + timedelta(days=i), "open": price,
                 "high": price + 1, "low": price - 1, "close": price, "value": None,
                 "volume": None, "source": "fixture", "quality": "ok"}
                for i, price in enumerate(prices)]
        result = fibonacci_assessment(rows)
        self.assertIsNotNone(result)
        self.assertEqual(set(result["retracements"]), {"0.382", "0.5", "0.618", "0.786"})
        self.assertEqual(set(result["extensions"]), {"1.272", "1.618", "2.0", "2.618"})
        self.assertEqual(result["evidence"], "latest_completed_mechanical_zigzag_swing")

    def test_provider_dates_accept_dash_and_slash(self):
        self.assertEqual(_iso_day("2026-08-14"), "2026-08-14")
        self.assertEqual(_iso_day("2026/08/14"), "2026-08-14")

    def test_eastmoney_index_uses_configured_yahoo_fallback(self):
        item = {"key": "csi300", "kind": "ohlcv", "primary": "eastmoney", "symbol": "1.000300",
                "fallback": "yahoo_chart:000300.SS"}
        with patch("market_system.bootstrap.collectors.eastmoney_history", side_effect=OSError("blocked")), \
             patch("market_system.bootstrap.collectors.yahoo_history", return_value=[{"observed_date": "2026-09-02"}]) as yahoo:
            rows, mode = collect_item("china", item, 0)
        self.assertEqual(mode, "fallback")
        self.assertEqual(rows[0]["observed_date"], "2026-09-02")
        yahoo.assert_called_once_with("china.csi300", "000300.SS")

    def test_markdown_is_ios_friendly_utf8_bom_with_crlf(self):
        target = Path(self.temp.name) / "report.md"
        _write_markdown(target, "# 八市场\n\n中文正文\n")
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertIn("中文正文", raw.decode("utf-8-sig"))

    def test_market_analysis_schema_is_valid_and_requires_fibonacci(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas" / "market-analysis-v1.schema.json")
                            .read_text(encoding="utf-8"))
        self.assertIn("fibonacci", schema["properties"]["technical_view"]["required"])

    def test_monitoring_tables_and_runtime_metrics(self):
        record_runtime(self.db_path, "2026-09-03", "complete",
                       {"ai": 120.0, "delivery_document": 2.0,
                        "delivery_summary": 1.0, "total": 200.0})
        row = self.connection.execute("SELECT ai_seconds,total_seconds FROM daily_runtime_metrics").fetchone()
        self.assertEqual(row, (120.0, 200.0))

    def test_monthly_calibration_contains_all_five_diagnostic_groups(self):
        target = Path(self.temp.name) / "calibration.json"
        payload = build_monthly_calibration(self.db_path, "2026-08", target)
        self.assertEqual(set(payload["five_factor_diagnostics"]), {
            "realized_volatility_20d_60d", "empirical_percentile", "max_drawdown",
            "ols_log_price_slope_r2", "support_resistance_touches_strength",
        })
        self.assertTrue(target.exists())

    def test_monitor_reports_missing_daily_run_without_blocking_evaluation(self):
        result = evaluate_system(self.db_path, "2026-09-03")
        self.assertEqual(result["status"], "critical")
        self.assertIn("未找到", result["daily_run"]["reason"])
        self.assertIn("监控失败", alert_text(result))

    def test_open_meteo_black_sea_weather_aggregates_daily_ports(self):
        location = {"daily": {"time": ["2026-09-03"], "temperature_2m_mean": [20],
                    "precipitation_sum": [2], "wind_speed_10m_max": [30], "wind_gusts_10m_max": [45]}}
        with patch("market_system.collectors.fetch_json", return_value=[location] * 4):
            result = open_meteo_black_sea_weather()
        self.assertEqual(result["2026-09-03"]["wind_kmh_max"], 30)
        self.assertEqual(len(result["2026-09-03"]["ports"]), 4)

    def test_opec_momr_latest_parses_total_and_keeps_monthly_date(self):
        response = MagicMock()
        response.read.return_value = b"%PDF-fake"
        response.headers = {"Last-Modified": "Wed, 15 Jul 2026 12:00:00 GMT"}
        response.__enter__.return_value = response
        page = MagicMock()
        page.extract_text.return_value = "Total DoC crude oil production averaged 36.28 mb/d in June 2026"
        reader = MagicMock(); reader.pages = [page]
        with patch("urllib.request.urlopen", return_value=response), patch("pypdf.PdfReader", return_value=reader):
            result = opec_momr_latest("energy.opec_plus_output")
        self.assertEqual(result["observed_date"], "2026-06-30")
        self.assertEqual(result["value"], 36280.0)

    def test_event_bundle_exposes_current_weather_without_forward_filling_export(self):
        rows = [
            {"series_id": "agriculture.black_sea_export_weather", "observed_date": "2026-08-28",
             "value": 100.0, "close": 100.0, "source": "fixture", "quality_status": "ok",
             "available_at_utc": "2026-08-29T00:00:00+00:00", "method_version": "fixture-v2",
             "metadata": {"component_dates": {"exports": "2026-08-28", "weather": "2026-08-28"}}},
            {"series_id": "agriculture.black_sea_export_weather", "observed_date": "2026-09-03",
             "value": None, "close": None, "source": "fixture", "quality_status": "partial",
             "available_at_utc": "2026-09-04T00:00:00+00:00", "method_version": "fixture-v2",
             "metadata": {"component_dates": {"exports": "2026-08-28", "weather": "2026-09-03"},
                          "latest_export": {"date": "2026-08-28", "export_dry_bulk": 100.0},
                          "weather": {"wind_kmh_max": 30.0}}},
        ]
        upsert_observations(self.connection, rows)
        compute_statistics(self.connection, "2026-09-03")
        output = Path(self.temp.name) / "event-packages"
        build_packages(self.connection, "2026-09-03", output)
        package = json.loads((output / "agriculture.json").read_text(encoding="utf-8"))
        indicator = next(item for item in package["indicators"] if item["key"] == "black_sea_export_weather")
        self.assertEqual(indicator["latest"]["date"], "2026-08-28")
        self.assertEqual(indicator["current_context"]["date"], "2026-09-03")
        self.assertEqual(indicator["current_context"]["weather"]["wind_kmh_max"], 30.0)
        advisory = next(x for x in package["data_quality"]["advisories"]
                        if x["series_id"] == "agriculture.black_sea_export_weather")
        self.assertEqual(advisory["component_dates"]["weather"], "2026-09-03")

    def test_opec_monthly_context_does_not_publish_stitched_statistics(self):
        row = {"series_id": "energy.opec_plus_output", "observed_date": "2026-06-30",
               "value": 36280.0, "close": 36280.0, "source": "opec_momr",
               "quality_status": "ok", "available_at_utc": "2026-08-12T13:59:36+00:00",
               "method_version": "opec-momr-doc-total-v1",
               "metadata": {"production_month": "2026-06-30", "report_month": "2026-07-01"}}
        upsert_observations(self.connection, [row])
        compute_statistics(self.connection, "2026-09-03")
        output = Path(self.temp.name) / "opec-packages"
        build_packages(self.connection, "2026-09-03", output)
        package = json.loads((output / "energy.json").read_text(encoding="utf-8"))
        indicator = next(item for item in package["indicators"] if item["key"] == "opec_plus_output")
        self.assertEqual(indicator["current_context"]["production_month"], "2026-06-30")
        self.assertEqual(indicator["statistics"], {})
        self.assertNotIn("calibration", indicator)
        advisory = next(x for x in package["data_quality"]["advisories"]
                        if x["series_id"] == "energy.opec_plus_output")
        self.assertEqual(advisory["type"], "statistics_disabled")


if __name__ == "__main__":
    unittest.main()
