"""Daily target traversal with synthetic batch runners and no network requests."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.cloud_update import _count, run_daily_session
from paper_plot_library.bootstrap import bootstrap_assessment, ensure_bootstrap
from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.scope import assess_scope
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import RASTER
from test_bootstrap_pipeline import materials
from test_pipeline_audit import adapters, record
from test_request_budget import BudgetFixtureOpener


NOW = "2026-10-11T12:00:00Z"


class DailySessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-daily-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site_patch = patch("paper_plot_library.site.build_site")
        self.site_patch.start()
        self.addCleanup(self.site_patch.stop)
        self.settings = Settings(llm_api_key="offline-fixture", llm_model="offline-model",
            max_ai_calls=15, session_max_batches=12, session_max_ai_attempts=165,
            session_max_requests=1800, session_max_seconds=2700)
        write_json(self.root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [], "inputSpecs": {}})
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        self.calls = []
        self.time = 1000.0

    def run_fixture(self, runner, **kwargs):
        return run_daily_session(self.root, self.settings, batch_runner=runner,
            now=kwargs.pop("now", lambda: NOW), clock=lambda: self.time, **kwargs)

    def runner(self, admissions, requests=10, ai=3, candidates=3, fatal=None):
        values = iter(admissions)
        def batch(root, settings, client, adapters, discover_new, mode):
            count = next(values)
            self.calls.append(count)
            self.assertTrue(settings.require_complete_new_cases)
            self.assertEqual(settings.discovery_limit, 100)
            self.assertEqual(settings.repair_limit, 0)
            self.assertLessEqual(settings.max_papers_per_run, 5)
            self.assertLessEqual(settings.max_ai_calls, 15)
            self.assertLessEqual(settings.max_ai_attempts_per_paper, 3)
            self.assertTrue(discover_new)
            self.assertEqual(mode, "daily")
            self.assertAlmostEqual(client.session_deadline, self.time + 2700)
            catalog = read_json(root / "data" / "catalog.json")
            ledger = read_json(root / "data" / "ledger.json")
            stamp = NOW
            for _ in range(count):
                value = record(1000 + len(catalog["papers"]))
                value["admittedAt"] = stamp
                catalog["papers"].append(value)
                ledger.setdefault("days", {}).setdefault(stamp[:10], {"newDOIs": []})["newDOIs"].append(value["doi"])
            write_json(root / "data" / "catalog.json", catalog)
            write_json(root / "data" / "ledger.json", ledger)
            client.requests, client.post_attempts = requests, ai
            report = {"requests": requests, "aiAttempts": ai, "checkedCount": candidates + 2, "repairCount": 2}
            if fatal:
                report["aiStoppedStatus"] = fatal
            return report
        return batch

    def legacy_ledger(self):
        self.run_fixture(self.runner([1], requests=1, ai=1, candidates=0))
        ledger = read_json(self.root / "data" / "ledger.json")
        ledger["dailyCollectionSession"].update(batchCount=12, requests=667, aiAttempts=1,
            uncertainRequests=30, uncertainAiAttempts=2, elapsedSeconds=900,
            status="paused", endReason="batch_limit_reached")
        write_json(self.root / "data" / "ledger.json", ledger)
        return ledger

    def test_failed_candidates_are_not_counted_but_later_batches_reach_five(self):
        callbacks = []
        result = self.run_fixture(self.runner([0, 2, 3]), batch_callback=lambda report, summary: callbacks.append(summary))
        self.assertEqual(self.calls, [0, 2, 3])
        self.assertEqual((result["status"], result["endReason"], result["dailyNewCount"]), ("finished", "target_reached", 5))
        self.assertEqual((result["batchCount"], result["requests"], result["aiAttempts"]), (3, 30, 9))
        self.assertEqual(len(callbacks), 3)
        self.assertNotIn("inFlight", read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"])

    def test_same_day_finished_rerun_never_resets_budget_or_calls_runner(self):
        first = self.run_fixture(self.runner([3, 2]))
        def forbidden(*args, **kwargs):
            raise AssertionError("must not call after five durable admissions")
        second = self.run_fixture(forbidden)
        self.assertEqual((second["dailyNewCount"], second["requests"], second["aiAttempts"], second["batchCount"]),
                         (5, first["requests"], first["aiAttempts"], first["batchCount"]))

    def test_no_fresh_candidate_work_stops_instead_of_maintenance_loop(self):
        result = self.run_fixture(self.runner([0], candidates=0))
        self.assertEqual((result["status"], result["endReason"], result["batchCount"]), ("paused", "no_progress", 1))

    def test_fatal_and_callback_failure_save_before_stopping(self):
        result = self.run_fixture(self.runner([0], requests=1, ai=1, fatal="authentication_error"))
        self.assertEqual((result["endReason"], result["aiAttempts"]), ("ai_configuration_blocked", 1))
        def failed_callback(report, summary):
            saved = read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"]
            self.assertEqual(saved["requests"], 11)
            raise RuntimeError("DO_NOT_LEAK")
        resumed = self.run_fixture(self.runner([1]), batch_callback=failed_callback)
        self.assertEqual((resumed["endReason"], resumed["dailyNewCount"], resumed["requests"]), ("checkpoint_callback_failed", 1, 11))
        self.assertNotIn("DO_NOT_LEAK", str(resumed))

    def test_batch_exception_charges_unknown_reservation_and_same_day_resume_keeps_it(self):
        def failure(root, settings, **kwargs):
            saved = read_json(root / "data" / "ledger.json")["dailyCollectionSession"]
            self.assertEqual(saved["inFlight"]["reservedRequests"], 150)
            raise RuntimeError("private response DO_NOT_LEAK")
        result = self.run_fixture(failure)
        self.assertEqual((result["requests"], result["aiAttempts"], result["uncertainRequests"], result["uncertainAiAttempts"]), (0, 0, 150, 15))
        resumed = self.run_fixture(self.runner([5]))
        self.assertEqual((resumed["requests"], resumed["uncertainRequests"], resumed["uncertainAiAttempts"]), (10, 150, 15))
        self.assertNotIn("DO_NOT_LEAK", str(result))

    def test_interrupted_inflight_restores_conservative_usage_without_reset(self):
        self.run_fixture(self.runner([0], candidates=0))
        ledger = read_json(self.root / "data" / "ledger.json")
        state = ledger["dailyCollectionSession"]
        state["batchCount"] += 1
        state.update(status="active", endReason="")
        state["inFlight"] = {"startedAt": NOW, "reservedRequests": 150, "reservedAiAttempts": 15}
        write_json(self.root / "data" / "ledger.json", ledger)
        result = self.run_fixture(self.runner([5]))
        self.assertEqual((result["requests"], result["uncertainRequests"], result["uncertainAiAttempts"]), (20, 150, 15))

    def test_global_budget_binds_repeated_zero_admission_batches(self):
        self.settings.session_max_ai_attempts = 6
        result = self.run_fixture(self.runner([0, 0]))
        self.assertEqual((result["endReason"], result["aiAttempts"], result["batchCount"]), ("ai_budget_exhausted", 6, 2))

    def test_daily_thirty_six_batch_ceiling_allows_more_low_yield_candidates(self):
        self.settings.session_max_batches = 36
        result = self.run_fixture(self.runner([0] * 36, requests=1, ai=0))
        self.assertEqual((result["endReason"], result["batchCount"], result["maxBatches"]),
                         ("batch_limit_reached", 36, 36))
        self.assertEqual((len(self.calls), result["requests"], result["aiAttempts"]), (36, 36, 0))
        self.assertEqual((result["maxRequests"], result["maxAiAttempts"]), (1800, 165))
        self.assertEqual(read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"]["maxSeconds"], 2700)

    def test_legacy_twelve_batch_session_resumes_without_resetting_any_spent_budget(self):
        original = self.legacy_ledger()["dailyCollectionSession"]
        self.settings.session_max_batches = 36
        checkpoints = []
        def checkpoint(summary):
            saved = read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"]
            for field in ("requests", "aiAttempts", "uncertainRequests", "uncertainAiAttempts", "elapsedSeconds",
                          "maxRequests", "maxAiAttempts", "maxSeconds", "dailyNewCount", "startedAt"):
                self.assertEqual(saved[field], original[field], field)
            self.assertEqual((saved["batchCount"], saved["maxBatches"]), (13, 36))
            self.assertEqual(saved["inFlight"]["reservedRequests"], 150)
            checkpoints.append(summary)
        def no_fresh_work(root, settings, client, **kwargs):
            self.assertEqual(client.session_deadline, self.time + 2700 - 900)
            client.requests = client.post_attempts = 0
            return {"requests": 0, "aiAttempts": 0, "checkedCount": 0, "repairCount": 0}
        result = self.run_fixture(no_fresh_work, batch_start_callback=checkpoint)
        self.assertEqual((len(checkpoints), result["batchCount"], result["endReason"]), (1, 13, "no_progress"))
        self.assertEqual((result["requests"], result["aiAttempts"], result["uncertainRequests"],
                          result["uncertainAiAttempts"], result["dailyNewCount"]), (667, 1, 30, 2, 1))

    def test_batch_migration_does_not_raise_saved_network_paid_or_time_ceiling(self):
        original = self.legacy_ledger()
        self.settings.session_max_batches = 36
        for changes, reason in (({"maxRequests": 697}, "http_budget_exhausted"),
                                ({"maxAiAttempts": 3}, "ai_budget_exhausted"),
                                ({"maxSeconds": 900}, "time_limit_reached")):
            with self.subTest(changes=changes):
                ledger = deepcopy(original)
                ledger["dailyCollectionSession"].update(changes)
                write_json(self.root / "data" / "ledger.json", ledger)
                result = self.run_fixture(lambda *args, **kwargs: self.fail("exhausted budget cannot collect"),
                    client_factory=lambda *args: self.fail("exhausted budget cannot create a client"))
                self.assertEqual((result["batchCount"], result["maxBatches"], result["endReason"]), (12, 36, reason))
                saved = read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"]
                for field in ("requests", "aiAttempts", "uncertainRequests", "uncertainAiAttempts", "elapsedSeconds",
                              "maxRequests", "maxAiAttempts", "maxSeconds", "dailyNewCount"):
                    self.assertEqual(saved[field], ledger["dailyCollectionSession"][field], field)

    def test_lower_configured_or_custom_saved_batch_cap_is_not_migrated_upward(self):
        original = self.legacy_ledger()
        for configured, saved_limit, batches in ((6, 12, 12), (36, 6, 6), (36, 20, 20)):
            with self.subTest(configured=configured, saved_limit=saved_limit):
                self.settings.session_max_batches = configured
                ledger = deepcopy(original)
                ledger["dailyCollectionSession"].update(maxBatches=saved_limit, batchCount=batches)
                write_json(self.root / "data" / "ledger.json", ledger)
                result = self.run_fixture(lambda *args, **kwargs: self.fail("effective batch ceiling already spent"),
                    client_factory=lambda *args: self.fail("no client after effective batch ceiling"))
                self.assertEqual((result["batchCount"], result["maxBatches"], result["endReason"]),
                                 (batches, saved_limit, "batch_limit_reached"))

    def test_scope_change_recounts_qualified_admissions_without_refunding_any_cost(self):
        original = self.legacy_ledger()["dailyCollectionSession"]
        catalog = read_json(self.root / "data" / "catalog.json")
        catalog["papers"][0]["paper"] = "Marine biodiversity sequencing atlas"
        write_json(self.root / "data" / "catalog.json", catalog)
        self.settings.research_scope = "aging-sequencing-metabolic"
        self.settings.session_max_batches = 36
        seen = []
        def eligible_batch(root, settings, client, **kwargs):
            saved = read_json(root / "data" / "ledger.json")["dailyCollectionSession"]
            self.assertEqual(saved["dailyNewCount"], 0)
            self.assertEqual((saved["requests"], saved["aiAttempts"]), (original["requests"], original["aiAttempts"]))
            current = read_json(root / "data" / "catalog.json")
            ledger = read_json(root / "data" / "ledger.json")
            for index in range(1, 6):
                paper = record(2000 + index)
                paper.update(paper="Long-read sequencing of human aging", admittedAt=NOW)
                paper["researchScope"] = assess_scope(paper, source="verified-metadata")
                current["papers"].append(paper)
                ledger["days"][NOW[:10]]["newDOIs"].append(paper["doi"])
            write_json(root / "data" / "catalog.json", current)
            write_json(root / "data" / "ledger.json", ledger)
            client.requests = client.post_attempts = 5
            seen.append(True)
            return {"requests": 5, "aiAttempts": 5, "checkedCount": 5, "repairCount": 0}
        result = self.run_fixture(eligible_batch)
        self.assertEqual((result["endReason"], result["dailyNewCount"], result["batchCount"], len(seen)),
                         ("target_reached", 5, 13, 1))
        self.assertEqual((result["requests"], result["aiAttempts"], result["uncertainRequests"],
                          result["uncertainAiAttempts"]), (672, 6, 30, 2))
        ledger = read_json(self.root / "data" / "ledger.json")
        self.assertEqual(len(ledger["days"][NOW[:10]]["newDOIs"]), 6, "raw admission history is not deleted")
        repeated = self.run_fixture(lambda *args, **kwargs: self.fail("five qualified admissions already durable"))
        self.assertEqual((repeated["requests"], repeated["dailyNewCount"], repeated["batchCount"]), (672, 5, 13))

    def test_scoped_count_uses_only_current_qualified_papers_not_raw_history_size(self):
        papers = [dict(record(1), paper="Human aging long-read sequencing", admittedAt=NOW)]
        papers[0]["researchScope"] = assess_scope(papers[0], source="verified-metadata")
        history = [record(index)["doi"] for index in range(1, 8)]
        catalog = {"papers": papers}
        ledger = {"days": {NOW[:10]: {"newDOIs": history}}}
        self.assertEqual(_count(catalog, ledger, NOW[:10], "aging-sequencing-metabolic"), 1)
        with self.assertRaises(ValueError):
            _count(catalog, ledger, NOW[:10])

    def test_five_title_only_legacy_admissions_cannot_skip_verified_scope_work(self):
        self.run_fixture(self.runner([5], requests=5, ai=5))
        current = read_json(self.root / "data" / "catalog.json")
        for paper in current["papers"]:
            paper["paper"] = "Human aging long-read sequencing"
        write_json(self.root / "data" / "catalog.json", current)
        self.settings.research_scope = "aging-sequencing-metabolic"
        checked = []
        def canonical_exclusion(root, settings, client, **kwargs):
            state = read_json(root / "data" / "ledger.json")["dailyCollectionSession"]
            self.assertEqual((state["dailyNewCount"], state["requests"], state["aiAttempts"]), (0, 5, 5))
            catalog = read_json(root / "data" / "catalog.json")
            for paper in catalog["papers"]:
                paper["paper"] = "Marine biodiversity sequencing atlas"
                paper["researchScope"] = assess_scope(paper, source="verified-metadata")
            write_json(root / "data" / "catalog.json", catalog)
            checked.append(True)
            client.requests, client.post_attempts = 1, 0
            return {"requests": 1, "aiAttempts": 0, "checkedCount": 0, "repairCount": 0}
        result = self.run_fixture(canonical_exclusion)
        self.assertEqual((len(checked), result["endReason"], result["dailyNewCount"], result["batchCount"]),
                         (1, "no_progress", 0, 2))
        self.assertEqual((result["requests"], result["aiAttempts"]), (6, 5))

    def test_soft_deadline_stops_between_batches_and_is_passed_to_transport(self):
        base = self.runner([0])
        def expired(*args, **kwargs):
            result = base(*args, **kwargs)
            self.time += 2701
            return result
        result = self.run_fixture(expired)
        self.assertEqual((result["endReason"], result["batchCount"]), ("time_limit_reached", 1))

    def test_failed_discovery_is_not_misreported_as_no_candidates(self):
        base = self.runner([0], candidates=0)
        def failed(*args, **kwargs):
            return dict(base(*args, **kwargs), discoveryStatus="partial_failure")
        result = self.run_fixture(failed)
        self.assertEqual((result["status"], result["dailyNewCount"], result["endReason"]),
                         ("paused", 0, "discovery_failed"))

    def test_actual_client_usage_mismatch_is_unknown_not_false_zero(self):
        base = self.runner([0])
        def mismatch(*args, **kwargs):
            result = base(*args, **kwargs)
            result["requests"] = 0
            result["aiAttempts"] = 0
            return result
        result = self.run_fixture(mismatch)
        self.assertEqual((result["endReason"], result["requests"], result["uncertainRequests"]), ("batch_failed", 0, 150))

    def test_batch_start_checkpoint_precedes_client_creation_and_paid_requests(self):
        events = []
        def checkpoint(summary):
            saved = read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"]
            self.assertEqual((saved["batchCount"], saved["requests"], saved["aiAttempts"]), (1, 0, 0))
            self.assertEqual(saved["inFlight"]["reservedAiAttempts"], 15)
            self.assertEqual(summary["inFlightCount"], 1)
            events.append("reservation-uploaded")
        def factory(settings):
            self.assertEqual(events, ["reservation-uploaded"])
            events.append("client-created")
            return HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
                              max_post_attempts=settings.max_ai_calls)
        base = self.runner([5])
        def batch(*args, **kwargs):
            self.assertEqual(events, ["reservation-uploaded", "client-created"])
            events.append("batch-called")
            return base(*args, **kwargs)
        result = self.run_fixture(batch, client_factory=factory, batch_start_callback=checkpoint)
        self.assertEqual(events, ["reservation-uploaded", "client-created", "batch-called"])
        self.assertEqual(result["endReason"], "target_reached")

    def test_failed_batch_start_checkpoint_keeps_unknown_reservation_without_any_call(self):
        def failure(summary):
            raise RuntimeError("DO_NOT_LEAK private upstream push error")
        result = self.run_fixture(lambda *args, **kwargs: self.fail("no batch before upstream reservation"),
            client_factory=lambda *args: self.fail("no client before upstream reservation"),
            batch_start_callback=failure)
        self.assertEqual((result["status"], result["endReason"], result["requests"], result["aiAttempts"],
                          result["uncertainRequests"], result["uncertainAiAttempts"], result["inFlightCount"]),
                         ("paused", "checkpoint_callback_failed", 0, 0, 0, 0, 1))
        self.assertEqual(read_json(self.root / "data" / "catalog.json")["dailySession"], result)
        self.assertNotIn("DO_NOT_LEAK", str(result))
        resumed = self.run_fixture(self.runner([5]))
        self.assertEqual((resumed["uncertainRequests"], resumed["uncertainAiAttempts"], resumed["aiAttempts"]), (150, 15, 3))

    def test_batch_start_checkpoint_time_does_not_extend_transport_deadline(self):
        def delayed(summary):
            self.time += 100
        def batch(root, settings, client, **kwargs):
            self.assertEqual(client.session_deadline, 3700.0)
            client.requests = 0
            client.post_attempts = 0
            return {"requests": 0, "aiAttempts": 0, "checkedCount": 0, "repairCount": 0}
        result = self.run_fixture(batch, batch_start_callback=delayed)
        self.assertEqual((result["endReason"], result["batchCount"]), ("no_progress", 1))
        self.assertEqual(read_json(self.root / "data" / "ledger.json")["dailyCollectionSession"]["elapsedSeconds"], 100)

    def test_malformed_or_future_saved_budget_fails_closed(self):
        self.run_fixture(self.runner([0], candidates=0))
        self.settings.session_max_batches = 36
        original = read_json(self.root / "data" / "ledger.json")
        for mutate in (lambda state: state.update(requests=True), lambda state: state.update(date="2026-10-12", startedAt="2026-10-12T12:00:00Z"),
                       lambda state: state.update(response="DO_NOT_LEAK"), lambda state: state.update(requests=1801),
                       lambda state: state.update(uncertainRequests=0, uncertainAiAttempts=1),
                       lambda state: state.update(status="finished", endReason="no_progress"),
                       lambda state: state.update(maxBatches=37),
                       lambda state: state.update(maxBatches=True),
                       lambda state: state.update(maxBatches=36, batchCount=37),
                       lambda state: state.update(batchCount=state["maxBatches"] + 1)):
            from copy import deepcopy
            ledger = deepcopy(original)
            mutate(ledger["dailyCollectionSession"])
            write_json(self.root / "data" / "ledger.json", ledger)
            before = (self.root / "data" / "ledger.json").read_text()
            with self.assertRaises(ValueError):
                self.run_fixture(self.runner([5]))
            self.assertEqual((self.root / "data" / "ledger.json").read_text(), before)

    def test_new_utc_day_resets_daily_session_but_not_prior_quota_records(self):
        self.run_fixture(self.runner([0], candidates=0))
        seen = []
        def next_day(root, settings, client, **kwargs):
            state = read_json(root / "data" / "ledger.json")["dailyCollectionSession"]
            seen.append((state["date"], state["requests"], state["batchCount"]))
            client.requests = 1
            client.post_attempts = 0
            return {"requests": 1, "aiAttempts": 0, "checkedCount": 0, "repairCount": 0}
        result = self.run_fixture(next_day, now=lambda: "2026-10-12T12:00:00Z")
        self.assertEqual(seen, [("2026-10-12", 0, 1)])
        self.assertEqual(result["requests"], 1)

    def test_pending_initialization_does_not_block_daily_admissions(self):
        self.settings.initialize_existing_first = True
        write_json(self.root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [record(1)], "inputSpecs": {}})
        result = self.run_fixture(self.runner([5]))
        self.assertEqual((result["endReason"], result["batchCount"], result["dailyNewCount"]), ("target_reached", 1, 5))

    def test_incomplete_later_admission_does_not_block_the_next_daily_work(self):
        asset = self.root / "data" / "assets" / "fixture.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(RASTER)
        catalog = {"authorCodeExecuted": False, "papers": [materials(1, complete=True)], "inputSpecs": {}}
        ledger = {"days": {}, "candidates": {}}
        ensure_bootstrap(catalog, ledger, NOW)
        catalog["papers"].append(record(2))
        write_json(self.root / "data" / "catalog.json", catalog)
        write_json(self.root / "data" / "ledger.json", ledger)
        result = self.run_fixture(self.runner([5]))
        self.assertEqual((result["endReason"], result["batchCount"], result["dailyNewCount"]), ("target_reached", 1, 5))

    def test_legacy_material_regression_label_cannot_reenable_daily_blocking(self):
        base = self.runner([0], candidates=0)
        def regressed(*args, **kwargs):
            return dict(base(*args, **kwargs), discoveryStatus="existing_materials_pending")
        result = self.run_fixture(regressed)
        self.assertEqual((result["endReason"], result["batchCount"]), ("no_progress", 1))

    def test_real_pipeline_rejects_first_five_then_admits_five_and_stops_same_day(self):
        # Real inspection/admission/AI validation and actual HttpClient accounting,
        # using only temporary materials and a transport that never opens a socket.
        asset = self.root / "data" / "assets" / "fixture.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(RASTER)
        self.settings.repair_limit = 0
        self.settings.citation_enrichment = False
        opener = BudgetFixtureOpener()
        source = materials()
        scanned = []
        discovered = []
        def article(client, selected, *args):
            scanned.append(selected["doi"])
            if int(selected["doi"].rsplit("-", 1)[1]) <= 5:
                return dict(deepcopy(source["article"]), status="no_final_version", figures=[])
            return deepcopy(source["article"])
        active = adapters(article=article,
            repository=lambda *args: deepcopy(source["repository"]),
            metrics=lambda *args: deepcopy(source["metrics"]),
            discover=lambda *args: discovered.append(True) or [record(index) for index in range(1, 11)])
        active.pop("summary")
        def client_factory(settings):
            return HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
                max_post_attempts=settings.max_ai_calls, opener=opener, retries=1, sleep=lambda delay: None)
        callbacks = []
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), patch("paper_plot_library.pipeline.build_site"):
            result = run_daily_session(self.root, self.settings, adapters=active, client_factory=client_factory,
                now=lambda: NOW, clock=lambda: self.time,
                batch_callback=lambda report, summary: callbacks.append((report["newCount"], report["aiAttempts"])))
            repeat = run_daily_session(self.root, self.settings, adapters=active, client_factory=client_factory,
                now=lambda: NOW, clock=lambda: self.time)
        self.assertEqual((result["status"], result["endReason"], result["dailyNewCount"], result["batchCount"]),
                         ("finished", "target_reached", 5, 2))
        self.assertEqual(callbacks, [(0, 0), (5, 5)])
        self.assertEqual(len(scanned), 10)
        self.assertEqual(len(set(scanned)), 10)
        self.assertEqual(len(discovered), 2)
        self.assertEqual(opener.methods, ["POST"] * 5)
        self.assertEqual((result["aiAttempts"], repeat["aiAttempts"], repeat["dailyNewCount"], repeat["batchCount"]), (5, 5, 5, 2))
        catalog = read_json(self.root / "data" / "catalog.json")
        ledger = read_json(self.root / "data" / "ledger.json")
        reports = read_json(self.root / "data" / "updates.json")
        self.assertEqual({paper["doi"] for paper in catalog["papers"]}, {record(index)["doi"] for index in range(6, 11)})
        self.assertEqual(len(ledger["days"][NOW[:10]]["newDOIs"]), 5)
        self.assertTrue(all(bootstrap_assessment(paper, catalog["inputSpecs"][paper["id"]], self.root)["status"] == "complete"
                            for paper in catalog["papers"]))
        self.assertEqual(catalog["dailySession"], repeat)
        self.assertEqual(reports[0]["dailySession"], repeat)


if __name__ == "__main__":
    unittest.main()
