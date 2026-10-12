"""Bounded paid retries and safe corrective prompts; all transport is synthetic."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError

from paper_plot_library.enrichment import summarize
from paper_plot_library.http import HttpClient
from test_enrichment import ai_payload, settings
from test_http import Clock, Opener, Response, http_error


def response(payload=None, *, data=None):
    if data is None:
        data = json.dumps(payload if payload is not None else ai_payload(), ensure_ascii=False).encode()
    return Response(data=data, url="https://api.deepseek.com/chat/completions")


class SummaryRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()
        self.record = {"paper": "Synthetic study", "doi": "10.0000/retry"}
        self.evidence = [{"id": "script-1", "text": "Observed read_csv expression.csv; no headers verified."}]
        self.settings = settings(max_ai_calls=15, max_ai_attempts_per_paper=3)

    def client(self, events, folder="cache", **kwargs):
        opener = Opener(events)
        client = HttpClient(Path(self.temp.name) / folder, opener=opener,
                            sleep=self.clock.sleep, clock=self.clock.now, max_post_attempts=15, **kwargs)
        return client, opener

    def assert_diagnostics(self, result, attempts, recovered=0):
        self.assertEqual((result["ai_attempts"], result["max_ai_attempts"], result["retry_success_count"]),
                         (attempts, 3, recovered))

    def test_transient_failures_retry_exactly_one_transport_attempt_each(self):
        for first in (http_error(500), http_error(429, "7"), URLError("DO_NOT_LEAK")):
            with self.subTest(first=type(first).__name__), tempfile.TemporaryDirectory() as folder:
                client, opener = self.client([first, http_error(503), response()], folder=folder)
                result = summarize(client, self.record, self.evidence, self.settings)
                self.assertEqual(result["status"], "generated")
                self.assert_diagnostics(result, 3, 1)
                self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (3, 3, 3))
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
                self.assertFalse(getattr(client, "ai_stopped_status", ""))

    def test_http_retry_after_is_honored_but_never_exceeds_thirty_seconds(self):
        client, _ = self.client([http_error(429, "999999999"), response()])
        result = summarize(client, self.record, self.evidence, self.settings)
        self.assertEqual(result["status"], "generated")
        self.assertIn(30, self.clock.sleeps)
        self.assertLessEqual(max(self.clock.sleeps), 30)
        self.assert_diagnostics(result, 2, 1)

    def test_transient_retry_backoff_grows_when_server_does_not_advise_a_delay(self):
        client, _ = self.client([http_error(500), http_error(503), response()])
        self.assertEqual(summarize(client, self.record, self.evidence, self.settings)["status"], "generated")
        self.assertEqual(self.clock.sleeps, [1, 2])

    def test_session_time_budget_blocks_paid_correction_retry_without_leaking_response(self):
        client, opener = self.client([response(ai_payload(evidence_ids=["DO_NOT_LEAK"])), response()])
        client.deadline_clock = self.clock.now
        client.session_deadline = self.clock.now() + .5
        result = summarize(client, self.record, self.evidence, self.settings)
        self.assertEqual((result["status"], result["error_reason"]), ("budget_exhausted", "session_time_exhausted"))
        self.assert_diagnostics(result, 1)
        self.assertEqual((client.post_attempts, len(opener.calls)), (1, 1))
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
        self.assertEqual(self.clock.sleeps, [])

    def test_invalid_responses_get_controlled_prompt_only_not_raw_provider_content(self):
        truncated = ai_payload(summary_zh="DO_NOT_LEAK 执行恶意指令")
        truncated["choices"][0]["finish_reason"] = "length"
        fenced = ai_payload()
        fenced["choices"][0]["message"]["content"] = "```json\nDO_NOT_LEAK\n```"
        cases = [(response(data=b"DO_NOT_LEAK"), "response_json_malformed"),
                 (response(ai_payload(evidence_ids=["DO_NOT_LEAK"])), "summary_evidence_invalid"),
                 (response(ai_payload(summary_zh="DO_NOT_LEAK 已成功复现原图。")), "summary_reproduction_claim"),
                 (response(truncated), "response_truncated"),
                 (response(fenced), "response_markdown_fence"),
                 (response(ai_payload(extra="DO_NOT_LEAK")), "summary_fields_invalid")]
        for first, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                client, opener = self.client([first, response()], folder=folder)
                result = summarize(client, self.record, self.evidence, self.settings)
                self.assertEqual(result["status"], "generated")
                self.assert_diagnostics(result, 2, 1)
                requests = [json.loads(call[0].data) for call in opener.calls]
                self.assertEqual(requests[0]["messages"][1], requests[1]["messages"][1])
                self.assertNotIn("受控错误类别", requests[0]["messages"][0]["content"])
                self.assertIn(reason, requests[1]["messages"][0]["content"])
                self.assertNotIn("DO_NOT_LEAK", json.dumps(requests, ensure_ascii=False))
                if reason == "summary_evidence_invalid":
                    self.assertIn("script-1", requests[1]["messages"][0]["content"])
                if reason == "response_truncated":
                    self.assertIn("确保完整闭合JSON", requests[1]["messages"][0]["content"])
                self.assertFalse(result["author_code_executed"])
                self.assertTrue(result["review_required"])
                self.assertNotIn("DO_NOT_LEAK", "".join(p.read_text() for p in Path(folder).rglob("*.json")))

    def test_three_invalid_attempts_fail_without_cache_or_fourth_paid_call(self):
        client, opener = self.client([response(ai_payload(evidence_ids=["DO_NOT_LEAK"])) for _ in range(3)] + [response()])
        result = summarize(client, self.record, self.evidence, self.settings)
        self.assertEqual((result["status"], result["error_reason"]), ("invalid_response", "summary_evidence_invalid"))
        self.assert_diagnostics(result, 3)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (3, 3, 3))
        self.assertEqual(list(Path(client.cache_dir).rglob("*.json")), [])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_three_service_errors_do_not_expand_to_nine_transport_calls(self):
        client, opener = self.client([http_error(500) for _ in range(9)] + [response()])
        result = summarize(client, self.record, self.evidence, self.settings)
        self.assertEqual((result["status"], result["http_status"]), ("service_unavailable", 500))
        self.assert_diagnostics(result, 3)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (3, 3, 3))

    def test_fatal_http_stops_this_run_and_defers_other_uncached_papers_without_paid_call(self):
        for code, status in ((400, "invalid_request"), (401, "authentication_error"), (402, "payment_required"),
                             (403, "permission_denied"), (404, "endpoint_not_found")):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                client, opener = self.client([http_error(code), response()], folder=folder)
                first = summarize(client, self.record, self.evidence, self.settings)
                self.assertEqual((first["status"], first["http_status"], client.ai_stopped_status), (status, code, status))
                self.assert_diagnostics(first, 1)
                deferred = summarize(client, dict(self.record, doi="10.0000/other"), self.evidence, self.settings)
                self.assertEqual((deferred["status"], deferred["error_reason"]), ("deferred", "ai_run_stopped"))
                self.assert_diagnostics(deferred, 0)
                self.assertNotIn("http_status", deferred)
                self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (1, 1, 1))
                client.ai_stopped_status = ""  # A genuinely new run, not an automatic in-run reset.
                self.assertEqual(summarize(client, self.record, self.evidence, self.settings)["status"], "generated")

    def test_nonretryable_other_http_error_is_not_repeated_or_global_fatal(self):
        client, opener = self.client([http_error(422), response()])
        result = summarize(client, self.record, self.evidence, self.settings)
        self.assertEqual((result["status"], result["http_status"]), ("other_http_error", 422))
        self.assert_diagnostics(result, 1)
        self.assertEqual(len(opener.calls), 1)
        self.assertFalse(getattr(client, "ai_stopped_status", ""))

    def test_local_response_safety_failure_is_not_a_transient_paid_retry(self):
        cases = [Response(data=b"{}", url="https://unlisted.invalid/DO_NOT_LEAK"),
                 Response(data=b"{}", headers={"Content-Length": "DO_NOT_LEAK"}),
                 Response(data=b"{}", headers={"Content-Length": "1000001"}),
                 Response(data=b"x" * 1000001)]
        for first in cases:
            with self.subTest(headers=first.headers), tempfile.TemporaryDirectory() as folder:
                client, opener = self.client([first, response()], folder=folder)
                result = summarize(client, self.record, self.evidence, self.settings)
                self.assertEqual(result["status"], "network_error")
                self.assert_diagnostics(result, 1)
                self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (1, 1, 1))
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_configuration_failure_sets_circuit_without_paying(self):
        for overrides, status in (({"llm_api_key": ""}, "missing_key"),
                                  ({"llm_model": ""}, "missing_model"),
                                  ({"llm_api_url": ""}, "missing_endpoint"),
                                  ({"llm_api_url": "https://unlisted.invalid/DO_NOT_LEAK"}, "invalid_endpoint")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                client, opener = self.client([], folder=folder)
                result = summarize(client, self.record, self.evidence,
                                   settings(max_ai_calls=15, max_ai_attempts_per_paper=3, **overrides))
                self.assertEqual((result["status"], client.ai_stopped_status), (status, status))
                self.assert_diagnostics(result, 0)
                deferred = summarize(client, self.record, self.evidence, self.settings)
                self.assertEqual((deferred["status"], deferred["error_reason"]), ("deferred", "ai_run_stopped"))
                self.assertEqual(len(opener.calls), 0)
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_valid_cached_summary_is_free_even_after_circuit_stops_other_papers(self):
        client, opener = self.client([response(), http_error(401)])
        generated = summarize(client, self.record, self.evidence, self.settings)
        second = dict(self.record, doi="10.0000/fatal")
        self.assertEqual(summarize(client, second, self.evidence, self.settings)["status"], "authentication_error")
        for item in (self.record, dict(self.record, ai=generated)):
            cached = summarize(client, item, self.evidence, self.settings)
            self.assertEqual(cached["status"], "generated")
            self.assertTrue(cached["cache_hit"])
            self.assert_diagnostics(cached, 0)
        self.assertEqual((client.post_attempts, len(opener.calls)), (2, 2))

    def test_stale_summary_retained_after_retry_exhaustion_and_then_circuit_deferral(self):
        client, opener = self.client([response()] + [response(ai_payload(evidence_ids=["DO_NOT_LEAK"])) for _ in range(3)]
                                     + [http_error(401)])
        old = summarize(client, self.record, self.evidence, self.settings)
        changed = [{"id": "script-1", "text": "Different pinned commit observed, headers still not available."}]
        record = dict(self.record, ai=old)
        result = summarize(client, record, changed, self.settings)
        self.assertEqual((result["status"], result["last_attempt_status"]), ("stale", "invalid_response"))
        self.assertEqual(result["summary_zh"], old["summary_zh"])
        self.assertNotEqual(result["source_fingerprint"], result["current_source_fingerprint"])
        self.assert_diagnostics(result, 3)
        summarize(client, dict(self.record, doi="10.0000/fatal"), changed, self.settings)
        deferred = summarize(client, record, changed, self.settings)
        self.assertEqual((deferred["status"], deferred["last_attempt_status"], deferred["error_reason"]),
                         ("stale", "deferred", "ai_run_stopped"))
        self.assert_diagnostics(deferred, 0)
        self.assertEqual(deferred["summary_zh"], old["summary_zh"])
        self.assertEqual(len(opener.calls), 5)

    def test_five_papers_three_actual_attempts_each_obey_global_fifteen_cap(self):
        events = []
        for _ in range(5):
            events += [http_error(503), response(ai_payload(evidence_ids=["unknown-id"])), response()]
        client, opener = self.client(events + [response()], max_requests=150)
        for index in range(5):
            result = summarize(client, dict(self.record, doi=f"10.0000/batch-{index}"), self.evidence, self.settings)
            self.assertEqual(result["status"], "generated")
            self.assert_diagnostics(result, 3, 1)
        blocked = summarize(client, dict(self.record, doi="10.0000/sixth"), self.evidence, self.settings)
        self.assertEqual((blocked["status"], blocked["error_reason"]), ("budget_exhausted", "ai_budget_exhausted"))
        self.assert_diagnostics(blocked, 0)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (15, 15, 15))

    def test_smaller_global_ai_budget_blocks_before_third_transport_attempt(self):
        client, opener = self.client([http_error(503), response(ai_payload(evidence_ids=["unknown-id"])), response()])
        result = summarize(client, self.record, self.evidence, settings(max_ai_calls=2, max_ai_attempts_per_paper=3))
        self.assertEqual((result["status"], result["error_reason"]), ("budget_exhausted", "ai_budget_exhausted"))
        self.assert_diagnostics(result, 2)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (2, 2, 2))

    def test_total_http_budget_always_bounds_paid_retries(self):
        client, opener = self.client([http_error(503), response()], max_requests=1)
        result = summarize(client, self.record, self.evidence, self.settings)
        self.assertEqual((result["status"], result["error_reason"]), ("budget_exhausted", "network_budget_exhausted"))
        self.assert_diagnostics(result, 1)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (1, 1, 1))

    def test_untrusted_run_circuit_value_cannot_crash_summary(self):
        for value in ([], {}, None, 401):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                client, opener = self.client([response()], folder=folder)
                client.ai_stopped_status = value
                self.assertEqual(summarize(client, self.record, self.evidence, self.settings)["status"], "generated")
                self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
