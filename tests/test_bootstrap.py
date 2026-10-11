"""Bootstrap truth/fairness acceptance; synthetic assets only, no network/code runs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from paper_plot_library.bootstrap import (
    BOOTSTRAP_REASON_CODES, bootstrap_assessment, bootstrap_plan, bootstrap_report,
    ensure_bootstrap, note_bootstrap_attempt,
)
from test_pipeline_audit import article_good, record, repo_good


NOW = "2026-10-11T12:00:00Z"
_raster_stream = io.BytesIO()
Image.new("RGB", (20, 20), "white").save(_raster_stream, "PNG")
RASTER = _raster_stream.getvalue()  # A synthetic test asset, never a publication image.


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-bootstrap-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.ledger = {}

    def catalog(self, papers):
        return {"authorCodeExecuted": False, "papers": papers, "inputSpecs": {}}

    def full_case(self, index=1):
        value = record(index)
        value.update(article=article_good(), repository=repo_good(), figure="Figure 1")
        value["article"]["figures"][0]["sha256"] = hashlib.sha256(RASTER).hexdigest()
        repo = value["repository"]
        repo["scripts"][0]["url"] = repo["url"] + "/blob/" + repo["commit"] + "/plot.py"
        repo["inputs"] = [{"status": "header_observed", "headerSHA256": "c" * 64,
                           "fields": [{"name": "age", "kind": "actual_file_header"}]}]
        value["ai"] = {"status": "generated", "title_zh": "合成绘图静态学习案例",
                       "summary_zh": "合成静态学习注释；作者代码未执行。",
                       "learning_points": ["核对真实输入列名，再人工核对图号对应。"],
                       "limitations": ["作者代码未运行；静态检查不等于图形复现或结果验证。"],
                       "evidence_ids": ["publication", "script-0"]}
        value["metrics"] = {"status": "matched", "if": "12.3", "jcr": "Q1", "source": "EasyScholar", "retrieved_at": NOW}
        value["automation"] = {"status": "partial", "lastCheckedAt": NOW, "errors": [],
            "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "generated", "metrics": "matched"}}
        return value

    def test_fixed_cohort_snapshot_never_expands_with_later_admissions(self):
        catalog = self.catalog([record(1), record(2)])
        state = ensure_bootstrap(catalog, self.ledger, NOW)
        self.assertEqual(state["cohortDOIs"], [record(1)["doi"], record(2)["doi"]])
        catalog["papers"].append(record(3))
        second = ensure_bootstrap(catalog, self.ledger, "2026-10-12T12:00:00Z")
        self.assertEqual(second["cohortDOIs"], state["cohortDOIs"])
        self.assertEqual(second["startedAt"], NOW)

    def test_curated_stub_links_and_inspected_flag_are_not_completion(self):
        stub = record(1)
        stub.update(codeURL="https://github.com/fixture/plot", scriptURL="https://github.com/fixture/plot/blob/main/plot.py",
                    figureURL="https://example.org/paper/figure1", article={"status": "inspected"}, repository={"status": "inspected"})
        assessment = bootstrap_assessment(stub, root=self.root)
        self.assertEqual(assessment["status"], "pending")
        self.assertIn("selected_figure_missing", assessment["reasonCodes"])
        self.assertIn("code_not_pinned", assessment["reasonCodes"])
        self.assertIn("input_undocumented", assessment["reasonCodes"])
        self.assertIn("ai_missing", assessment["reasonCodes"])

    def test_full_static_materials_complete_without_faking_manual_mapping(self):
        case = self.full_case()
        assessment = bootstrap_assessment(case, root=self.root)
        self.assertEqual(assessment["status"], "complete")
        self.assertEqual(assessment["reasonCodes"], [])
        self.assertFalse(assessment["bindingVerified"])
        self.assertFalse(case["runPerformed"])
        self.assertNotIn("mappingReview", case)
        result = bootstrap_report(self.catalog([case]), self.ledger, self.root, NOW)
        self.assertEqual((result["status"], result["pendingCount"], result["attentionCount"]), ("complete", 0, 0))

    def test_actual_asset_bytes_digest_selection_and_symlink_all_gate_completion(self):
        case = self.full_case()
        self.assertIn("figure_asset_unverified", bootstrap_assessment(case)["reasonCodes"])
        wrong_hash = deepcopy(case)
        wrong_hash["article"]["figures"][0]["sha256"] = "0" * 64
        self.assertIn("figure_asset_unverified", bootstrap_assessment(wrong_hash, root=self.root)["reasonCodes"])
        wrong_figure = deepcopy(case)
        wrong_figure["figure"] = "Figure 2"
        self.assertIn("selected_figure_missing", bootstrap_assessment(wrong_figure, root=self.root)["reasonCodes"])
        removed = self.root / "data" / "assets" / "fixture.png"
        removed.unlink()
        self.assertIn("figure_asset_unverified", bootstrap_assessment(case, root=self.root)["reasonCodes"])
        outside = self.root / "outside.png"
        outside.write_bytes(RASTER)
        removed.symlink_to(outside)
        self.assertIn("figure_asset_unverified", bootstrap_assessment(case, root=self.root)["reasonCodes"])

    def test_no_unrelated_figure_substitution_for_unparseable_selected_label(self):
        case = self.full_case()
        case["figure"] = "unknown selected drawing"
        self.assertIn("selected_figure_missing", bootstrap_assessment(case, root=self.root)["reasonCodes"])

    def test_corrupt_raster_with_matching_hash_does_not_finish_bootstrap(self):
        case = self.full_case()
        corrupt = b"\x89PNG\r\n\x1a\ncorrupt raster fixture"
        (self.root / "data" / "assets" / "fixture.png").write_bytes(corrupt)
        case["article"]["figures"][0]["sha256"] = hashlib.sha256(corrupt).hexdigest()
        self.assertIn("figure_asset_unverified", bootstrap_assessment(case, root=self.root)["reasonCodes"])

    def test_branch_link_or_missing_code_hash_is_not_fixed_author_code(self):
        for key, value in (("url", "https://github.com/fixture/plot/blob/main/plot.py"), ("sha256", "")):
            with self.subTest(key=key):
                case = self.full_case()
                case["repository"]["scripts"][0][key] = value
                self.assertIn("script_integrity_missing", bootstrap_assessment(case, root=self.root)["reasonCodes"])
        unlinked = self.full_case()
        unlinked["article"]["codeURLEvidence"] = []
        self.assertIn("author_code_link_unverified", bootstrap_assessment(unlinked, root=self.root)["reasonCodes"])
        unlinked["codeURL"] = unlinked["repository"]["url"]
        self.assertNotIn("author_code_link_unverified", bootstrap_assessment(unlinked, root=self.root)["reasonCodes"])

    def test_selected_documented_input_notes_count_but_code_only_field_names_do_not(self):
        case = self.full_case()
        case["repository"]["inputs"] = [{"fields": [{"name": "age", "kind": "code_field_reference"}]}]
        self.assertIn("input_undocumented", bootstrap_assessment(case, root=self.root)["reasonCodes"])
        spec = {"files": [{"name": "input.csv", "fields": [["age", "numeric", "作者脚本字段引用", "单位未核"]]}]}
        assessed = bootstrap_assessment(case, spec, self.root)
        self.assertEqual(assessed["status"], "complete")
        self.assertFalse(assessed["bindingVerified"])

    def test_metrics_query_not_found_can_complete_without_inventing_if(self):
        case = self.full_case()
        case["metrics"] = {"status": "not_found", "if": "", "jcr": "", "source": "EasyScholar", "retrieved_at": NOW}
        self.assertEqual(bootstrap_assessment(case, root=self.root)["status"], "complete")
        case["metrics"].pop("retrieved_at")
        self.assertIn("metrics_unchecked", bootstrap_assessment(case, root=self.root)["reasonCodes"])
        case["metrics"] = {"status": "matched", "if": {}, "source": "EasyScholar", "retrieved_at": NOW}
        self.assertIn("metrics_unchecked", bootstrap_assessment(case, root=self.root)["reasonCodes"])

    def test_retained_old_sources_and_stale_ai_never_complete(self):
        for source in ("article", "repository"):
            with self.subTest(source=source):
                case = self.full_case()
                case[source]["retained_from_previous"] = True
                self.assertIn("source_refresh_failed", bootstrap_assessment(case, root=self.root)["reasonCodes"])
        case = self.full_case()
        case["ai"]["status"] = "stale"
        self.assertIn("ai_stale", bootstrap_assessment(case, root=self.root)["reasonCodes"])

    def test_three_real_unsuccessful_scans_mark_review_not_complete_and_stop_auto_retries(self):
        case = self.full_case()
        case["article"]["figures"][0]["reuseStatus"] = "link_only"
        catalog = self.catalog([case])
        ensure_bootstrap(catalog, self.ledger, NOW)
        for count in range(1, 4):
            state = note_bootstrap_attempt(case, None, NOW, self.root)
            self.assertEqual(state["attempts"], count)
            self.assertEqual(state["unsuccessfulScans"], count)
        self.assertEqual(state["status"], "needs_attention")
        result = bootstrap_report(catalog, self.ledger, self.root, NOW)
        self.assertEqual((result["status"], result["completeCount"], result["pendingCount"], result["attentionCount"]),
                         ("needs_attention", 0, 0, 1))
        self.assertEqual(bootstrap_plan(catalog, self.ledger, 5, NOW, self.root), [])
        self.assertEqual(len(bootstrap_plan(catalog, self.ledger, 5, NOW, self.root, retry_attention=True)), 1)
        case["article"]["figures"][0]["reuseStatus"] = "allowed"
        recovered = note_bootstrap_attempt(case, None, NOW, self.root)
        self.assertEqual(recovered["status"], "complete")

    def test_budget_deferral_never_consumes_failure_allowance_even_after_many_runs(self):
        case = self.full_case()
        case["ai"] = {"status": "budget_exhausted"}
        case["automation"]["stages"]["ai"] = "budget_exhausted"
        for _ in range(20):
            state = note_bootstrap_attempt(case, None, NOW, self.root)
        self.assertEqual((state["attempts"], state["unsuccessfulScans"], state["status"]), (0, 0, "pending"))
        self.assertNotIn("nextAttemptAt", state)

    def test_successful_collection_starts_a_new_allowance_for_later_regressions(self):
        case = self.full_case()
        case["article"]["figures"][0]["reuseStatus"] = "link_only"
        for _ in range(2):
            note_bootstrap_attempt(case, None, NOW, self.root)
        case["article"]["figures"][0]["reuseStatus"] = "allowed"
        self.assertEqual(note_bootstrap_attempt(case, None, NOW, self.root)["status"], "complete")
        case["article"]["retained_from_previous"] = True
        case["automation"]["stages"]["article"] = "unavailable"
        case["automation"]["stages"]["ai"] = "missing_evidence"
        for count in range(1, 4):
            state = note_bootstrap_attempt(case, None, NOW, self.root)
            self.assertEqual(state["attempts"], count)
            self.assertEqual(state["status"], "needs_attention" if count == 3 else "pending")

    def test_retained_yesterday_budget_does_not_hide_today_source_failure(self):
        for old_component in ("ai", "metrics", "both"):
            with self.subTest(old_component=old_component):
                case = self.full_case()
                if old_component in {"ai", "both"}:
                    case["ai"] = {"status": "budget_exhausted"}
                if old_component in {"metrics", "both"}:
                    case["metrics"]["last_attempt_status"] = "budget_exhausted"
                case["automation"]["stages"] = {"metadata": "network_error", "ai": "missing_evidence", "metrics": "not_checked"}
                assessment = bootstrap_assessment(case, root=self.root)
                self.assertFalse(assessment["budgetDeferred"])
                for _ in range(3):
                    state = note_bootstrap_attempt(case, None, NOW, self.root)
                self.assertEqual((state["attempts"], state["status"]), (3, "needs_attention"))
                self.assertNotIn("request_budget_deferred", state["reasonCodes"])

    def test_explicit_current_budget_deferral_does_not_consume_source_attempt(self):
        case = self.full_case()
        case["automation"]["stages"] = {"metadata": "network_error"}
        state = note_bootstrap_attempt(case, None, NOW, self.root, attempt_status="budget_exhausted")
        self.assertEqual((state["attempts"], state["unsuccessfulScans"], state["status"]), (0, 0, "pending"))
        self.assertIn("request_budget_deferred", state["reasonCodes"])

    def test_retained_old_config_or_auth_diagnostics_do_not_create_new_human_blocker(self):
        case = self.full_case()
        case["ai"] = {"status": "authentication_error"}
        case["metrics"] = {"status": "missing_key"}
        case["automation"]["stages"] = {"metadata": "network_error", "ai": "missing_evidence", "metrics": "not_checked"}
        state = note_bootstrap_attempt(case, None, NOW, self.root)
        self.assertEqual((state["attempts"], state["status"]), (1, "pending"))
        self.assertNotIn("ai_authentication_failed", state["reasonCodes"])
        self.assertNotIn("metrics_configuration_missing", state["reasonCodes"])

    def test_generated_flag_alone_invalid_evidence_and_reproduction_claim_never_complete(self):
        invalid_values = [
            {"status": "generated", "summary_zh": "随意文字"},
            {"summary_zh": "已成功复现作者原图。"},
            {"evidence_ids": ["fabricated-evidence"]},
            {"learning_points": ["代码运行成功，与原图完全一致。"]},
            {"author_code_executed": True},
            {"current_source_fingerprint": "f" * 64, "source_fingerprint": "e" * 64},
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                case = self.full_case()
                if set(value) == {"status", "summary_zh"}:
                    case["ai"] = value
                else:
                    case["ai"].update(value)
                assessed = bootstrap_assessment(case, root=self.root)
                self.assertNotEqual(assessed["status"], "complete")
                self.assertIn("ai_response_invalid", assessed["reasonCodes"])

    def test_pure_unknown_input_placeholders_do_not_complete_but_uncertainty_notes_remain_valid(self):
        case = self.full_case()
        case["repository"]["inputs"] = []
        for field in (["列名待核", "未知", "来源待核"], ["unknown", "unknown", "source pending"],
                      ["age", "未知", "待核"], ["[TBD]", "numeric", "作者脚本字段引用"]):
            with self.subTest(field=field):
                assessment = bootstrap_assessment(case, {"files": [{"fields": [field]}]}, self.root)
                self.assertIn("input_undocumented", assessment["reasonCodes"])
        true_notes = {"files": [{"fields": [["age", "单位未核", "作者代码明确读取；单位待核", "方向待核"]]}]}
        self.assertEqual(bootstrap_assessment(case, true_notes, self.root)["status"], "complete")

    def test_missing_ai_credentials_authentication_and_balance_need_human_action(self):
        for value, reason in (("missing_key", "ai_configuration_missing"),
                              ("authentication_error", "ai_authentication_failed"),
                              ("payment_required", "ai_payment_required")):
            with self.subTest(value=value):
                case = self.full_case()
                case["ai"] = {"status": value}
                case["automation"]["stages"]["ai"] = value
                # Do not block the first bootstrap attempt: secrets may have been fixed.
                self.assertEqual(bootstrap_assessment(case, root=self.root)["status"], "pending")
                state = note_bootstrap_attempt(case, None, NOW, self.root)
                self.assertEqual(state["status"], "needs_attention")
                self.assertIn(reason, state["reasonCodes"])

    def test_transient_source_failures_stop_after_three_attempts_not_three_fake_scans(self):
        case = record(1)
        case["automation"]["lastCheckedAt"] = NOW
        case["automation"]["stages"] = {"metadata": "network_error"}
        for _ in range(3):
            state = note_bootstrap_attempt(case, None, NOW, self.root)
        self.assertEqual(state["status"], "needs_attention")
        self.assertEqual(state["attempts"], 3)
        self.assertEqual(state["unsuccessfulScans"], 0)
        self.assertIn("source_transient_failure", state["reasonCodes"])

    def test_fifty_unattempted_cases_are_selected_before_repeating_failures(self):
        papers = [record(i) for i in range(1, 51)]
        catalog = self.catalog(papers)
        visited = set()
        for round_index in range(10):
            now = (datetime(2026, 10, 11, 12, tzinfo=timezone.utc) + timedelta(hours=round_index)).isoformat().replace("+00:00", "Z")
            plan = bootstrap_plan(catalog, self.ledger, 5, now, self.root)
            self.assertEqual(len(plan), 5)
            for case in plan:
                self.assertNotIn(case["id"], visited)
                visited.add(case["id"])
                case["automation"]["stages"] = {"metadata": "network_error"}
                case["automation"]["lastCheckedAt"] = now
                note_bootstrap_attempt(case, None, now, self.root)
        self.assertEqual(len(visited), 50)

    def test_50_full_cases_progress_partition_does_not_complete_while_one_attention_exists(self):
        papers = [self.full_case(i) for i in range(1, 51)]
        papers[0]["repository"]["inputs"] = []
        for _ in range(3):
            note_bootstrap_attempt(papers[0], None, NOW, self.root)
        report = bootstrap_report(self.catalog(papers), self.ledger, self.root, NOW)
        self.assertEqual((report["totalCount"], report["completeCount"], report["pendingCount"], report["attentionCount"]), (50, 49, 0, 1))
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["issues"][0]["reasonCodes"], ["input_undocumented"])

    def test_removed_cohort_record_requires_attention_not_silent_drop(self):
        catalog = self.catalog([self.full_case(1), self.full_case(2)])
        ensure_bootstrap(catalog, self.ledger, NOW)
        catalog["papers"].pop()
        report = bootstrap_report(catalog, self.ledger, self.root, NOW)
        self.assertEqual((report["totalCount"], report["attentionCount"], report["status"]), (2, 1, "needs_attention"))
        self.assertEqual(report["issues"][0]["reasonCodes"], ["cohort_record_missing"])

    def test_malformed_state_is_fail_closed_without_recapturing_cohort(self):
        for state in ({"version": True}, {"version": 1, "cohortDOIs": [""], "startedAt": NOW, "status": "active"},
                      {"version": 1, "cohortDOIs": [record(1)["doi"]], "startedAt": NOW, "status": []}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                ensure_bootstrap(self.catalog([record(2)]), {"bootstrap": state}, NOW)

    def test_lost_ledger_with_existing_catalog_progress_cannot_recapture_enlarged_cohort(self):
        catalog = self.catalog([record(1)])
        ensure_bootstrap(catalog, self.ledger, NOW)
        catalog["bootstrap"] = bootstrap_report(catalog, self.ledger, self.root, NOW)
        catalog["papers"].append(record(2))  # A later daily admission is not initial cohort.
        missing = {}
        with self.assertRaisesRegex(ValueError, "恢复 data/ledger.json"):
            ensure_bootstrap(catalog, missing, NOW)
        self.assertEqual(missing, {})
        with self.assertRaises(ValueError):
            ensure_bootstrap(self.catalog([record(1)]), {"bootstrap": None}, NOW)

    def test_safe_reason_projection_and_corrupt_future_cooldown_cannot_freeze(self):
        case = record(1)
        case["automation"]["bootstrap"] = {"attempts": True, "unsuccessfulScans": {}, "status": [],
            "lastAttemptAt": NOW, "nextAttemptAt": "2099-01-01T00:00:00Z", "reasonCodes": ["secretKey=fixture", []]}
        assessment = bootstrap_assessment(case, root=self.root)
        self.assertTrue(set(assessment["reasonCodes"]) <= BOOTSTRAP_REASON_CODES)
        self.assertEqual(len(bootstrap_plan(self.catalog([case]), self.ledger, 5, NOW, self.root)), 1)
        case["automation"]["stages"] = {"metadata": [], "article": {}, "ai": []}
        case["ai"] = {"status": []}
        case["metrics"] = {"status": {}}
        self.assertEqual(bootstrap_assessment(case, root=self.root)["status"], "pending")

    def test_empty_cohort_completes_without_inventing_cases(self):
        report = bootstrap_report(self.catalog([]), self.ledger, self.root, NOW)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(sum(report[key] for key in ("completeCount", "pendingCount", "attentionCount")), 0)


if __name__ == "__main__":
    unittest.main()
