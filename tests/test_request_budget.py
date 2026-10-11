"""Offline cloud-budget regressions; no provider traffic or real credentials."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.pipeline import complete_summary, run_pipeline
from paper_plot_library.storage import read_json, write_json
from test_enrichment import ai_payload
from test_http import Response
from test_pipeline_audit import adapters, article_good, record, repo_good


class BudgetFixtureOpener:
    def __init__(self):
        self.methods = []

    def open(self, request, timeout):
        self.methods.append(request.get_method())
        payload = ai_payload(evidence_ids=["publication"]) if request.data is not None else {"ok": True}
        return Response(json.dumps(payload, ensure_ascii=False).encode(), url=request.full_url)


class RequestBudgetRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-budget-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(llm_api_key="offline-fixture-key", llm_model="offline-fixture-model",
                                 citation_enrichment=False)
        self.opener = BudgetFixtureOpener()

    def catalog(self, papers):
        write_json(self.root / "data" / "catalog.json", {
            "schemaVersion": 4, "authorCodeExecuted": False, "papers": papers,
            "inputSpecs": {}, "dimensions": {},
        })

    def client(self):
        return HttpClient(self.root / ".ppl" / "cache", self.settings.max_requests,
                          max_post_attempts=self.settings.max_ai_calls,
                          opener=self.opener, retries=1, sleep=lambda delay: None)

    def run_fixture(self, active, client=None, **kwargs):
        with patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, client or self.client(), active, **kwargs)

    def backlog(self, index=10):
        value = record(index)
        value.update(repository=repo_good(), article=article_good(), ai={"status": "error"})
        value["automation"] = {"status": "partial", "lastCheckedAt": "2026-10-11T10:48:24Z",
                               "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "error"},
                               "errors": ["ai: error"]}
        return value

    def test_145_collection_requests_leave_five_actual_ai_attempts(self):
        # Match the cloud work shape: five seed repairs, eight candidate checks.
        self.catalog([record(i) for i in range(100, 105)])
        def lookup(client, doi):
            for _ in range(11):
                client.get_json("https://api.github.com/offline-fixture", ttl=0)
            return {"status": "matched", "doi": doi}

        def discovery(client, settings):
            client.get_json("https://api.github.com/offline-fixture", ttl=0)
            return [record(i) for i in range(1, 9)]

        def metrics(client, paper, settings):
            if paper["doi"] == record(8)["doi"]:
                client.get_json("https://api.github.com/offline-fixture", ttl=0)
            return {"status": "missing_key"}

        active = adapters(lookup=lookup, discover=discovery, metrics=metrics)
        active.pop("summary")  # Exercise the real AI transport against the fake opener.
        report = self.run_fixture(active)
        self.assertEqual(report["checkedCount"], 13)
        self.assertEqual(report["newCount"], 5)
        self.assertEqual(report["reservedAiRequests"], 5)
        self.assertEqual(report["collectionRequests"], 145)
        self.assertEqual(report["aiAttempts"], 5)
        self.assertEqual(report["requests"], 150)
        self.assertTrue(report["collectionBudgetExhausted"])
        self.assertEqual(self.opener.methods.count("POST"), 5)
        papers = read_json(self.root / "data" / "catalog.json")["papers"]
        self.assertTrue(all(p["ai"]["status"] == "generated" for p in papers if p["id"].startswith("fixture-") and int(p["id"].split("-")[-1]) < 100))
        self.assertTrue(all(p["ai"]["status"] == "budget_exhausted" for p in papers[:5]))
        self.assertNotIn("error", report["stageCounts"]["ai"])

    def test_checked_ai_backlog_precedes_fresh_seed_and_new_case(self):
        self.settings.repair_limit = 1
        self.settings.max_ai_calls = 1
        self.settings.max_new_per_day = 1
        self.catalog([record(11), self.backlog(10)])
        active = adapters()
        active.pop("summary")
        report = self.run_fixture(active)
        papers = {p["id"]: p for p in read_json(self.root / "data" / "catalog.json")["papers"]}
        self.assertEqual(papers["fixture-10"]["ai"]["status"], "generated")
        self.assertEqual(papers["fixture-11"]["automation"]["status"], "seed")
        self.assertEqual(papers["fixture-1"]["ai"]["status"], "budget_exhausted")
        self.assertEqual((report["aiRepairCount"], report["aiAttempts"]), (1, 1))
        self.assertEqual(self.opener.methods, ["POST"])
        self.assertFalse(any(f["doi"] == record(10)["doi"] for f in report["failures"]))

    def test_backlog_not_summarized_after_failed_current_metadata_check(self):
        self.settings.repair_limit = 1
        self.catalog([self.backlog()])
        active = adapters(lookup=lambda *args: {"status": "metadata_unavailable"})
        active.pop("summary")
        report = self.run_fixture(active, discover_new=False)
        self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(report["aiRepairCount"], 0)
        self.assertEqual(self.opener.methods, [])

    def test_backlog_does_not_summarize_retained_repository_after_current_failure(self):
        self.settings.repair_limit = 1
        self.catalog([self.backlog()])
        active = adapters(repository=lambda *args: {"status": "unavailable", "scripts": []})
        active.pop("summary")
        report = self.run_fixture(active, discover_new=False)
        paper = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertTrue(paper["repository"].get("retained_from_previous"))
        self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(self.opener.methods, [])
        self.assertEqual(report["stageCounts"]["ai"], {"deferred_until_selection": 1})
        self.assertFalse(any(f.get("stage") == "ai" for f in report["failures"]))

    def test_backlog_does_not_pay_to_summarize_retained_article_captions(self):
        self.settings.repair_limit = 1
        self.catalog([self.backlog()])
        active = adapters(article=lambda *args: {"status": "unavailable", "figures": []})
        active.pop("summary")
        report = self.run_fixture(active, discover_new=False)
        paper = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertTrue(paper["article"].get("retained_from_previous"))
        self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(self.opener.methods, [])

    def test_no_final_article_defers_new_case_summary_without_paid_request(self):
        self.settings.repair_limit = 0
        self.catalog([])
        active = adapters(article=lambda *args: dict(article_good(), status="no_final_version"))
        active.pop("summary")
        report = self.run_fixture(active)
        paper = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertEqual(report["newCount"], 1)
        self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(paper["ai"]["status"], "missing_evidence")
        self.assertEqual(self.opener.methods, [])

    def test_old_failure_diagnostics_are_not_counted_when_stage_not_run_today(self):
        self.settings.repair_limit = 1
        old = self.backlog()
        old["ai"] = {"status": "stale", "last_attempt_status": "authentication_error"}
        old["metrics"] = {"status": "matched", "last_attempt_status": "budget_exhausted"}
        self.catalog([old])
        active = adapters(lookup=lambda *args: {"status": "metadata_unavailable"})
        active.pop("summary")
        report = self.run_fixture(active, discover_new=False)
        self.assertEqual(report["stageCounts"]["ai"], {"not_checked": 1})
        self.assertEqual(report["stageCounts"]["metrics"], {"not_checked": 1})
        self.assertFalse(any(f.get("stage") in {"ai", "metrics"} for f in report["failures"]))

    def test_generated_old_summary_is_marked_stale_when_source_recheck_fails(self):
        self.settings.repair_limit = 1
        old = self.backlog()
        old["ai"] = {"status": "generated", "summary_zh": "旧的学习注释", "http_status": 401}
        self.catalog([old])
        active = adapters(article=lambda *args: {"status": "unavailable", "figures": []})
        active.pop("summary")
        report = self.run_fixture(active, discover_new=False)
        paper = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertEqual(paper["ai"]["status"], "stale")
        self.assertEqual(paper["ai"]["summary_zh"], "旧的学习注释")
        self.assertEqual(paper["ai"]["last_attempt_status"], "missing_evidence")
        self.assertNotIn("http_status", paper["ai"])
        self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(report["stageCounts"]["ai"], {"deferred_until_selection": 1})

    def test_no_ai_configuration_does_not_hold_source_request_slots(self):
        self.settings.llm_api_key = ""
        self.settings.repair_limit = 0
        self.catalog([])
        report = self.run_fixture(adapters(discover=lambda *args: []))
        self.assertEqual(report["reservedAiRequests"], 0)
        self.assertEqual(report["aiAttempts"], 0)

    def test_tiny_budget_stops_collection_without_overspending_or_looping(self):
        self.settings.max_requests = 1
        self.settings.max_ai_calls = 5
        self.catalog([record(10)])
        report = self.run_fixture(adapters())
        self.assertEqual(report["reservedAiRequests"], 1)
        self.assertEqual(report["checkedCount"], 0)
        self.assertEqual(report["requests"], 0)
        self.assertEqual(report["discoveryStatus"], "budget_exhausted")
        self.assertTrue(report["collectionBudgetExhausted"])
        self.assertEqual(self.opener.methods, [])

    def test_successful_summary_retry_removes_old_ai_failure_only(self):
        value = self.backlog()
        value["automation"]["errors"].append("article: unavailable")
        complete_summary(self.client(), value, self.settings, adapters())
        self.assertEqual(value["automation"]["stages"]["ai"], "generated")
        self.assertEqual(value["automation"]["errors"], ["article: unavailable"])

    def test_retained_ai_and_metrics_report_current_attempt_not_old_success(self):
        self.settings.repair_limit = 1
        self.catalog([record(10)])
        active = adapters(
            metrics=lambda *args: {"status": "matched", "if": "12", "last_attempt_status": "budget_exhausted"},
            summary=lambda *args: {"status": "stale", "last_attempt_status": "authentication_error"},
        )
        report = self.run_fixture(active, discover_new=False)
        self.assertEqual(report["stageCounts"]["metrics"], {"budget_exhausted": 1})
        self.assertEqual(report["stageCounts"]["ai"], {"authentication_error": 1})
        self.assertTrue(any(f["stage"] == "ai" and "authentication_error" in f["error"] for f in report["failures"]))


if __name__ == "__main__":
    unittest.main()
