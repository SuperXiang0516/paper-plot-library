"""Classified provider failures reach reports without publishing raw responses."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import _stage_failure, run_pipeline
from paper_plot_library.storage import write_json
from test_pipeline_audit import Client, adapters, record


class ReportDiagnosticTests(unittest.TestCase):
    def test_current_ai_and_metric_reasons_reach_run_report(self):
        with tempfile.TemporaryDirectory(prefix="ppl-report-fixture-") as folder:
            root = Path(folder)
            write_json(root / "data" / "catalog.json", {"authorCodeExecuted": False,
                       "papers": [record(1)], "inputSpecs": {}, "dimensions": {}})
            active = adapters(
                summary=lambda *args: {"status": "invalid_response", "error_reason": "response_truncated"},
                metrics=lambda *args: {"status": "error", "error_reason": "metrics_provider_error"})
            settings = Settings(llm_api_key="offline-only", llm_model="offline-only",
                                easyscholar_key="offline-only", citation_enrichment=False)
            with patch("paper_plot_library.pipeline.build_site"):
                report = run_pipeline(root, settings, Client(), active, discover_new=False)
            failures = {failure["stage"]: failure for failure in report["failures"]}
            self.assertEqual(failures["ai"]["error_reason"], "response_truncated")
            self.assertEqual(failures["ai"]["status"], "invalid_response")
            self.assertEqual(failures["metrics"]["error_reason"], "metrics_provider_error")
            self.assertEqual(report["status"], "partial")

    def test_unsafe_reasons_and_old_stage_diagnostics_are_not_copied(self):
        paper = record(1)
        paper["automation"]["stages"] = {"ai": "invalid_response", "metrics": "not_checked"}
        paper["ai"] = {"error_reason": "https://invalid.example?key=private", "http_status": True}
        paper["metrics"] = {"error_reason": "metrics_provider_error", "http_status": 401}
        result = _stage_failure(paper, "ai: invalid_response")
        self.assertNotIn("error_reason", result)
        self.assertNotIn("http_status", result)
        old = _stage_failure(paper, "metrics: error")
        self.assertNotIn("error_reason", old)
        self.assertNotIn("status", old)


if __name__ == "__main__":
    unittest.main()
