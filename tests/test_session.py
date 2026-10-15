"""Finite existing-case passes, durable resume and true material counts; no network."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.session import run_backfill_session, session_summary
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_http import Clock, Opener, Response
from test_enrichment import ai_payload
from test_pipeline_audit import adapters, record


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-offline-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.clock = Clock()
        self.settings = Settings(initialize_existing_first=True, max_ai_calls=15,
            llm_api_key="DO_NOT_LEAK", llm_model="offline-fixture", citation_enrichment=False)
        self.selected = []
        self.clients = []
        self.ai_per_paper = 3
        self.done_materials = False
        self.build = patch("paper_plot_library.site.build_site")
        self.build.start()
        self.addCleanup(self.build.stop)
        self.catalog([record(index) for index in range(1, 56)])

    def catalog(self, papers):
        write_json(self.root / "data" / "catalog.json", {"schemaVersion": 4,
            "authorCodeExecuted": False, "papers": papers, "inputSpecs": {}, "dimensions": {}})

    def factory(self, settings):
        events = [Response(url="https://api.github.com/offline") for _ in range(150)]
        client = HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
            max_post_attempts=settings.max_ai_calls, opener=Opener(events), sleep=self.clock.sleep, clock=self.clock.now)
        self.clients.append(client)
        return client

    def batch(self, root, settings, client, adapters, only_dois, discover_new, mode, session_dois):
        self.assertFalse(discover_new)
        self.assertEqual(mode, "backfill")
        self.assertEqual(only_dois, session_dois)
        self.assertLessEqual(len(session_dois), 5)
        self.assertLessEqual(settings.max_ai_calls, 15)
        self.assertLessEqual(settings.max_ai_attempts_per_paper, 3)
        self.assertLessEqual(settings.max_requests, 150)
        current = read_json(root / "data" / "catalog.json")
        checked = []
        for paper in current["papers"]:
            if paper["doi"] not in session_dois:
                continue
            client.get_json("https://api.github.com/offline", ttl=0)
            for _ in range(min(self.ai_per_paper, settings.max_ai_calls - client.post_attempts)):
                client.post_json("https://api.deepseek.com/chat/completions", {"synthetic": True}, attempt_limit=1)
            self.selected.append(paper["doi"])
            checked.append(paper["doi"])
            if self.done_materials:
                paper.update(materials(int(paper["id"].split("-")[-1]), complete=True))
            else:
                paper["automation"] = {"lastCheckedAt": NOW, "status": "pending", "errors": [],
                    "stages": {"metadata": "error"}}
        write_json(root / "data" / "catalog.json", current)
        report = {"startedAt": NOW, "checkedCount": len(checked), "checkedDOIs": checked,
            "requests": client.requests, "aiAttempts": client.post_attempts, "newCount": 0}
        reports = read_json(root / "data" / "updates.json", [])
        write_json(root / "data" / "updates.json", [report] + reports)
        return report

    def run_session(self, **kwargs):
        return run_backfill_session(self.root, self.settings, client_factory=self.factory,
            batch_runner=kwargs.pop("batch_runner", self.batch), now=lambda: NOW,
            clock=self.clock.now, **kwargs)

    def state(self):
        return read_json(self.root / "data" / "ledger.json")["backfillSession"]

    def test_fifty_five_existing_cases_traverse_eleven_five_paper_batches_without_false_complete(self):
        callbacks = []
        result = self.run_session(batch_callback=lambda report, summary: callbacks.append(deepcopy(summary)))
        self.assertEqual((result["status"], result["endReason"], result["batchCount"], result["processedCount"]),
                         ("finished", "cohort_traversed", 11, 55))
        self.assertEqual((result["requests"], result["aiAttempts"], result["uncertainRequests"]), (220, 165, 0))
        self.assertEqual((result["completeCount"], result["pendingCount"], result["materialsStatus"]), (0, 55, "active"))
        self.assertEqual(len(self.selected), len(set(self.selected)))
        self.assertEqual(len(callbacks), 11)
        self.assertEqual([value["processedCount"] for value in callbacks], list(range(5, 56, 5)))
        self.assertEqual((callbacks[-1]["status"], callbacks[-1]["endReason"]), ("finished", "cohort_traversed"))
        self.assertTrue(all(value["localCheckpointSaved"] for value in callbacks))
        self.assertEqual(self.state()["cohortDOIs"], [record(i)["doi"] for i in range(1, 56)])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(self.state()))
        self.assertEqual(len(read_json(self.root / "data" / "catalog.json")["papers"]), 55)

    def test_original_bootstrap_fifty_is_frozen_while_extra_five_are_traversed(self):
        original = {"version": 1, "cohortDOIs": [record(i)["doi"] for i in range(1, 51)], "startedAt": NOW, "status": "active"}
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}, "bootstrap": original})
        result = self.run_session()
        self.assertEqual(result["processedCount"], 55)
        self.assertIn(record(55)["doi"], self.selected)
        self.assertEqual(read_json(self.root / "data" / "ledger.json")["bootstrap"], original)

    def test_strictly_complete_cases_are_free_and_do_not_need_manual_binding_approval(self):
        self.catalog([materials(1, complete=True), record(2)])
        self.done_materials = True
        result = self.run_session()
        self.assertEqual((result["targetCount"], result["processedCount"], result["completeCount"], result["materialsStatus"]),
                         (2, 1, 2, "complete"))
        self.assertEqual(self.selected, [record(2)["doi"]])
        self.assertEqual(result["aiAttempts"], 3)

    def test_callback_failure_preserves_saved_material_and_resumes_only_remaining_papers(self):
        def fail(report, summary):
            saved = self.state()
            self.assertEqual(len(saved["processedDOIs"]), 5)
            raise RuntimeError("DO_NOT_LEAK external push error")
        first = self.run_session(batch_callback=fail)
        self.assertEqual((first["status"], first["endReason"], first["processedCount"]),
                         ("paused", "checkpoint_callback_failed", 5))
        final = self.run_session()
        self.assertEqual((final["status"], final["processedCount"], final["batchCount"]), ("finished", 55, 11))
        self.assertEqual(len(self.selected), len(set(self.selected)))
        self.assertEqual(final["aiAttempts"], 165)

    def test_batch_start_callback_persists_cloud_reservation_before_any_collection_or_ai(self):
        before, after = [], []
        def checkpoint(summary):
            saved = self.state()
            self.assertEqual(len(saved["inFlight"]["dois"]), 5)
            self.assertEqual(saved["inFlight"]["reservedAiAttempts"], 15)
            self.assertEqual(saved["inFlight"]["reservedRequests"], 150)
            self.assertEqual((saved["requests"], saved["aiAttempts"]),
                             (len(after) * 20, len(after) * 15))
            self.assertEqual(len(self.clients), len(after))
            before.append(deepcopy(summary))
        result = self.run_session(batch_start_callback=checkpoint,
            batch_callback=lambda report, summary: after.append(deepcopy(summary)))
        self.assertEqual((len(before), len(after), result["processedCount"]), (11, 11, 55))
        self.assertTrue(all(summary["inFlightCount"] == 5 for summary in before))
        self.assertEqual(after[-1]["status"], "finished")

    def test_failed_batch_start_checkpoint_preserves_reservation_but_never_claims_actual_paid_use(self):
        def unavailable(summary):
            raise RuntimeError("DO_NOT_LEAK push failed before data transport")
        result = self.run_session(batch_start_callback=unavailable)
        self.assertEqual((result["status"], result["endReason"], result["batchCount"]), ("paused", "checkpoint_callback_failed", 1))
        self.assertEqual((result["requests"], result["aiAttempts"], result["uncertainRequests"], result["uncertainAiAttempts"]), (0, 0, 0, 0))
        self.assertEqual(result["inFlightCount"], 5)
        self.assertIn("inFlight", self.state())
        self.assertEqual(self.clients, [])
        self.assertEqual(self.selected, [])
        reservation_dois = set(self.state()["plannedDOIs"])
        resumed = self.run_session()
        self.assertEqual((resumed["processedCount"], resumed["uncertainAiAttempts"], resumed["aiAttempts"]), (50, 15, 150))
        self.assertTrue(set(self.selected).isdisjoint(reservation_dois))
        self.assertNotIn("DO_NOT_LEAK", json.dumps(self.state()))

    def test_cloud_saved_reservation_survives_hard_cancel_and_is_recovered_after_local_state_restore(self):
        pushed = {}
        def push_then_cancel(summary):
            pushed["ledger"] = read_json(self.root / "data" / "ledger.json")
            pushed["catalog"] = read_json(self.root / "data" / "catalog.json")
            raise SystemExit("simulated cloud hard cancellation before HTTP")
        with self.assertRaises(SystemExit):
            self.run_session(batch_start_callback=push_then_cancel)
        self.assertEqual((pushed["ledger"]["backfillSession"]["requests"], pushed["ledger"]["backfillSession"]["aiAttempts"]), (0, 0))
        self.assertIn("inFlight", pushed["ledger"]["backfillSession"])
        write_json(self.root / "data" / "ledger.json", pushed["ledger"])
        write_json(self.root / "data" / "catalog.json", pushed["catalog"])
        result = self.run_session()
        self.assertEqual((result["uncertainRequests"], result["uncertainAiAttempts"], result["processedCount"]), (150, 15, 50))
        self.assertEqual(result["plannedCount"], 55)
        self.assertEqual(result["endReason"], "no_eligible_cases")

    def test_batch_start_git_push_time_is_included_in_absolute_session_deadline(self):
        self.settings.session_max_seconds = 60
        def slow_push(summary):
            self.clock.value += 61
        result = self.run_session(batch_start_callback=slow_push)
        self.assertEqual((result["endReason"], result["processedCount"], result["requests"], result["aiAttempts"]),
                         ("time_limit_reached", 0, 0, 0))
        self.assertEqual(result["inFlightCount"], 5)
        self.assertEqual(self.clients, [])

    def test_remaining_socket_timeout_after_pre_push_does_not_restart_the_time_budget(self):
        self.catalog([record(1)])
        self.ai_per_paper = 0
        self.settings.session_max_seconds = 60
        def push(summary):
            self.clock.value += 55
        result = self.run_session(batch_start_callback=push)
        self.assertEqual(result["processedCount"], 1)
        self.assertEqual(self.clients[0].opener.calls[0][1], 5)

    def test_interrupted_batch_accounts_unknown_budget_and_does_not_repeat_claimed_cases(self):
        def interrupted(*args, **kwargs):
            current = read_json(self.root / "data" / "catalog.json")
            current["papers"][0]["automation"]["lastCheckedAt"] = NOW
            write_json(self.root / "data" / "catalog.json", current)
            raise KeyboardInterrupt()
        result = self.run_session(batch_runner=interrupted)
        self.assertEqual((result["status"], result["endReason"], result["processedCount"]), ("paused", "interrupted", 1))
        self.assertEqual((result["requests"], result["aiAttempts"], result["uncertainRequests"], result["uncertainAiAttempts"]), (0, 0, 150, 15))
        interrupted_claims = set(self.state()["plannedDOIs"])
        final = self.run_session()
        self.assertEqual(final["processedCount"], 51)
        self.assertEqual(final["plannedCount"], 55)
        self.assertEqual(final["endReason"], "no_eligible_cases")
        self.assertEqual(final["uncertainAiAttempts"] + final["aiAttempts"], 165)
        self.assertTrue(set(self.selected).isdisjoint(interrupted_claims))

    def test_inflight_ledger_survives_hard_interrupt_without_completed_case_rebilling(self):
        def hard_interrupt(*args, **kwargs):
            current = read_json(self.root / "data" / "catalog.json")
            current["papers"][0]["automation"]["lastCheckedAt"] = NOW
            write_json(self.root / "data" / "catalog.json", current)
            raise SystemExit("simulated hard exit")
        with self.assertRaises(SystemExit):
            self.run_session(batch_runner=hard_interrupt)
        self.assertIn("inFlight", self.state())
        with self.assertRaisesRegex(ValueError, "中断批次"):
            self.run_session(new_session=True)
        result = self.run_session()
        self.assertEqual(result["uncertainAiAttempts"], 15)
        self.assertNotIn(record(1)["doi"], self.selected)

    def test_batch_limit_stops_even_with_more_eligible_cases(self):
        self.settings.session_max_batches = 2
        result = self.run_session()
        self.assertEqual((result["status"], result["endReason"], result["batchCount"], result["processedCount"]),
                         ("paused", "batch_limit_reached", 2, 10))
        self.assertEqual(self.run_session()["processedCount"], 10)

    def test_daily_extended_shared_setting_keeps_backfill_at_twelve_batches(self):
        self.settings.session_max_batches = 36
        self.ai_per_paper = 0
        self.catalog([record(index) for index in range(1, 66)])
        result = self.run_session()
        self.assertEqual((result["endReason"], result["batchCount"], result["maxBatches"], result["processedCount"]),
                         ("batch_limit_reached", 12, 12, 60))
        self.assertEqual((result["requests"], result["aiAttempts"]), (60, 0))
        self.assertEqual(self.state()["maxSeconds"], 2700)

    def test_shared_batch_setting_above_thirty_six_fails_before_any_transport(self):
        for value in (37, True):
            with self.subTest(value=value):
                self.settings.session_max_batches = value
                with self.assertRaises(ValueError):
                    self.run_session()
                self.assertEqual(self.clients, [])
                self.assertEqual(self.selected, [])

    def test_global_ai_ceiling_applies_across_fresh_batch_clients(self):
        self.settings.session_max_ai_attempts = 20
        result = self.run_session()
        self.assertEqual((result["status"], result["endReason"], result["aiAttempts"], result["batchCount"]),
                         ("paused", "ai_budget_exhausted", 20, 2))
        self.assertEqual([client.post_attempts for client in self.clients], [15, 5])

    def test_total_http_ceiling_applies_across_batch_clients(self):
        self.settings.session_max_requests = 20
        result = self.run_session()
        self.assertEqual((result["requests"], result["processedCount"], result["endReason"]), (20, 5, "http_budget_exhausted"))
        self.assertEqual(len(self.clients), 1)

    def test_time_limit_stops_between_batches_before_next_request(self):
        self.settings.session_max_seconds = 60
        def callback(*args):
            self.clock.value += 61
        result = self.run_session(batch_callback=callback)
        self.assertEqual((result["endReason"], result["batchCount"], result["processedCount"]), ("time_limit_reached", 1, 5))

    def test_no_eligible_cases_stop_without_empty_batch_or_paid_calls(self):
        paper = record(1)
        paper["automation"]["bootstrap"] = {"status": "pending", "attempts": 1, "unsuccessfulScans": 1,
            "lastAttemptAt": NOW, "nextAttemptAt": "2026-10-11T13:00:00Z", "reasonCodes": ["selected_figure_missing"]}
        self.catalog([paper])
        result = self.run_session()
        self.assertEqual((result["status"], result["endReason"], result["batchCount"], result["requests"]),
                         ("finished", "no_eligible_cases", 0, 0))
        self.assertEqual(self.clients, [])
        self.assertEqual(self.selected, [])

    def test_fatal_ai_configuration_stops_after_current_batch_without_repeated_paid_attempts(self):
        def fatal(*args, **kwargs):
            result = self.batch(*args, **kwargs)
            result["aiStoppedStatus"] = "authentication_error"
            return result
        result = self.run_session(batch_runner=fatal)
        self.assertEqual((result["status"], result["endReason"], result["batchCount"]), ("paused", "ai_configuration_blocked", 1))
        self.assertEqual(len(self.selected), 5)

    def test_corrupt_or_lost_session_cursor_never_resets_paid_counters(self):
        self.settings.session_max_batches = 1
        self.run_session()
        saved = self.state()
        for field, value in (("cohortDOIs", [{}]), ("requests", -1), ("plannedDOIs", [record(1)["doi"]] * 2)):
            with self.subTest(field=field):
                ledger = {"days": {}, "candidates": {}, "backfillSession": dict(saved, **{field: value})}
                write_json(self.root / "data" / "ledger.json", ledger)
                with self.assertRaises(ValueError):
                    self.run_session()
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        with self.assertRaisesRegex(ValueError, "游标缺失"):
            self.run_session()

    def test_finished_session_requires_explicit_next_pass_and_never_discovers_new_papers(self):
        self.catalog([record(1)])
        first = self.run_session()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.run_session()["id"], first["id"])
        self.assertEqual(len(self.selected), 1)
        second = self.run_session(new_session=True)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(self.selected), 2)

    def test_authorized_removal_is_counted_separately_not_missing_or_material_complete(self):
        self.catalog([record(1), record(2)])
        self.done_materials = True
        def remove(*args, **kwargs):
            report = self.batch(*args, **kwargs)
            catalog = read_json(self.root / "data" / "catalog.json")
            catalog["papers"] = [paper for paper in catalog["papers"] if paper["doi"] != record(1)["doi"]]
            write_json(self.root / "data" / "catalog.json", catalog)
            ledger = read_json(self.root / "data" / "ledger.json")
            ledger["removedPapers"] = {record(1)["doi"]: {"reasonCode": "formal_version_unavailable", "removedAt": NOW}}
            write_json(self.root / "data" / "ledger.json", ledger)
            return report
        result = self.run_session(batch_runner=remove)
        self.assertEqual((result["targetCount"], result["processedCount"], result["removedCount"], result["completeCount"]), (2, 2, 1, 1))
        self.assertEqual((result["status"], result["endReason"], result["materialsStatus"]),
                         ("finished", "cohort_traversed", "complete_with_removals"))
        self.assertEqual(result["attentionCount"], 0)

    def test_unexpected_missing_record_is_not_accepted_as_authorized_removal(self):
        self.catalog([record(1), record(2)])
        def lose_record(*args, **kwargs):
            report = self.batch(*args, **kwargs)
            catalog = read_json(self.root / "data" / "catalog.json")
            catalog["papers"] = catalog["papers"][1:]
            write_json(self.root / "data" / "catalog.json", catalog)
            return report
        result = self.run_session(batch_runner=lose_record)
        self.assertEqual((result["removedCount"], result["attentionCount"], result["materialsStatus"]), (0, 1, "needs_attention"))

    def test_time_budget_inside_http_preserves_partial_work_and_stops_session(self):
        self.settings.session_max_seconds = 60
        def timeout(root, settings, client, **kwargs):
            self.clock.value += 61
            client.get_json("https://api.github.com/offline", ttl=0)
        result = self.run_session(batch_runner=timeout)
        self.assertEqual((result["status"], result["endReason"], result["batchCount"]), ("paused", "time_limit_reached", 1))
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["uncertainRequests"], 150)
        self.assertEqual(self.clients[0].requests, 0)

    def test_real_pipeline_fifty_five_case_three_post_retry_batches_finish_without_discovery(self):
        source = materials()
        discovery_calls = []
        active = adapters(article=lambda *args: deepcopy(source["article"]),
            repository=lambda *args: deepcopy(source["repository"]), metrics=lambda *args: deepcopy(source["metrics"]),
            discover=lambda *args: discovery_calls.append(True) or [record(100)])
        active.pop("summary")
        class RetryOpener:
            calls = 0
            def open(opener, request, timeout):
                opener.calls += 1
                payload = ai_payload(evidence_ids=["publication"]) if opener.calls % 3 == 0 else ai_payload(evidence_ids=["unsupported"])
                return Response(json.dumps(payload, ensure_ascii=False).encode(), url=request.full_url)
        opener = RetryOpener()
        clients = []
        def factory(settings):
            client = HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
                max_post_attempts=settings.max_ai_calls, opener=opener, sleep=self.clock.sleep, clock=self.clock.now)
            clients.append(client)
            return client
        self.catalog([dict(record(index), figure="Figure 1") for index in range(1, 56)])
        original = {"version": 1, "cohortDOIs": [record(i)["doi"] for i in range(1, 51)], "startedAt": NOW, "status": "active"}
        write_json(self.root / "data" / "ledger.json", {"days": {}, "candidates": {}, "bootstrap": original})
        callbacks = []
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), patch("paper_plot_library.pipeline.build_site"):
            result = run_backfill_session(self.root, self.settings, client_factory=factory, adapters=active,
                now=lambda: NOW, clock=self.clock.now,
                batch_callback=lambda report, summary: callbacks.append(deepcopy(summary)))
        self.assertEqual((result["status"], result["batchCount"], result["processedCount"], result["completeCount"]),
                         ("finished", 11, 55, 55))
        self.assertEqual((result["aiAttempts"], opener.calls), (165, 165))
        self.assertEqual(discovery_calls, [])
        self.assertEqual(len(callbacks), 11)
        self.assertEqual((callbacks[-1]["status"], callbacks[-1]["completeCount"]), ("finished", 55))
        self.assertTrue(all(client.post_attempts == 15 for client in clients))
        ledger = read_json(self.root / "data" / "ledger.json")
        self.assertEqual(ledger["bootstrap"]["cohortDOIs"], original["cohortDOIs"])
        self.assertEqual(len(ledger["backfillSession"]["cohortDOIs"]), 55)
        self.assertEqual(len(read_json(self.root / "data" / "updates.json")), 11)

    def test_real_pipeline_missing_images_are_not_retried_in_empty_endless_batches(self):
        self.catalog([dict(record(index), figure="Figure 2") for index in range(1, 7)])
        source = materials()
        active = adapters(article=lambda *args: deepcopy(source["article"]),
            repository=lambda *args: deepcopy(source["repository"]), metrics=lambda *args: deepcopy(source["metrics"]))
        from test_request_budget import BudgetFixtureOpener
        opener = BudgetFixtureOpener()
        def factory(settings):
            return HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
                max_post_attempts=settings.max_ai_calls, opener=opener, sleep=self.clock.sleep, clock=self.clock.now)
        active.pop("summary")
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), patch("paper_plot_library.pipeline.build_site"):
            result = run_backfill_session(self.root, self.settings, client_factory=factory, adapters=active,
                now=lambda: NOW, clock=self.clock.now)
        self.assertEqual((result["batchCount"], result["processedCount"], result["completeCount"], result["pendingCount"]), (2, 6, 0, 6))
        self.assertEqual(opener.methods.count("POST"), 6)
        self.assertEqual(result["endReason"], "cohort_traversed")

    def test_legacy_ai_attempt_limit_does_not_freeze_clean_source_gap_before_third_scan_and_removal(self):
        from paper_plot_library.pipeline import run_pipeline
        from test_request_budget import BudgetFixtureOpener
        source = materials()
        old = dict(record(1), figure="Figure 2", ai={"status": "authentication_error"})
        old["automation"]["bootstrap"] = {"status": "needs_attention", "attempts": 3, "unsuccessfulScans": 3,
            "sourceGapScans": {}, "lastAttemptAt": "2026-10-10T12:00:00Z", "reasonCodes": ["ai_authentication_failed"]}
        self.catalog([old])
        self.settings.auto_remove_uncollectable = True
        active = adapters(article=lambda *args: deepcopy(source["article"]),
            repository=lambda *args: deepcopy(source["repository"]), metrics=lambda *args: deepcopy(source["metrics"]))
        active.pop("summary")
        opener = BudgetFixtureOpener()
        def factory(settings):
            return HttpClient(self.root / ".ppl" / "cache", max_requests=settings.max_requests,
                max_post_attempts=settings.max_ai_calls, opener=opener, sleep=self.clock.sleep, clock=self.clock.now)
        # An explicit repair after fixing the AI key finds a real missing selected
        # figure. Legacy generic attempts already3 must not make gap1 permanent.
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), patch("paper_plot_library.pipeline.build_site"):
            run_pipeline(self.root, self.settings, client=factory(self.settings), adapters=active,
                         only_dois=[old["doi"]], discover_new=False, mode="backfill")
        current = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertEqual((current["automation"]["bootstrap"]["status"],
            current["automation"]["bootstrap"]["sourceGapScans"]["selected_figure_unavailable"]), ("needs_attention", 1))
        def session(moment):
            with patch("paper_plot_library.pipeline.stamp", return_value=moment), patch("paper_plot_library.pipeline.build_site"):
                return run_backfill_session(self.root, self.settings, adapters=active, client_factory=factory,
                    now=lambda: moment, clock=self.clock.now, new_session=True)
        same_day = session(NOW)
        self.assertEqual((same_day["batchCount"], same_day["endReason"]), (0, "no_eligible_cases"))
        second = session("2026-10-12T12:00:00Z")
        self.assertEqual((second["batchCount"], second["processedCount"], second["removedCount"]), (1, 1, 0))
        current = read_json(self.root / "data" / "catalog.json")["papers"][0]
        self.assertEqual(current["automation"]["bootstrap"]["sourceGapScans"]["selected_figure_unavailable"], 2)
        immediate = session("2026-10-12T12:01:00Z")
        self.assertEqual(immediate["batchCount"], 0)
        third = session("2026-10-13T12:00:00Z")
        self.assertEqual((third["batchCount"], third["processedCount"], third["removedCount"], third["completeCount"]), (1, 1, 1, 0))
        self.assertEqual((third["materialsStatus"], third["endReason"]), ("complete_with_removals", "cohort_traversed"))
        self.assertEqual(read_json(self.root / "data" / "catalog.json")["papers"], [])
        self.assertEqual(read_json(self.root / "data" / "ledger.json")["removedPapers"][old["doi"]]["reasonCode"], "selected_figure_unavailable")

    def test_source_gap_attention_rechecks_require_auto_removal_opt_in(self):
        paper = materials(1, complete=True)
        paper["figure"] = "Figure 2"
        paper["automation"]["bootstrap"] = {"status": "needs_attention", "attempts": 4, "unsuccessfulScans": 1,
            "sourceGapScans": {"selected_figure_unavailable": 1}, "lastAttemptAt": "2026-10-10T12:00:00Z",
            "reasonCodes": ["selected_figure_missing"]}
        self.catalog([paper])
        self.assertFalse(self.settings.auto_remove_uncollectable)
        result = self.run_session()
        self.assertEqual((result["processedCount"], result["batchCount"], result["endReason"]), (0, 0, "no_eligible_cases"))

    def test_attention_human_configuration_metrics_and_transient_errors_do_not_loop_as_source_cleanup(self):
        self.settings.auto_remove_uncollectable = True
        cases = [("ai", "authentication_error"), ("ai", "payment_required"), ("ai", "missing_key"),
                 ("metrics", "missing_key"), ("metrics", "network_error"), ("article", "network_error")]
        for index, (stage, error) in enumerate(cases, start=1):
            paper = materials(index, complete=True)
            paper["figure"] = "Figure 2"
            paper[stage]["status"] = error
            paper["automation"]["stages"][stage] = error
            paper["automation"]["bootstrap"] = {"status": "needs_attention", "attempts": 4, "unsuccessfulScans": 1,
                "sourceGapScans": {"selected_figure_unavailable": 1}, "lastAttemptAt": "2026-10-10T12:00:00Z",
                "reasonCodes": ["selected_figure_missing"]}
            with self.subTest(stage=stage, error=error):
                # A new temp root keeps prior fixed-session summaries out of this
                # independent eligibility check; no transport should be touched.
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    (root / "data" / "assets").mkdir(parents=True)
                    (root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
                    write_json(root / "data" / "catalog.json", {"authorCodeExecuted": False, "papers": [paper], "inputSpecs": {}})
                    result = run_backfill_session(root, self.settings, batch_runner=lambda *args, **kwargs: self.fail("no human-error retries"),
                        now=lambda: NOW, clock=self.clock.now)
                self.assertEqual((result["batchCount"], result["requests"], result["endReason"]), (0, 0, "no_eligible_cases"))


if __name__ == "__main__":
    unittest.main()
