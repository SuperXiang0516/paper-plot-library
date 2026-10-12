"""Only complete static materials count toward daily admissions; offline data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import run_pipeline
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_pipeline_audit import Client, adapters, record


class SuccessfulAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-admission-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dimensions": {}})
        source = materials(complete=True)
        self.settings = Settings(require_complete_new_cases=True, initialize_existing_first=True,
            repair_limit=0, max_papers_per_run=5, candidate_checks=8, citation_enrichment=False)
        self.active = adapters(article=lambda *args: deepcopy(source["article"]),
            repository=lambda *args: deepcopy(source["repository"]),
            metrics=lambda *args: deepcopy(source["metrics"]),
            summary=lambda *args: deepcopy(source["ai"]),
            discover=lambda *args: [record(i) for i in range(1, 11)])

    def run_batch(self, active=None, **kwargs):
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), \
                patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, Client(), active or self.active, **kwargs)

    def test_incomplete_candidate_not_public_or_counted_then_next_batch_fills_target(self):
        original = self.active["repository"]
        def repository(client, selected):
            value = original(client, selected)
            if selected["doi"] == record(1)["doi"]:
                value["inputs"] = []
            return value
        first = self.run_batch(dict(self.active, repository=repository))
        self.assertEqual((first["checkedCount"], first["newCount"], first["dailyNewCount"]), (5, 4, 4))
        self.assertEqual(first["rejectedCandidateCount"], 1)
        self.assertNotIn(record(1)["doi"], [p["doi"] for p in read_json(self.root / "data" / "catalog.json")["papers"]])
        second = self.run_batch()
        self.assertEqual((second["newCount"], second["dailyNewCount"]), (1, 5))
        third = self.run_batch()
        self.assertEqual(third["newCount"], 0)

    def test_ai_failure_cannot_consume_success_quota_or_leave_incomplete_public_case(self):
        report = self.run_batch(dict(self.active, summary=lambda *args: {"status": "invalid_response"}))
        self.assertEqual((report["newCount"], report["dailyNewCount"]), (0, 0))
        self.assertEqual(read_json(self.root / "data" / "catalog.json")["papers"], [])
        self.assertEqual(len(read_json(self.root / "data" / "ledger.json")["candidates"]), 5)

    def test_one_remaining_success_slot_can_skip_bad_candidate_and_use_next_good_one(self):
        self.run_batch(dict(self.active, discover=lambda *args: [record(i) for i in range(1, 5)]))
        original = self.active["summary"]
        def summary(client, selected, *args):
            return {"status": "invalid_response"} if selected["doi"] == record(5)["doi"] else original(client, selected, *args)
        report = self.run_batch(dict(self.active, summary=summary,
            discover=lambda *args: [record(5), record(6)]))
        self.assertEqual((report["rejectedCandidateCount"], report["newCount"], report["dailyNewCount"]), (1, 1, 5))

    def test_no_licensed_image_means_no_admission_despite_valid_ai(self):
        original = self.active["article"]
        def article(*args):
            value = original(*args)
            value["figures"][0]["reuseStatus"] = "link_only"
            return value
        self.assertEqual(self.run_batch(dict(self.active, article=article))["newCount"], 0)

    def test_removed_doi_never_reappears_or_causes_source_or_ai_calls(self):
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {},
            "removedPapers": {record(1)["doi"]: {"reasonCode": "selected_figure_unavailable", "removedAt": NOW}}})
        touched = []
        active = dict(self.active, discover=lambda *args: [record(1)],
            lookup=lambda *args: touched.append("source"), summary=lambda *args: touched.append("ai"))
        report = self.run_batch(active)
        self.assertEqual((report["newCount"], report["checkedCount"], report["aiAttempts"]), (0, 0, 0))
        self.assertEqual(touched, [])
        self.assertEqual(read_json(self.root / "data" / "catalog.json")["papers"], [])

    def test_session_explicit_batch_rejects_invalid_or_nonexisting_dois_before_requests(self):
        before = (self.root / "data" / "catalog.json").read_bytes()
        for dois in ([record(1)["doi"]], ["bad"], []):
            with self.subTest(dois=dois), self.assertRaises(ValueError):
                self.run_batch(mode="backfill", discover_new=False, session_dois=dois)
        self.assertEqual((self.root / "data" / "catalog.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
