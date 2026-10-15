"""Old material gaps never block visible daily additions; entirely offline."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.bootstrap import bootstrap_assessment, effective_collection_mode, ensure_bootstrap
from paper_plot_library.cloud_update import public_daily_summary, run_daily_session
from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.pipeline import run_pipeline
from paper_plot_library.site import has_publishable_body_figure
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_pipeline_audit import adapters, record
from test_request_budget import BudgetFixtureOpener


class DailyIndependenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ppl-independent-daily-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        asset = self.root / "data" / "assets" / "fixture.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(RASTER)
        self.settings = Settings(initialize_existing_first=True, require_complete_new_cases=True,
            repair_limit=5, candidate_checks=20, max_ai_calls=15, max_ai_attempts_per_paper=3,
            max_papers_per_run=5, citation_enrichment=False,
            llm_api_key="offline-fixture-key", llm_model="offline-fixture-model")
        old_attention = record(1000)
        old_attention["automation"]["bootstrap"] = {"status": "needs_attention",
            "attempts": 3, "unsuccessfulScans": 3, "reasonCodes": ["input_undocumented"]}
        self.old_papers = [old_attention, record(1001)]
        catalog = {"schemaVersion": 4, "authorCodeExecuted": False,
                   "papers": deepcopy(self.old_papers), "inputSpecs": {}, "dimensions": {}}
        ledger = {"days": {}, "candidates": {}}
        ensure_bootstrap(catalog, ledger, NOW)
        write_json(self.root / "data" / "catalog.json", catalog)
        write_json(self.root / "data" / "ledger.json", ledger)
        self.opener = BudgetFixtureOpener()
        self.scanned = []
        self.source = materials()
        def article(client, selected, *args):
            self.scanned.append(selected["doi"])
            if int(selected["doi"].rsplit("-", 1)[1]) <= 5:
                return {"status": "no_final_version", "figures": []}
            return deepcopy(self.source["article"])
        self.active = adapters(article=article,
            repository=lambda *args: deepcopy(self.source["repository"]),
            metrics=lambda *args: deepcopy(self.source["metrics"]),
            discover=lambda *args: [record(index) for index in range(1, 16)])
        self.active.pop("summary")

    def client(self, settings):
        return HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
            max_post_attempts=settings.max_ai_calls, opener=self.opener,
            retries=1, sleep=lambda delay: None)

    def run_daily(self, moment=NOW, **kwargs):
        with patch("paper_plot_library.pipeline.stamp", return_value=moment), \
                patch("paper_plot_library.pipeline.build_site"), patch("paper_plot_library.site.build_site"):
            return run_daily_session(self.root, self.settings, adapters=self.active,
                client_factory=self.client, now=lambda: moment, clock=lambda: 1000.0, **kwargs)

    def test_auto_and_daily_policy_never_depend_on_old_material_completeness(self):
        self.assertEqual(effective_collection_mode("auto"), "daily")
        self.assertEqual(effective_collection_mode("daily"), "daily")
        self.assertEqual(effective_collection_mode("backfill"), "backfill")
        for value in (None, [], {}, "unsupported", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                effective_collection_mode(value)

    def test_real_daily_replaces_failed_five_and_publishes_five_despite_old_attention(self):
        batches = []
        result = self.run_daily(batch_callback=lambda report, summary: batches.append(report))
        self.assertEqual((result["status"], result["endReason"], result["dailyNewCount"], result["batchCount"]),
                         ("finished", "target_reached", 5, 2))
        self.assertEqual([(item["repairCount"], item["newCount"]) for item in batches], [(0, 0), (0, 5)])
        self.assertTrue(all(item["mode"] == "daily" for item in batches))
        self.assertEqual(self.scanned, [record(index)["doi"] for index in range(1, 11)])
        self.assertEqual(self.opener.methods, ["POST"] * 5)
        self.assertEqual((batches[-1]["bootstrap"]["attentionCount"], batches[-1]["bootstrap"]["pendingCount"]), (1, 1))
        catalog = read_json(self.root / "data" / "catalog.json")
        self.assertEqual(catalog["papers"][:2], self.old_papers)
        for paper in catalog["papers"][2:]:
            self.assertTrue(has_publishable_body_figure(self.root, paper))
            self.assertEqual(bootstrap_assessment(paper, catalog["inputSpecs"][paper["id"]], self.root)["status"], "complete")
        before = len(self.scanned)
        repeated = self.run_daily()
        self.assertEqual((repeated["dailyNewCount"], repeated["batchCount"], repeated["aiAttempts"]), (5, 2, 5))
        self.assertEqual(len(self.scanned), before)

    def test_next_utc_day_gets_new_success_quota_not_a_stale_finished_session(self):
        self.run_daily()
        result = self.run_daily("2026-10-12T12:00:00Z")
        self.assertEqual((result["date"], result["endReason"], result["dailyNewCount"]),
                         ("2026-10-12", "target_reached", 5))
        ledger = read_json(self.root / "data" / "ledger.json")
        first, second = (set(ledger["days"][day]["newDOIs"]) for day in ("2026-10-11", "2026-10-12"))
        self.assertEqual((len(first), len(second), len(first & second)), (5, 5, 0))
        self.assertEqual(self.opener.methods, ["POST"] * 10)

    def test_no_collectable_candidate_does_not_claim_daily_target_completed(self):
        self.settings.session_max_batches = 1
        self.active["discover"] = lambda *args: [record(index) for index in range(1, 6)]
        result = self.run_daily()
        self.assertEqual((result["status"], result["endReason"], result["dailyNewCount"]),
                         ("paused", "batch_limit_reached", 0))
        self.assertEqual(self.opener.methods, [])

    def test_source_confirmed_orphan_is_not_rescanned_each_daily_batch(self):
        ledger = read_json(self.root / "data" / "ledger.json")
        ledger["candidates"][record(6)["doi"]] = {"status": "source_confirmed", "checkedAt": NOW}
        write_json(self.root / "data" / "ledger.json", ledger)
        self.settings.repair_limit = 0
        self.active["discover"] = lambda *args: [record(6), record(7)]
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), patch("paper_plot_library.pipeline.build_site"):
            result = run_pipeline(self.root, self.settings, self.client(self.settings), self.active, mode="auto")
        self.assertEqual((result["mode"], result["newCount"], result["checkedCount"]), ("daily", 1, 1))
        self.assertEqual(self.scanned, [record(7)["doi"]])

    def test_known_input_gap_rotates_out_for_seven_days_instead_of_starving_new_candidates(self):
        repository = self.active["repository"]
        def missing_input(*args):
            value = repository(*args)
            value["inputs"] = []
            return value
        self.active["repository"] = missing_input
        self.active["discover"] = lambda *args: [record(6)]
        self.assertEqual(self.run_daily()["dailyNewCount"], 0)
        state = read_json(self.root / "data" / "ledger.json")["candidates"][record(6)["doi"]]
        self.assertEqual(state["retryClass"], "materials")
        self.active["repository"] = repository
        count = len(self.scanned)
        self.assertEqual(self.run_daily("2026-10-12T12:00:00Z")["dailyNewCount"], 0)
        self.assertEqual(len(self.scanned), count)
        self.assertEqual(self.run_daily("2026-10-18T12:00:00Z")["dailyNewCount"], 1)

    def test_ai_failure_is_not_quarantined_for_a_week_after_provider_recovers(self):
        self.active["discover"] = lambda *args: [record(6)]
        self.active["summary"] = lambda *args: {"status": "invalid_response"}
        self.assertEqual(self.run_daily()["dailyNewCount"], 0)
        state = read_json(self.root / "data" / "ledger.json")["candidates"][record(6)["doi"]]
        self.assertEqual(state["retryClass"], "transient")
        self.active.pop("summary")
        self.assertEqual(self.run_daily("2026-10-12T12:00:00Z")["dailyNewCount"], 1)

    def test_metric_budget_skip_and_transient_body_download_are_not_week_long_material_failures(self):
        for kind in ("metrics_budget", "image_download"):
            with self.subTest(kind=kind):
                candidate = record(6 if kind == "metrics_budget" else 7)
                self.active["discover"] = lambda *args: [candidate]
                original_article, original_metrics = self.active["article"], self.active["metrics"]
                if kind == "metrics_budget":
                    self.active["metrics"] = lambda *args: {"status": "budget_exhausted"}
                else:
                    def missing_image(*args):
                        article = original_article(*args)
                        article["figures"][0].pop("asset", None)
                        article["figures"][0]["downloadStatus"] = "failed"
                        return article
                    self.active["article"] = missing_image
                moment = "2026-10-11T12:00:00Z" if kind == "metrics_budget" else "2026-10-13T12:00:00Z"
                self.run_daily(moment)
                state = read_json(self.root / "data" / "ledger.json")["candidates"][candidate["doi"]]
                self.assertEqual(state["retryClass"], "transient")
                self.active["article"], self.active["metrics"] = original_article, original_metrics
                next_day = "2026-10-12T12:00:00Z" if kind == "metrics_budget" else "2026-10-14T12:00:00Z"
                self.assertEqual(self.run_daily(next_day)["dailyNewCount"], 1)

    def test_daily_public_timestamps_are_optional_but_strict_and_date_bound(self):
        value = self.run_daily()
        self.assertEqual((value["startedAt"], value["updatedAt"]), (NOW, NOW))
        legacy = {key: item for key, item in value.items() if key not in {"startedAt", "updatedAt"}}
        self.assertEqual(public_daily_summary(legacy), legacy)
        for replacement in ({"startedAt": "DO_NOT_LEAK"}, {"updatedAt": "2026-02-30T12:00:00Z"},
                            {"startedAt": "2026-10-10T12:00:00Z"}, {"updatedAt": "2026-10-11T11:59:59Z"},
                            {"date": "2026-02-30"}, {"startedAt": "2026-10-11T12:00:00+08:00"}):
            with self.subTest(replacement=replacement):
                self.assertIsNone(public_daily_summary(dict(value, **replacement)))


if __name__ == "__main__":
    unittest.main()
