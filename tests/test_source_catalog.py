import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SourceCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "config" / "source_catalog.json").read_text(encoding="utf-8"))

    def test_exact_market_domains(self):
        self.assertEqual(
            set(self.catalog["markets"]),
            {"us", "china", "hong_kong", "crypto", "energy", "precious_metals", "agriculture", "fx"},
        )

    def test_expected_indicator_counts(self):
        expected = {
            "us": 9, "china": 10, "hong_kong": 10, "crypto": 10,
            "energy": 10, "precious_metals": 10, "agriculture": 10, "fx": 14,
        }
        self.assertEqual({key: len(value) for key, value in self.catalog["markets"].items()}, expected)

    def test_market_keys_are_unique_within_domain(self):
        for market, indicators in self.catalog["markets"].items():
            keys = [item["key"] for item in indicators]
            self.assertEqual(len(keys), len(set(keys)), market)

    def test_source_references_exist(self):
        source_ids = set(self.catalog["sources"])
        for indicators in self.catalog["markets"].values():
            for item in indicators:
                for field in ("primary", "fallback"):
                    source = item.get(field)
                    if not source:
                        continue
                    source = source.split(":", 1)[0]
                    self.assertIn(source, source_ids, f"{item['key']} {field}")

    def test_no_duplicate_source_keys_in_raw_json(self):
        raw = (ROOT / "config" / "source_catalog.json").read_text(encoding="utf-8")
        pairs = []

        def hook(items):
            keys = [key for key, _ in items]
            self.assertEqual(len(keys), len(set(keys)), f"duplicate JSON key in {keys}")
            pairs.extend(keys)
            return dict(items)

        json.loads(raw, object_pairs_hook=hook)

    def test_readiness_report_covers_all_entries(self):
        output = subprocess.check_output(
            [sys.executable, str(ROOT / "scripts" / "assess_source_readiness.py")],
            text=True,
        )
        report = json.loads(output)
        self.assertEqual(report["entry_count"], 83)
        self.assertEqual(report["ready_count"] + report["unresolved_count"], 83)

    def test_perpetual_funding_is_browser_free_and_method_fixed(self):
        funding = next(item for item in self.catalog["markets"]["crypto"] if item["key"] == "perpetual_funding")
        source = self.catalog["sources"][funding["primary"]]
        self.assertEqual(funding["primary"], "exchange_funding_apis")
        self.assertEqual(source["access"], "keyless_rest_apis")
        self.assertIn("8-hour", funding["definition"])
        self.assertIn("not mixed", funding["definition"])

    def test_fx_uses_existing_vix_without_fxvl_dependency(self):
        fx_volatility = self.catalog["markets"]["fx"][-1]
        self.assertEqual(fx_volatility, {"key": "vix", "label": "VIX", "kind": "reference", "ref": "us.vix"})
        self.assertNotIn("cme_cvol", self.catalog["sources"])
        self.assertNotIn("FXVL", json.dumps(self.catalog))


if __name__ == "__main__":
    unittest.main()
