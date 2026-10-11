"""Offline bootstrap-to-daily acceptance; no real traffic, billing or code runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.bootstrap import bootstrap_report
from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.pipeline import run_pipeline
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_enrichment import ai_payload
from test_pipeline_audit import adapters, article_good, record, repo_good
from test_request_budget import BudgetFixtureOpener


def materials(index=1, complete=False):
    value = dict(record(index), figure="Figure 1")
    article = article_good()
    article["figures"][0]["sha256"] = hashlib.sha256(RASTER).hexdigest()
    repo = repo_good()
    repo["scripts"][0]["url"] = repo["url"] + "/blob/" + repo["commit"] + "/plot.py"
    repo["inputs"] = [{"name": "input.csv", "format": "csv", "status": "header_observed",
                       "headerSHA256": "c" * 64,
                       "fields": [{"name": "age", "kind": "actual_file_header"}]}]
    metrics = {"status": "matched", "if": "12.3", "jcr": "Q1", "source": "EasyScholar", "retrieved_at": NOW}
    value.update(article=article, repository=repo, metrics=metrics)
    if complete:
        summary = json.loads(ai_payload(evidence_ids=["publication"])["choices"][0]["message"]["content"])
        value["ai"] = dict(summary, status="generated")
        value["automation"] = {"status": "partial", "lastCheckedAt": NOW, "errors": [],
            "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected",
                       "ai": "generated", "metrics": "matched"}}
    return value


class BootstrapPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-bootstrap-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.settings = Settings(initialize_existing_first=True, bootstrap_batch_limit=20,
            repair_limit=5, max_ai_calls=5, max_requests=150, citation_enrichment=False,
            llm_api_key="offline-fixture-key", llm_model="offline-fixture-model")
        self.opener = BudgetFixtureOpener()
        source = materials()
        self.active = adapters(article=lambda *args: deepcopy(source["article"]),
            repository=lambda *args: deepcopy(source["repository"]),
            metrics=lambda *args: deepcopy(source["metrics"]),
            discover=lambda *args: [record(index) for index in range(100, 110)])
        self.active.pop("summary")  # Actual bounded transport, but synthetic responses.

    def catalog(self, papers):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": papers, "inputSpecs": {}, "dimensions": {}})

    def run_fixture(self, active=None, now=NOW, **kwargs):
        client = HttpClient(self.root / ".ppl" / "cache", self.settings.max_requests,
            max_post_attempts=self.settings.max_ai_calls, opener=self.opener,
            retries=1, sleep=lambda delay: None)
        with patch("paper_plot_library.pipeline.stamp", return_value=now), \
                patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, client, active or self.active, **kwargs)

    def current(self):
        return read_json(self.root / "data" / "catalog.json")

    def test_fifty_existing_cases_finish_before_any_discovery_then_next_auto_is_daily(self):
        self.catalog([dict(record(index), figure="Figure 1") for index in range(1, 51)])
        discovery_calls = []
        active = dict(self.active, discover=lambda *args: discovery_calls.append(True) or [record(i) for i in range(100, 110)])
        for day in range(11, 21):
            report = self.run_fixture(active, now=f"2026-10-{day:02d}T12:00:00Z")
            self.assertEqual(report["mode"], "backfill")
            self.assertEqual(report["newCount"], 0)
            self.assertLessEqual(report["checkedCount"], 20)
            self.assertLessEqual(report["aiAttempts"], 5)
            self.assertLessEqual(report["requests"], 150)
            self.assertEqual(report["bootstrap"]["completeCount"], (day - 10) * 5)
            self.assertEqual(discovery_calls, [])
            if day == 11:
                self.assertEqual(report["checkedCount"], 20)  # Not the old five-case repair cap.
        self.assertEqual(report["bootstrap"]["status"], "complete")
        self.assertEqual(report["discoveryStatus"], "bootstrap_complete_next_run")
        self.assertEqual(self.opener.methods.count("POST"), 50)
        self.settings.repair_limit = 0
        daily = self.run_fixture(active, now="2026-10-21T12:00:00Z")
        self.assertEqual((daily["mode"], daily["newCount"]), ("daily", 5))
        self.assertEqual(discovery_calls, [True])
        self.assertEqual(daily["bootstrap"]["totalCount"], 50)
        self.assertEqual(len(self.current()["papers"]), 55)
        ledger = read_json(self.root / "data" / "ledger.json")
        self.assertEqual(len(ledger["bootstrap"]["cohortDOIs"]), 50)
        self.assertTrue(all(p["runPerformed"] is False for p in self.current()["papers"]))

    def test_collection_budget_does_not_spend_more_than_five_ai_attempts(self):
        self.catalog([dict(record(index), figure="Figure 1") for index in range(1, 51)])
        def lookup(client, doi):
            for _ in range(11):
                client.get_json("https://api.github.com/offline-fixture", ttl=0)
            return {"status": "matched", "doi": doi}
        active = dict(self.active, lookup=lookup)
        report = self.run_fixture(active)
        self.assertEqual((report["requests"], report["collectionRequests"], report["aiAttempts"]), (150, 145, 5))
        self.assertEqual(report["newCount"], 0)
        self.assertEqual(report["bootstrap"]["completeCount"], 5)
        self.assertTrue(report["collectionBudgetExhausted"])
        self.assertEqual(self.opener.methods.count("POST"), 5)
        deferred = [p for p in self.current()["papers"] if p.get("automation", {}).get("stages", {}).get("metadata") == "budget_exhausted"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["automation"]["bootstrap"]["attempts"], 0)

    def test_daily_cannot_bypass_initialization_and_leaves_data_untouched(self):
        self.catalog([dict(record(1), figure="Figure 1")])
        path = self.root / "data" / "catalog.json"
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "尚未补齐"):
            self.run_fixture(mode="daily")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "data" / "ledger.json").exists())
        self.assertEqual(self.opener.methods, [])

    def test_attention_stops_auto_discovery_but_manual_retry_can_recover(self):
        self.catalog([dict(record(1), figure="Figure 1")])
        article = dict(materials()["article"], status="no_final_version", figures=[])
        active = dict(self.active, article=lambda *args: deepcopy(article))
        for count in range(1, 4):
            report = self.run_fixture(active, mode="backfill")
            saved = self.current()["papers"][0]
            self.assertEqual(saved["automation"]["bootstrap"]["attempts"], count)
            self.assertEqual(saved["automation"]["stages"]["ai"], "missing_evidence")
            self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(report["bootstrap"]["status"], "needs_attention")
        self.assertEqual(report["bootstrap"]["completeCount"], 0)
        stopped = self.run_fixture(now="2026-10-12T12:00:00Z")
        self.assertEqual((stopped["checkedCount"], stopped["newCount"]), (0, 0))
        self.assertEqual(stopped["discoveryStatus"], "bootstrap_needs_attention")
        recovered = self.run_fixture(mode="backfill")
        self.assertEqual(recovered["bootstrap"]["status"], "complete")
        self.assertEqual(recovered["newCount"], 0)
        self.assertEqual(self.opener.methods.count("POST"), 1)

    def test_source_exceptions_preserve_attempt_history_despite_old_budget_diagnostics(self):
        old = dict(record(1), figure="Figure 1", ai={"status": "budget_exhausted"}, metrics={"status": "budget_exhausted"})
        self.catalog([old])
        def unavailable(*args):
            raise RuntimeError("synthetic failure, not public transport text")
        active = dict(self.active, lookup=unavailable)
        for count in range(1, 4):
            report = self.run_fixture(active, mode="backfill")
            state = self.current()["papers"][0]["automation"]["bootstrap"]
            self.assertEqual(state["attempts"], count)
        self.assertEqual((state["status"], report["bootstrap"]["attentionCount"]), ("needs_attention", 1))
        self.assertEqual(self.opener.methods, [])

    def test_complete_cohort_regression_blocks_discovery_in_same_run(self):
        self.catalog([materials(1, complete=True)])
        self.settings.repair_limit = 1
        discovery_calls = []
        active = dict(self.active, article=lambda *args: {"status": "unavailable", "figures": []},
                      discover=lambda *args: discovery_calls.append(True) or [record(100)])
        report = self.run_fixture(active)
        self.assertEqual(report["mode"], "backfill")
        self.assertEqual(report["newCount"], 0)
        self.assertNotEqual(report["bootstrap"]["status"], "complete")
        self.assertEqual(discovery_calls, [])
        self.assertEqual(self.opener.methods, [])
        self.assertEqual(self.current()["papers"][0]["ai"]["status"], "stale")

    def test_complete_cohort_healthy_refresh_allows_daily_in_same_run(self):
        self.catalog([materials(1, complete=True)])
        self.settings.repair_limit = 1
        discovery_calls = []
        active = dict(self.active, discover=lambda *args: discovery_calls.append(True) or [])
        report = self.run_fixture(active)
        self.assertEqual(report["mode"], "daily")
        self.assertEqual(report["bootstrap"]["status"], "complete")
        self.assertEqual(discovery_calls, [True])
        self.assertEqual(report["aiAttempts"], 1)

    def test_complete_cases_are_skipped_during_manual_backfill(self):
        self.catalog([materials(1, complete=True), dict(record(2), figure="Figure 1")])
        report = self.run_fixture(mode="backfill")
        self.assertEqual((report["checkedCount"], report["aiAttempts"]), (1, 1))
        self.assertEqual(report["bootstrap"]["completeCount"], 2)
        self.assertNotIn("bootstrap", self.current()["papers"][0]["automation"])

    def test_explicit_doi_after_batch_boundary_is_not_lost(self):
        self.catalog([dict(record(index), figure="Figure 1") for index in range(1, 51)])
        report = self.run_fixture(only_dois=[record(50)["doi"]])
        self.assertEqual(report["checkedCount"], 1)
        chosen = next(p for p in self.current()["papers"] if p["id"] == "fixture-50")
        self.assertEqual(chosen["automation"]["bootstrap"]["status"], "complete")
        self.assertEqual(report["newCount"], 0)

    def test_tiny_budget_pauses_without_consuming_bootstrap_attempts(self):
        self.catalog([dict(record(1), figure="Figure 1")])
        self.settings.max_requests = 1
        report = self.run_fixture()
        self.assertEqual((report["checkedCount"], report["requests"], report["newCount"]), (0, 0, 0))
        self.assertTrue(report["collectionBudgetExhausted"])
        self.assertEqual(report["bootstrap"]["pendingCount"], 1)
        self.assertNotIn("bootstrap", self.current()["papers"][0]["automation"])

    def test_other_figure_does_not_finish_even_with_generated_ai(self):
        self.catalog([dict(record(1), figure="Figure 2")])
        report = self.run_fixture()
        self.assertEqual(report["bootstrap"]["completeCount"], 0)
        self.assertIn("selected_figure_missing", report["bootstrap"]["issues"][0]["reasonCodes"])
        self.assertEqual(report["aiAttempts"], 1)

    def test_backfill_mode_initializes_even_if_settings_flag_is_off(self):
        self.settings.initialize_existing_first = False
        self.catalog([dict(record(1), figure="Figure 1")])
        report = self.run_fixture(mode="backfill")
        self.assertEqual(report["bootstrap"]["status"], "complete")
        self.assertEqual((report["mode"], report["newCount"]), ("backfill", 0))

    def test_lost_ledger_does_not_recapture_a_larger_cohort(self):
        catalog = {"schemaVersion": 4, "authorCodeExecuted": False,
                   "papers": [materials(1, complete=True)], "inputSpecs": {}, "dimensions": {}}
        ledger = {}
        catalog["bootstrap"] = bootstrap_report(catalog, ledger, self.root, NOW)
        catalog["papers"].append(record(2))
        write_json(self.root / "data" / "catalog.json", catalog)
        before = (self.root / "data" / "catalog.json").read_bytes()
        for enabled in (True, False):
            self.settings.initialize_existing_first = enabled
            with self.subTest(enabled=enabled), self.assertRaisesRegex(ValueError, "ledger"):
                self.run_fixture()
            self.assertEqual((self.root / "data" / "catalog.json").read_bytes(), before)
            self.assertEqual(self.opener.methods, [])

    def test_interruption_saves_fixed_cohort_before_first_source_request(self):
        self.catalog([dict(record(1), figure="Figure 1")])
        def interruption(*args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_fixture(dict(self.active, lookup=interruption))
        ledger = read_json(self.root / "data" / "ledger.json")
        self.assertEqual(ledger["bootstrap"]["cohortDOIs"], [record(1)["doi"]])
        self.assertNotIn("bootstrap", self.current())
        catalog = self.current()
        catalog["papers"].append(record(2))
        write_json(self.root / "data" / "catalog.json", catalog)
        recovered = self.run_fixture()
        self.assertEqual(recovered["bootstrap"]["totalCount"], 1)
        self.assertEqual(recovered["bootstrap"]["status"], "complete")
        self.assertEqual(recovered["newCount"], 0)

    def test_bad_mode_types_stop_before_data_or_transport(self):
        self.catalog([record(1)])
        for mode in ([], {}, None, "unsupported"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "更新模式"):
                self.run_fixture(mode=mode)
        self.assertFalse((self.root / "data" / "ledger.json").exists())
        self.assertEqual(self.opener.methods, [])


if __name__ == "__main__":
    unittest.main()
