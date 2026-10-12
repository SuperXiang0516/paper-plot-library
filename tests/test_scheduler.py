"""Whole-catalog fair scheduling and bounded retry regressions, entirely offline."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.enrichment import summarize
from paper_plot_library.http import HttpClient
from paper_plot_library.pipeline import _repair_plan, _safe_repair_schedule, run_pipeline
from paper_plot_library.storage import read_json, write_json
from test_pipeline_audit import Client, adapters, article_good, record, repo_good
from test_request_budget import BudgetFixtureOpener


def checked_case(index, ai_status="invalid_response"):
    value = record(index)
    value.update(article=article_good(), repository=repo_good(), ai={"status": ai_status})
    value["automation"] = {
        "status": "partial", "lastCheckedAt": "2026-10-10T00:00:00Z",
        "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": ai_status},
        "errors": ["ai: invalid_response"] if ai_status == "invalid_response" else [],
    }
    return value


def repo_with_real_headers():
    value = repo_good()
    value["inputs"] = [{"path": "input.csv", "name": "input.csv", "fields": [{"name": "age"}]}]
    return value


class CatalogSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-scheduler-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(llm_api_key="offline-fixture-key", llm_model="offline-fixture-model",
                                 easyscholar_key="offline-metric-key", citation_enrichment=False,
                                 max_papers_per_run=20)
        self.clock = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    def catalog(self, papers):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4, "authorCodeExecuted": False,
                   "papers": papers, "inputSpecs": {}, "dimensions": {}})

    def papers(self):
        return read_json(self.root / "data" / "catalog.json")["papers"]

    def run_fixture(self, active=None, client=None, **kwargs):
        timestamp = self.clock.isoformat().replace("+00:00", "Z")
        self.clock += timedelta(seconds=1)
        defaults = adapters(repository=lambda *args: repo_with_real_headers(), metrics=lambda *args: {"status": "matched"})
        defaults.update(active or {})
        with patch("paper_plot_library.pipeline.build_site"), patch("paper_plot_library.pipeline.stamp", return_value=timestamp):
            return run_pipeline(self.root, self.settings, client or Client(), defaults, **kwargs)

    def test_fifty_seeds_progress_despite_seven_repeated_ai_failures(self):
        self.catalog([record(i) for i in range(1, 51)] + [checked_case(i) for i in range(100, 107)])
        visits = []
        def lookup(client, doi):
            visits.append(doi)
            return {"status": "matched", "doi": doi}
        def summary(client, paper, *args):
            return {"status": "invalid_response"} if int(paper["id"].split("-")[-1]) >= 100 else {"status": "generated"}
        reports = []
        for _ in range(18):
            result = self.run_fixture({"lookup": lookup, "summary": summary}, discover_new=False)
            reports.append(result)
            if result["untouchedRemainingCount"] == 0:
                break
            self.assertGreaterEqual(result["untouchedRepairCount"], 3)
            self.assertLessEqual(result["repairCount"], 5)
        self.assertEqual(reports[-1]["untouchedRemainingCount"], 0)
        self.assertEqual({doi for doi in visits if doi in {record(i)["doi"] for i in range(1, 51)}},
                         {record(i)["doi"] for i in range(1, 51)})
        self.assertTrue(any(r["cooldownSkippedCount"] for r in reports))
        self.assertEqual(sum(r["untouchedRepairCount"] for r in reports), 50)
        self.assertTrue(all(p["runPerformed"] is False for p in self.papers()))
        self.assertFalse(any(p.get("mappingReview", {}).get("approved") for p in self.papers()))

    def test_single_slot_alternates_retry_and_seed_lane(self):
        self.settings.repair_limit = 1
        self.catalog([record(1), record(2), checked_case(100)])
        first = self.run_fixture(discover_new=False)
        self.assertEqual((first["untouchedRepairCount"], first["retryRepairCount"]), (0, 1))
        second = self.run_fixture(discover_new=False)
        self.assertEqual((second["untouchedRepairCount"], second["retryRepairCount"]), (1, 0))
        self.assertEqual(second["untouchedRemainingCount"], 1)

    def test_repeated_error_cools_down_but_explicit_doi_can_recover(self):
        self.settings.repair_limit = 1
        self.catalog([checked_case(100)])
        failing = {"summary": lambda *args: {"status": "invalid_response"}}
        self.run_fixture(failing, discover_new=False)
        first = self.papers()[0]["automation"]["repairSchedule"]
        self.assertEqual(first["consecutiveFailures"], 1)
        self.assertNotIn("nextAttemptAt", first)
        self.run_fixture(failing, discover_new=False)
        second = self.papers()[0]["automation"]["repairSchedule"]
        self.assertEqual(second["consecutiveFailures"], 2)
        self.assertEqual(second["nextAttemptAt"], "2026-10-11T13:00:01Z")
        skipped = self.run_fixture(failing, discover_new=False)
        self.assertEqual((skipped["repairCount"], skipped["cooldownSkippedCount"]), (0, 1))
        recovered = self.run_fixture(discover_new=False, only_dois=[record(100)["doi"]])
        self.assertEqual(recovered["repairCount"], 1)
        self.assertEqual(self.papers()[0]["ai"]["status"], "generated")
        state = self.papers()[0]["automation"]["repairSchedule"]
        self.assertEqual(state["consecutiveFailures"], 0)
        self.assertNotIn("nextAttemptAt", state)

    def test_backoff_increases_and_is_bounded_at_twenty_four_hours(self):
        self.settings.repair_limit = 1
        self.catalog([checked_case(100)])
        for count in range(1, 10):
            self.run_fixture({"summary": lambda *args: {"status": "invalid_response"}},
                             discover_new=False, only_dois=[record(100)["doi"]])
            state = self.papers()[0]["automation"]["repairSchedule"]
            self.assertEqual(state["consecutiveFailures"], count)
            if count >= 2:
                start = datetime.fromisoformat(state["lastAttemptAt"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(state["nextAttemptAt"].replace("Z", "+00:00"))
                self.assertEqual((end - start).total_seconds() / 3600, min(24, 2 ** (count - 2)))

    def test_source_exception_is_persisted_and_not_retried_forever_same_run(self):
        self.catalog([record(1), record(2)])
        def failing(*args):
            raise RuntimeError("untrusted fixture message must not be persisted")
        first = self.run_fixture({"lookup": failing}, discover_new=False)
        second = self.run_fixture({"lookup": failing}, discover_new=False)
        third = self.run_fixture({"lookup": failing}, discover_new=False)
        self.assertEqual((first["checkedCount"], second["checkedCount"], third["checkedCount"]), (2, 2, 0))
        self.assertEqual(third["cooldownSkippedCount"], 2)
        self.assertNotIn("untrusted fixture", (self.root / "data" / "catalog.json").read_text())

    def test_source_failure_never_pays_for_retained_sources(self):
        self.catalog([checked_case(100)])
        paid = []
        result = self.run_fixture({"article": lambda *args: {"status": "unavailable", "figures": []},
                                   "summary": lambda *args: paid.append(1)}, discover_new=False)
        self.assertEqual(paid, [])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(self.papers()[0]["article"]["retained_from_previous"])

    def test_successful_auto_stages_are_completed_without_faking_manual_ready(self):
        self.catalog([record(1)])
        report = self.run_fixture(discover_new=False)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["automaticCompleteCount"], 1)
        self.assertEqual(report["incompleteCount"], 0)
        self.assertEqual(self.papers()[0]["automation"]["status"], "partial")

    def test_actual_ai_error_is_partial_not_automatic_complete(self):
        self.catalog([record(1)])
        report = self.run_fixture({"summary": lambda *args: {"status": "invalid_response"}}, discover_new=False)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["automaticCompleteCount"], 0)

    def test_missing_headers_or_reusable_images_have_gap_cooldown_not_api_error(self):
        self.catalog([record(1)])
        self.run_fixture({"repository": lambda *args: repo_good()}, discover_new=False)
        state = self.papers()[0]["automation"]["repairSchedule"]
        self.assertEqual(state["lastOutcome"], "automatic_complete")
        self.assertEqual(state["consecutiveFailures"], 0)
        self.assertEqual(state["nextAttemptAt"], "2026-10-12T12:00:00Z")

    def test_five_daily_admissions_survive_full_seed_queue_and_ledger_loss(self):
        self.catalog([record(i) for i in range(100, 150)])
        active = {"discover": lambda *args: [record(i) for i in range(1, 10)]}
        first = self.run_fixture(active)
        self.assertEqual((first["newCount"], first["dailyNewCount"]), (5, 5))
        self.assertEqual(first["untouchedRepairCount"], 5)
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        second = self.run_fixture(active)
        self.assertEqual((second["newCount"], second["dailyNewCount"]), (0, 5))
        self.assertEqual(second["discoveryStatus"], "daily_quota_reached")
        self.assertEqual(len(self.papers()), 55)
        disabled = self.run_fixture(active, discover_new=False)
        self.assertEqual(disabled["discoveryStatus"], "disabled")

    def test_tiny_total_budget_does_not_consume_seeds_or_paid_requests(self):
        self.catalog([record(i) for i in range(1, 51)] + [checked_case(100)])
        self.settings.max_requests = 1
        opener = BudgetFixtureOpener()
        client = HttpClient(self.root / ".ppl" / "cache", 1, max_post_attempts=5,
                            opener=opener, retries=1, sleep=lambda delay: None)
        report = self.run_fixture(client=client)
        self.assertEqual(report["checkedCount"], 0)
        self.assertEqual(report["untouchedRemainingCount"], 50)
        self.assertEqual(report["requests"], 0)
        self.assertEqual(report["aiAttempts"], 0)
        self.assertEqual(opener.methods, [])

    def test_budget_and_configuration_deferrals_are_not_failed_case_backoff(self):
        self.catalog([record(1)])
        self.run_fixture({"summary": lambda *args: {"status": "budget_exhausted"}}, discover_new=False)
        state = self.papers()[0]["automation"]["repairSchedule"]
        self.assertEqual(state["lastOutcome"], "deferred")
        self.assertEqual(state["consecutiveFailures"], 0)
        self.assertNotIn("nextAttemptAt", state)

    def test_schedule_persists_only_bounded_safe_fields(self):
        value = record(1)
        value["automation"]["repairSchedule"] = {
            "consecutiveFailures": True, "lastOutcome": "https://provider.invalid?secretKey=fixture",
            "nextAttemptAt": "bad", "lastAttemptAt": "2026-10-11T12:00:00Z", "providerMessage": "secret",
        }
        self.assertEqual(_safe_repair_schedule(value), {"lastAttemptAt": "2026-10-11T12:00:00Z"})
        plan, skipped = _repair_plan([value], 5, "2026-10-11T12:00:01Z", {}, set())
        self.assertEqual((len(plan), skipped), (1, 0))

    def test_malformed_outcome_and_unbounded_future_cooldowns_cannot_freeze_case(self):
        for outcome in ([], {}, True, 10):
            with self.subTest(outcome=outcome):
                value = checked_case(100)
                value["automation"]["repairSchedule"] = {
                    "lastOutcome": outcome, "lastAttemptAt": "2026-10-11T12:00:00Z",
                    "nextAttemptAt": "2099-01-01T00:00:00Z",
                }
                schedule = _safe_repair_schedule(value)
                self.assertNotIn("lastOutcome", schedule)
                self.assertNotIn("nextAttemptAt", schedule)
                plan, skipped = _repair_plan([value], 5, "2026-10-11T12:00:01Z", {}, {value["id"]})
                self.assertEqual((len(plan), skipped), (1, 0))
        future = checked_case(101)
        future["automation"]["repairSchedule"] = {
            "lastOutcome": "failure", "lastAttemptAt": "2099-01-01T00:00:00Z",
            "nextAttemptAt": "2099-01-01T01:00:00Z",
        }
        plan, skipped = _repair_plan([future], 5, "2026-10-11T12:00:01Z", {}, {future["id"]})
        self.assertEqual((len(plan), skipped), (1, 0))

    def test_cooldown_requires_anchor_and_accepts_only_zero_to_twenty_four_hours(self):
        value = checked_case(100)
        for last, upcoming, accepted in (
            ("", "2026-10-11T13:00:00Z", False),
            ("2026-10-11T12:00:00Z", "2026-10-11T11:00:00Z", False),
            ("2026-10-11T12:00:00Z", "2026-10-12T12:00:01Z", False),
            ("2026-10-11T12:00:00+00:00", "2026-10-12T12:00:00Z", True),
        ):
            with self.subTest(last=last, upcoming=upcoming):
                value["automation"]["repairSchedule"] = {"lastAttemptAt": last, "nextAttemptAt": upcoming}
                self.assertEqual("nextAttemptAt" in _safe_repair_schedule(value), accepted)

    def test_active_image_input_gaps_are_not_starved_by_constant_ai_backlog(self):
        gap = checked_case(99, ai_status="generated")
        gap["article"]["figures"] = []
        gap["automation"]["repairSchedule"] = {
            "lastAttemptAt": "2026-10-10T00:00:00Z", "lastOutcome": "automatic_complete",
        }
        newer_backlog = [checked_case(i) for i in range(100, 110)]
        for paper in newer_backlog:
            paper["automation"]["lastCheckedAt"] = "2026-10-10T01:00:00Z"
        plan, _ = _repair_plan([gap] + newer_backlog, 2, "2026-10-11T12:00:00Z", {},
                               {p["id"] for p in newer_backlog})
        self.assertEqual(plan[0]["id"], gap["id"])

    def test_daily_new_five_does_not_starve_original_fifty_ai_or_original_asset_gap(self):
        gap = checked_case(99, ai_status="generated")
        gap["article"]["figures"] = []
        gap["automation"]["repairSchedule"] = {
            "lastAttemptAt": "2026-10-10T00:00:00Z", "lastOutcome": "automatic_complete",
        }
        self.catalog([record(i) for i in range(1, 51)] + [gap] + [checked_case(100)])
        original_ids = {record(i)["id"] for i in range(1, 51)}
        reports = []
        for day in range(35):
            opener = BudgetFixtureOpener()
            client = HttpClient(self.root / ".ppl" / "cache", self.settings.max_requests,
                                max_post_attempts=5, opener=opener, retries=1, sleep=lambda delay: None)
            # A fresh five every mock UTC day; no source or AI adapter bypasses caps.
            report = self.run_fixture({"summary": summarize,
                "discover": lambda *args, offset=day: [record(1000 + offset * 5 + i) for i in range(5)]}, client=client)
            reports.append(report)
            self.assertEqual(report["newCount"], 5)
            self.assertEqual(report["dailyNewCount"], 5)
            self.assertLessEqual(report["aiAttempts"], 5)
            self.assertLessEqual(report["requests"], self.settings.max_requests)
            self.assertEqual(opener.methods.count("POST"), report["aiAttempts"])
            papers = {p["id"]: p for p in self.papers()}
            if day == 0:
                self.assertTrue(papers[gap["id"]]["article"]["figures"])
                self.assertTrue(papers[gap["id"]]["repository"]["inputs"][0]["fields"])
            if all(papers[identifier].get("ai", {}).get("status") == "generated" for identifier in original_ids):
                break
            self.clock += timedelta(days=1)
        self.assertTrue(all(papers[identifier].get("ai", {}).get("status") == "generated" for identifier in original_ids))
        self.assertEqual(reports[-1]["untouchedRemainingCount"], 0)
        self.assertEqual(sum(r["untouchedRepairCount"] for r in reports), 50)
        self.assertFalse(any(p.get("runPerformed") for p in papers.values()))

    def test_previous_round_same_second_does_not_pollute_stage_counts(self):
        old = checked_case(100, ai_status="authentication_error")
        old["automation"]["lastCheckedAt"] = self.clock.isoformat().replace("+00:00", "Z")
        self.catalog([record(1), old])
        self.settings.repair_limit = 1
        result = self.run_fixture(discover_new=False, only_dois=[record(1)["doi"]])
        self.assertEqual(result["stageCounts"]["ai"], {"generated": 1})
        self.assertFalse(any(f.get("doi") == old["doi"] for f in result["failures"]))


if __name__ == "__main__":
    unittest.main()
