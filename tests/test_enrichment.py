"""Offline boundary tests; all provider responses are synthetic fixtures."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from paper_plot_library.enrichment import enrich_metrics, summarize
from paper_plot_library.http import BudgetExceeded, FetchError, HttpClient


class FakeClient:
    def __init__(self, cache_dir, response=None, failure=None):
        self.cache_dir = Path(cache_dir)
        self.response = response
        self.failure = failure
        self.calls = []
        self.post_attempts = 0

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.failure:
            raise self.failure
        return self.response

    def post_json(self, url, payload, **kwargs):
        self.post_attempts += 1
        self.calls.append((url, payload, kwargs))
        if self.failure:
            raise self.failure
        return self.response


def settings(**overrides):
    values = dict(easyscholar_key="fixture-secret", llm_api_url="https://api.deepseek.com/chat/completions",
                  llm_api_key="fixture-ai-secret", llm_model="fixture-model", max_ai_calls=3,
                  max_ai_attempts_per_paper=1)
    values.update(overrides)
    return SimpleNamespace(**values)


def metrics_payload(**ranks):
    return {"code": 200, "msg": "SUCCESS", "data": {"officialRank": {"all": ranks}}}


def ai_payload(**overrides):
    value = {"title_zh": "测试论文中文标题", "summary_zh": "证据中脚本读取表达矩阵，输入列名尚未核实。",
             "learning_points": ["学习先读取数据再绘图的结构。"], "limitations": ["没有执行代码。"],
             "evidence_ids": ["script-1"]}
    value.update(overrides)
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}]}


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record = {"paper": "Synthetic test only", "doi": "10.0000/fixture", "journal": "Nature Methods", "issn": "1548-7091"}
        self.evidence = [{"id": "script-1", "text": "L4: pandas.read_csv('expression.csv'); columns not available"}]

    def test_metrics_projection_has_unknown_year_and_no_secret(self):
        client = FakeClient(self.temp.name, metrics_payload(sciif="12.5", sci="Q1", sciUp="1区"))
        result = enrich_metrics(client, self.record, settings())
        self.assertEqual((result["status"], result["if"], result["jcr"], result["metric_year"]), ("matched", "12.5", "Q1", "unknown"))
        self.assertIn("retrieved_at", result)
        saved = (Path(self.temp.name) / "journal-metrics.json").read_text()
        self.assertNotIn("fixture-secret", saved)
        self.assertNotIn("secretKey", saved)
        self.assertEqual(client.calls[0][1]["ttl"], 0)

    def test_metrics_issn_cache_precedes_renamed_journal(self):
        client = FakeClient(self.temp.name, metrics_payload(sciif=10, sci="Q2"))
        enrich_metrics(client, self.record, settings())
        renamed = dict(self.record, journal="Completely Different Name")
        result = enrich_metrics(client, renamed, settings(easyscholar_key=""))
        self.assertEqual(result["if"], "10")
        self.assertTrue(result["cache_hit"])
        self.assertEqual(len(client.calls), 1)

    def test_metrics_unsafe_fields_are_not_kept(self):
        client = FakeClient(self.temp.name, metrics_payload(sciif="<script>x</script>", sci="Q9", sciUp="<img>"))
        result = enrich_metrics(client, self.record, settings())
        self.assertEqual(result["status"], "not_found")
        self.assertFalse(result["if"] or result["jcr"] or result["cas"])

    def test_metrics_split_sci_ssci(self):
        client = FakeClient(self.temp.name, metrics_payload(sciif=True, sci="Q1", ssci="Q2"))
        result = enrich_metrics(client, self.record, settings())
        self.assertEqual(result["jcr"], "SCI Q1 / SSCI Q2")
        self.assertEqual(result["if"], "")

    def test_metrics_missing_key_and_preprint_do_not_call_api(self):
        client = FakeClient(self.temp.name)
        self.assertEqual(enrich_metrics(client, self.record, settings(easyscholar_key=""))["status"], "missing_key")
        preprint = dict(self.record, publicationTypes=["Preprint"])
        self.assertEqual(enrich_metrics(client, preprint, settings())["status"], "not_applicable")
        self.assertEqual(client.calls, [])

    def test_metrics_failed_refresh_preserves_good_cache(self):
        client = FakeClient(self.temp.name, metrics_payload(sciif=12, sci="Q1"))
        enrich_metrics(client, self.record, settings())
        path = Path(self.temp.name) / "journal-metrics.json"
        payload = json.loads(path.read_text())
        for entry in payload["journals"].values():
            entry["retrieved_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(payload))
        client.failure = RuntimeError("https://example.com?secretKey=DO_NOT_LEAK")
        result = enrich_metrics(client, self.record, settings())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["last_attempt_status"], "error")
        self.assertTrue(result["stale"])
        self.assertEqual(result["if"], "12")
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_metrics_provider_application_code_is_not_an_http_status(self):
        client = FakeClient(self.temp.name, {"code": 401, "msg": "fixture-secret DO_NOT_LEAK"})
        result = enrich_metrics(client, self.record, settings())
        self.assertEqual((result["status"], result["error_reason"]), ("error", "metrics_provider_error"))
        self.assertNotIn("http_status", result)
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_metrics_response_reasons_are_safe_and_specific(self):
        cases = ((["DO_NOT_LEAK"], "metrics_response_shape_invalid"),
                 ({"msg": "DO_NOT_LEAK"}, "metrics_response_shape_invalid"),
                 ({"code": True}, "metrics_response_shape_invalid"),
                 ({"code": 200, "msg": ["DO_NOT_LEAK"]}, "metrics_response_shape_invalid"),
                 ({"code": "https://example.com?key=DO_NOT_LEAK"}, "metrics_provider_error"),
                 ({"code": 200, "msg": "DO_NOT_LEAK"}, "metrics_provider_error"),
                 ({"code": 200, "data": ["DO_NOT_LEAK"]}, "metrics_rank_shape_invalid"),
                 ({"code": 200, "data": {"officialRank": {"all": "DO_NOT_LEAK"}}}, "metrics_rank_shape_invalid"),
                 (metrics_payload(), "metrics_not_found"))
        for payload, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                client = FakeClient(folder, payload)
                result = enrich_metrics(client, self.record, settings())
                self.assertEqual(result["error_reason"], reason)
                self.assertEqual(result["status"], "not_found" if reason == "metrics_not_found" else "error")
                self.assertEqual(len(client.calls), 1)
                self.assertNotIn("http_status", result)
                saved = json.dumps(result) + "".join(p.read_text() for p in Path(folder).rglob("*.json"))
                self.assertNotIn("DO_NOT_LEAK", saved)
                self.assertNotIn("https://", saved)
                self.assertNotIn("fixture-secret", saved)

    def test_metrics_missing_journal_has_reason_without_network(self):
        client = FakeClient(self.temp.name)
        result = enrich_metrics(client, dict(self.record, journal=""), settings())
        self.assertEqual((result["status"], result["error_reason"]), ("missing_journal", "missing_journal"))
        self.assertEqual(client.calls, [])

    def test_metrics_not_found_cached_reason_survives_without_key(self):
        client = FakeClient(self.temp.name, metrics_payload())
        enrich_metrics(client, self.record, settings())
        result = enrich_metrics(client, self.record, settings(easyscholar_key=""))
        self.assertEqual((result["status"], result["error_reason"]), ("not_found", "metrics_not_found"))
        self.assertTrue(result["cache_hit"])
        self.assertEqual(len(client.calls), 1)

    def test_metrics_invalid_provider_refresh_keeps_previous_evidence(self):
        old = {"status": "matched", "if": "5", "jcr": "Q1", "cas": "1区", "source": "EasyScholar",
               "retrieved_at": "2000-01-01T00:00:00Z", "metric_year": "unknown"}
        client = FakeClient(self.temp.name, {"code": 200, "data": "fixture-secret DO_NOT_LEAK"})
        result = enrich_metrics(client, dict(self.record, metrics=old), settings())
        self.assertEqual((result["status"], result["last_attempt_status"], result["error_reason"]),
                         ("matched", "error", "metrics_rank_shape_invalid"))
        self.assertEqual((result["if"], result["retrieved_at"]), (old["if"], old["retrieved_at"]))
        self.assertTrue(result["stale"])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_metrics_budget_or_http_refresh_retains_previous_evidence(self):
        old = {"status": "matched", "if": "5", "jcr": "Q1", "cas": "1区", "source": "EasyScholar",
               "retrieved_at": "2000-01-01T00:00:00Z", "metric_year": "unknown"}
        failures = ((BudgetExceeded("collection_budget_exhausted"), "budget_exhausted", "collection_budget_exhausted", None),
                    (FetchError("fixture-secret DO_NOT_LEAK", 402), "payment_required", "http_402", 402),
                    (FetchError("fixture-secret DO_NOT_LEAK"), "network_error", "network_error", None))
        for failure, status, reason, code in failures:
            with self.subTest(status=status):
                result = enrich_metrics(FakeClient(self.temp.name, failure=failure), dict(self.record, metrics=old), settings())
                self.assertEqual(result["status"], "matched")
                self.assertTrue(result["stale"])
                self.assertEqual((result["if"], result["jcr"], result["cas"], result["retrieved_at"]),
                                 ("5", "Q1", "1区", old["retrieved_at"]))
                self.assertEqual((result["last_attempt_status"], result["error_reason"], result.get("http_status")),
                                 (status, reason, code))
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_metrics_success_discards_old_failure_diagnostics(self):
        old = {"status": "matched", "if": "5", "jcr": "Q1", "cas": "1区", "source": "EasyScholar",
               "retrieved_at": "2000-01-01T00:00:00Z", "metric_year": "unknown", "stale": True,
               "last_attempt_status": "payment_required", "error_reason": "http_402", "http_status": 402}
        result = enrich_metrics(FakeClient(self.temp.name, metrics_payload(sciif="6", sci="Q1", sciUp="1区")),
                                dict(self.record, metrics=old), settings())
        self.assertEqual(result["if"], "6")
        for field in ("last_attempt_status", "error_reason", "http_status", "stale"):
            self.assertNotIn(field, result)

    def test_metrics_partial_refresh_retains_other_fields(self):
        old = {"status": "matched", "if": "5", "jcr": "Q1", "cas": "1区", "source": "EasyScholar",
               "retrieved_at": "2000-01-01T00:00:00Z", "metric_year": "unknown"}
        record = dict(self.record, metrics=old)
        client = FakeClient(self.temp.name, metrics_payload(sciif="6"))
        result = enrich_metrics(client, record, settings())
        self.assertEqual((result["if"], result["jcr"], result["cas"]), ("6", "Q1", "1区"))
        self.assertEqual(result["retained_field_asof"]["jcr"], old["retrieved_at"])
        cached = enrich_metrics(client, record, settings())
        self.assertEqual(cached["retained_field_asof"]["jcr"], old["retrieved_at"])

    def test_metrics_fresh_catalog_persists_without_private_cache_or_secret(self):
        first = enrich_metrics(FakeClient(self.temp.name, metrics_payload(sciif=12, sci="Q1")), self.record, settings())
        with tempfile.TemporaryDirectory() as empty_cache:
            client = FakeClient(empty_cache)
            result = enrich_metrics(client, dict(self.record, metrics=first), settings(easyscholar_key=""))
            self.assertTrue(result["persistent_cache_hit"])
            self.assertEqual(result["if"], "12")
            self.assertEqual(client.calls, [])

    def test_metrics_does_not_relabel_unrelated_source_as_easyscholar(self):
        record = dict(self.record, metrics={"status": "matched", "if": "20", "jcr": "Q1", "cas": "", "source": "unverified-manual", "retrieved_at": "2000-01-01T00:00:00Z"})
        client = FakeClient(self.temp.name, failure=RuntimeError("failed"))
        self.assertEqual(enrich_metrics(client, record, settings())["status"], "error")

    def test_summary_missing_key_model_evidence_and_url(self):
        client = FakeClient(self.temp.name)
        for overrides, evidence, expected in (({"llm_api_key": ""}, self.evidence, "missing_key"),
                                               ({"llm_model": ""}, self.evidence, "missing_model"),
                                               ({}, [], "missing_evidence"),
                                               ({"llm_api_url": ""}, self.evidence, "missing_endpoint"),
                                               ({"llm_api_url": "http://unsafe.example/api"}, self.evidence, "invalid_endpoint")):
            self.assertEqual(summarize(client, self.record, evidence, settings(**overrides))["status"], expected)
        self.assertEqual(client.calls, [])

    def test_summary_valid_generated_cached_and_cited(self):
        client = FakeClient(self.temp.name, ai_payload())
        result = summarize(client, self.record, self.evidence, settings())
        self.assertEqual(result["status"], "generated")
        self.assertTrue(result["review_required"])
        self.assertFalse(result["author_code_executed"])
        self.assertIn("静态检查", result["limitations"][-1])
        self.assertEqual(client.calls[0][1]["thinking"], {"type": "disabled"})
        self.assertEqual(client.calls[0][1]["response_format"], {"type": "json_object"})
        cached = summarize(client, self.record, self.evidence, settings())
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(client.post_attempts, 1)
        saved = "".join(p.read_text() for p in Path(self.temp.name).rglob("*.json"))
        self.assertNotIn("fixture-ai-secret", saved)
        self.assertNotIn("Bearer", saved)

    def test_summary_rejects_fake_evidence_and_new_factual_fields(self):
        for payload in (ai_payload(evidence_ids=["invented"]), ai_payload(input_fields=["invented_column"]),
                        ai_payload(learning_points="wrong type"), ai_payload(summary_zh="作者代码运行成功，与原图完全一致。")):
            with tempfile.TemporaryDirectory() as folder:
                client = FakeClient(folder, payload)
                self.assertEqual(summarize(client, self.record, self.evidence, settings())["status"], "invalid_response")

    def test_summary_rejects_truncated_response(self):
        payload = ai_payload()
        payload["choices"][0]["finish_reason"] = "length"
        client = FakeClient(self.temp.name, payload)
        self.assertEqual(summarize(client, self.record, self.evidence, settings())["status"], "invalid_response")

    def test_summary_rejects_malformed_json_and_message(self):
        for content in ("{broken", None):
            client = FakeClient(self.temp.name, {"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
            self.assertEqual(summarize(client, self.record, self.evidence, settings())["status"], "invalid_response")
        client = FakeClient(self.temp.name, {"choices": [{"finish_reason": "stop", "message": "bad"}]})
        self.assertEqual(summarize(client, self.record, self.evidence, settings())["status"], "invalid_response")

    def test_summary_invalid_response_reasons_without_extra_paid_calls_or_content(self):
        def content_payload(content, finish_reason="stop"):
            return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}

        good_content = ai_payload()["choices"][0]["message"]["content"]
        duplicate_fields = good_content[:-1] + ', "summary_zh": "DO_NOT_LEAK"}'
        missing_field = json.loads(good_content)
        del missing_field["limitations"]
        cases = (({"choices": ["DO_NOT_LEAK"]}, "response_shape_invalid"),
                 ({"choices": [None, None]}, "response_shape_invalid"),
                 (content_payload("DO_NOT_LEAK", "length"), "response_truncated"),
                 (content_payload("DO_NOT_LEAK", "DO_NOT_LEAK"), "response_finish_reason_invalid"),
                 (content_payload(None), "response_content_invalid"),
                 (content_payload("  "), "response_content_invalid"),
                 (content_payload("DO_NOT_LEAK" * 2001), "response_content_too_long"),
                 (content_payload("```json\n" + good_content + "\n```"), "response_markdown_fence"),
                 (content_payload("{DO_NOT_LEAK"), "response_json_malformed"),
                 (content_payload(duplicate_fields), "response_json_malformed"),
                 (content_payload('["DO_NOT_LEAK"]'), "summary_shape_invalid"),
                 (content_payload(json.dumps(missing_field)), "summary_fields_invalid"),
                 (ai_payload(secret="https://example.com?key=DO_NOT_LEAK"), "summary_fields_invalid"),
                 (ai_payload(title_zh=123), "summary_text_invalid"),
                 (ai_payload(summary_zh="  "), "summary_text_invalid"),
                 (ai_payload(title_zh="DO_NOT_LEAK", summary_zh="English only"), "summary_chinese_missing"),
                 (ai_payload(learning_points="DO_NOT_LEAK"), "summary_list_invalid"),
                 (ai_payload(limitations=[None]), "summary_list_invalid"),
                 (ai_payload(evidence_ids=[]), "summary_evidence_invalid"),
                 (ai_payload(evidence_ids=["DO_NOT_LEAK"]), "summary_evidence_invalid"),
                 (ai_payload(summary_zh="作者代码运行成功，与原图完全一致。"), "summary_reproduction_claim"),
                 (ai_payload(title_zh="已复现原图"), "summary_reproduction_claim"),
                 (ai_payload(limitations=["已执行作者代码。"]), "summary_reproduction_claim"))
        for payload, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                client = FakeClient(folder, payload)
                result = summarize(client, self.record, self.evidence, settings())
                self.assertEqual((result["status"], result["error_reason"]), ("invalid_response", reason))
                self.assertEqual(client.post_attempts, 1)
                self.assertEqual(list(Path(folder).rglob("*.json")), [])
                serialized = json.dumps(result)
                self.assertNotIn("DO_NOT_LEAK", serialized)
                self.assertNotIn("fixture-ai-secret", serialized)
                self.assertNotIn("https://", serialized)

    def test_summary_invalid_response_keeps_previous_summary_stale_with_reason(self):
        good = summarize(FakeClient(self.temp.name, ai_payload()), self.record, self.evidence, settings())
        changed = [{"id": "script-1", "text": "Updated script inputs are not verified"}]
        payload = ai_payload()
        payload["choices"][0]["finish_reason"] = "length"
        client = FakeClient(self.temp.name, payload)
        result = summarize(client, dict(self.record, ai=good), changed, settings())
        self.assertEqual((result["status"], result["last_attempt_status"], result["error_reason"]),
                         ("stale", "invalid_response", "response_truncated"))
        self.assertEqual(result["summary_zh"], good["summary_zh"])
        self.assertEqual(result["source_fingerprint"], good["source_fingerprint"])
        self.assertNotEqual(result["current_source_fingerprint"], good["source_fingerprint"])
        self.assertEqual(client.post_attempts, 1)

    def test_summary_bad_endpoint_and_budget_are_recoverable(self):
        client = FakeClient(self.temp.name, ai_payload())
        self.assertEqual(summarize(client, self.record, self.evidence, settings(llm_api_url="https://[bad"))["status"], "invalid_endpoint")
        client.ai_stopped_status = ""  # Separate run after fixing its endpoint configuration.
        result = summarize(client, self.record, self.evidence, settings(max_ai_calls="bad"))
        self.assertEqual((result["status"], result["error_reason"]), ("budget_exhausted", "ai_budget_exhausted"))

    def test_summary_sanitizes_provider_error(self):
        client = FakeClient(self.temp.name, failure=RuntimeError("Bearer fixture-ai-secret"))
        result = summarize(client, self.record, self.evidence, settings())
        self.assertEqual(result["status"], "error")
        self.assertNotIn("fixture-ai-secret", json.dumps(result))

    def test_summary_budget_is_checked_before_call(self):
        client = FakeClient(self.temp.name, ai_payload())
        client.post_attempts = 3
        result = summarize(client, self.record, self.evidence, settings(max_ai_calls=3))
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["error_reason"], "ai_budget_exhausted")
        self.assertEqual(client.calls, [])

    def test_summary_validates_paid_endpoint_before_post(self):
        client = FakeClient(self.temp.name, ai_payload())
        for endpoint in ("https://unlisted.example/chat/completions", "https://api.deepseek.com:444/chat/completions",
                         "https://fixture-ai-secret@api.deepseek.com/chat/completions",
                         "https://api.deepseek.com/chat/completions?key=fixture-ai-secret",
                         "https://api.deepseek.com/chat/completions#fixture-ai-secret"):
            with self.subTest(endpoint=endpoint):
                result = summarize(client, self.record, self.evidence, settings(llm_api_url=endpoint))
                self.assertEqual((result["status"], result["error_reason"]), ("invalid_endpoint", "invalid_endpoint"))
                self.assertNotIn("fixture-ai-secret", json.dumps(result))
                self.assertNotIn("https://", json.dumps(result))
        self.assertEqual(client.post_attempts, 0)
        self.assertEqual(client.calls, [])

    def test_shared_network_budget_exhaustion_does_not_attempt_ai(self):
        class NoNetwork:
            def open(self, *args, **kwargs):
                raise AssertionError("budget guard must prevent network access")

        client = HttpClient(self.temp.name, max_requests=150, opener=NoNetwork(), sleep=lambda _: None)
        client.requests = 150
        result = summarize(client, self.record, self.evidence, settings())
        self.assertEqual((result["status"], result["error_reason"]), ("budget_exhausted", "network_budget_exhausted"))
        self.assertEqual((client.requests, client.post_attempts), (150, 0))

    def test_transport_budget_reasons_are_explicit_for_both_enrichments(self):
        for reason in ("network_budget_exhausted", "collection_budget_exhausted", "ai_budget_exhausted"):
            with self.subTest(reason=reason):
                client = FakeClient(self.temp.name, failure=BudgetExceeded(reason))
                for result in (summarize(client, self.record, self.evidence, settings()),
                               enrich_metrics(client, self.record, settings())):
                    self.assertEqual((result["status"], result["error_reason"]), ("budget_exhausted", reason))
                    self.assertNotIn("http_status", result)

    def test_http_failures_have_safe_specific_classifications(self):
        classifications = {400: "invalid_request", 401: "authentication_error", 402: "payment_required",
                           403: "permission_denied", 404: "endpoint_not_found", 429: "rate_limited",
                           500: "service_unavailable", 503: "service_unavailable", 422: "other_http_error"}
        for code, status in classifications.items():
            with self.subTest(code=code):
                client = FakeClient(self.temp.name, failure=FetchError(
                    "https://api.deepseek.com?key=fixture-ai-secret HTTP body: DO_NOT_LEAK", code))
                for result in (summarize(client, self.record, self.evidence, settings()),
                               enrich_metrics(client, self.record, settings())):
                    self.assertEqual((result["status"], result["error_reason"], result["http_status"]),
                                     (status, f"http_{code}", code))
                    serialized = json.dumps(result)
                    self.assertNotIn("fixture-ai-secret", serialized)
                    self.assertNotIn("DO_NOT_LEAK", serialized)
                    self.assertNotIn("https://", serialized)

    def test_transport_without_safe_http_status_is_network_error(self):
        for code in (None, "401 fixture-ai-secret", True, 600):
            client = FakeClient(self.temp.name, failure=FetchError("secret body fixture-ai-secret", code))
            for result in (summarize(client, self.record, self.evidence, settings()),
                           enrich_metrics(client, self.record, settings())):
                self.assertEqual((result["status"], result["error_reason"]), ("network_error", "network_error"))
                self.assertNotIn("http_status", result)
                self.assertNotIn("fixture-ai-secret", json.dumps(result))

    def test_summary_fingerprint_changes_with_evidence(self):
        client = FakeClient(self.temp.name, ai_payload())
        summarize(client, self.record, self.evidence, settings())
        changed = [{"id": "script-1", "text": "Changed repository commit evidence"}]
        summarize(client, self.record, changed, settings())
        self.assertEqual(client.post_attempts, 2)

    def test_summary_source_changes_after_prompt_excerpt_invalidate_cache(self):
        client = FakeClient(self.temp.name, ai_payload())
        evidence = [{"id": "script-1", "text": "a" * 1400 + "commit=old"}]
        old = summarize(client, self.record, evidence, settings())
        changed = [{"id": "script-1", "text": "a" * 1400 + "commit=new"}]
        new = summarize(client, dict(self.record, ai=old), changed, settings())
        self.assertNotEqual(old["source_fingerprint"], new["source_fingerprint"])
        self.assertEqual(client.post_attempts, 2)

    def test_summary_existing_good_output_survives_api_failure(self):
        client = FakeClient(self.temp.name, ai_payload())
        good = summarize(client, self.record, self.evidence, settings())
        record = dict(self.record, ai=good)
        result = summarize(client, record, self.evidence, settings(llm_api_key=""))
        self.assertEqual(result["status"], "generated")
        self.assertTrue(result["persistent_cache_hit"])
        self.assertEqual(result["title_zh"], good["title_zh"])

    def test_summary_persistent_catalog_avoids_rebilling_after_cache_eviction(self):
        good = summarize(FakeClient(self.temp.name, ai_payload()), self.record, self.evidence, settings())
        self.assertEqual(len(good["source_fingerprint"]), 64)
        self.assertEqual(len(good["summary_fingerprint"]), 64)
        with tempfile.TemporaryDirectory() as empty_cache:
            client = FakeClient(empty_cache, failure=RuntimeError("must not call"))
            result = summarize(client, dict(self.record, ai=good), self.evidence, settings())
            self.assertEqual(result["status"], "generated")
            self.assertTrue(result["persistent_cache_hit"])
            self.assertEqual(client.post_attempts, 0)

    def test_summary_changed_evidence_same_ids_is_explicitly_stale_without_key(self):
        good = summarize(FakeClient(self.temp.name, ai_payload()), self.record, self.evidence, settings())
        changed = [{"id": "script-1", "text": "new repository commit removed the input file"}]
        result = summarize(FakeClient(self.temp.name), dict(self.record, ai=good), changed, settings(llm_api_key=""))
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["last_attempt_status"], "missing_key")
        self.assertNotEqual(result["current_source_fingerprint"], result["source_fingerprint"])
        self.assertEqual(result["summary_zh"], good["summary_zh"])

    def test_summary_budget_or_http_failure_keeps_previous_summary_stale(self):
        good = summarize(FakeClient(self.temp.name, ai_payload()), self.record, self.evidence, settings())
        changed = [{"id": "script-1", "text": "new repository commit changed the observed script"}]
        failures = ((BudgetExceeded("network_budget_exhausted"), "budget_exhausted", "network_budget_exhausted", None),
                    (FetchError("fixture-ai-secret DO_NOT_LEAK", 401), "authentication_error", "http_401", 401),
                    (FetchError("fixture-ai-secret DO_NOT_LEAK"), "network_error", "network_error", None))
        for failure, status, reason, code in failures:
            with self.subTest(status=status):
                result = summarize(FakeClient(self.temp.name, failure=failure), dict(self.record, ai=good), changed, settings())
                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["summary_zh"], good["summary_zh"])
                self.assertEqual(result["source_fingerprint"], good["source_fingerprint"])
                self.assertNotEqual(result["current_source_fingerprint"], good["source_fingerprint"])
                self.assertEqual((result["last_attempt_status"], result["error_reason"], result.get("http_status")),
                                 (status, reason, code))
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    def test_summary_invalid_endpoint_keeps_previous_summary_stale(self):
        good = summarize(FakeClient(self.temp.name, ai_payload()), self.record, self.evidence, settings())
        client = FakeClient(self.temp.name, ai_payload())
        result = summarize(client, dict(self.record, ai=good), self.evidence,
                           settings(llm_api_url="https://unlisted.example/fixture-ai-secret"))
        self.assertEqual((result["status"], result["last_attempt_status"], result["error_reason"]),
                         ("stale", "invalid_endpoint", "invalid_endpoint"))
        self.assertEqual(result["summary_zh"], good["summary_zh"])
        self.assertNotIn("fixture-ai-secret", json.dumps(result))
        self.assertEqual(client.post_attempts, 0)

    def test_summary_success_discards_old_failure_diagnostics(self):
        good = summarize(FakeClient(self.temp.name, ai_payload()), self.record, self.evidence, settings())
        old = dict(good, status="stale", last_attempt_status="authentication_error", error_reason="http_401", http_status=401)
        result = summarize(FakeClient(self.temp.name), dict(self.record, ai=old), self.evidence, settings())
        self.assertEqual(result["status"], "generated")
        self.assertTrue(result["persistent_cache_hit"])
        for field in ("last_attempt_status", "error_reason", "http_status", "stale_reason", "current_source_fingerprint"):
            self.assertNotIn(field, result)

    def test_summary_changed_model_recomputes_instead_of_persistent_reuse(self):
        client = FakeClient(self.temp.name, ai_payload())
        good = summarize(client, self.record, self.evidence, settings())
        changed = summarize(client, dict(self.record, ai=good), self.evidence, settings(llm_model="different-model"))
        self.assertEqual(client.post_attempts, 2)
        self.assertNotEqual(changed["summary_fingerprint"], good["summary_fingerprint"])
        self.assertEqual(changed["source_fingerprint"], good["source_fingerprint"])

    def test_summary_private_cache_reusable_without_paid_key(self):
        client = FakeClient(self.temp.name, ai_payload())
        summarize(client, self.record, self.evidence, settings())
        result = summarize(client, self.record, self.evidence, settings(llm_api_key=""))
        self.assertEqual(result["status"], "generated")
        self.assertTrue(result["cache_hit"])
        self.assertEqual(client.post_attempts, 1)

    def test_summary_mapping_evidence_supported(self):
        client = FakeClient(self.temp.name, ai_payload())
        result = summarize(client, self.record, {"script-1": "Observed script input expression.csv"}, settings())
        self.assertEqual(result["status"], "generated")

    def test_summary_maximum_limitations_remains_cacheable(self):
        client = FakeClient(self.temp.name, ai_payload(limitations=[f"未知条件 {i}" for i in range(10)]))
        first = summarize(client, self.record, self.evidence, settings())
        second = summarize(client, self.record, self.evidence, settings())
        self.assertEqual(len(first["limitations"]), 11)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(client.post_attempts, 1)


if __name__ == "__main__":
    unittest.main()
