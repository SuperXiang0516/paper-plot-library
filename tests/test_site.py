import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image

from paper_plot_library.site import build_site, public_catalog
from paper_plot_library.release_export import export_site


PROJECT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "/Users/xiangzhoufu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
_raster_stream = io.BytesIO()
Image.new("RGB", (2000, 1200), "white").save(_raster_stream, "PNG")
RASTER = _raster_stream.getvalue()  # Synthetic asset, never a publication image.
RASTER_SHA256 = hashlib.sha256(RASTER).hexdigest()


def sample():
    return {
        "schemaVersion": 4, "authorCodeExecuted": False,
        "generatedAt": "2026-10-11T12:00:00Z", "papers": [{
            "id": "paper-1", "paper": "<img src=x onerror=alert(1)>", "doi": "10.1000/test",
            "graphForms": ["scatter", "heatmap"], "formClassificationStatus": "approved",
            "analysisFunctions": ["association"], "functionClassificationStatus": "approved",
            "runPerformed": False, "paperURL": "https://example.org/paper", "api_key": "must-not-publish",
            "abstract": "Private fetched full abstract is not a public response dump.",
            "article": {"status": "ok", "license": "CC BY 4.0", "figures": [{
                "id": "fig-1", "label": "Figure 1", "asset": "assets/paper-1/fig-1.png",
                "reuseStatus": "allowed", "sourceURL": "https://example.org/image.png", "width": 2000, "height": 1200,
                "sha256": RASTER_SHA256,
            }], "raw_response": "must-not-publish"},
            "repository": {"status": "partial", "scripts": [{"path": "plot.py", "url": "https://github.com/author/repo/blob/123/plot.py", "raw_code": "must-not-publish", "evidence": ["L2: input-file read"]}]},
            "metrics": {"status": "ok", "if": "5.2", "jcr": "Q1", "source": "https://example.org/metrics?secret=must-not-publish"},
            "ai": {"status": "ok", "summary_zh": "中文解读", "review_required": True, "prompt": "must-not-publish"},
        }],
        "inputSpecs": {"paper-1": {"tree": "data/input.csv", "files": [{"name": "input.csv", "fields": [["age", "number", "actual header", "age in years"]]}]}, "unrelated": {"tree": "must-not-publish"}},
        "dimensions": {"forms": {"all": [{"id": "scatter", "label": "散点图"}, {"id": "heatmap", "label": "热图"}]}, "functions": {"all": [{"id": "association", "label": "相关分析"}]}},
    }


def sample_session():
    return {"version": 1, "id": "a" * 32, "status": "paused", "endReason": "time_limit_reached",
            "startedAt": "2026-10-11T12:00:00Z", "updatedAt": "2026-10-11T12:45:00Z",
            "targetCount": 55, "plannedCount": 10, "processedCount": 8, "remainingCount": 45,
            "batchCount": 2, "checkpointCount": 7, "requests": 190, "aiAttempts": 17,
            "uncertainRequests": 150, "uncertainAiAttempts": 15, "inFlightCount": 2,
            "maxBatches": 12, "maxRequests": 1800, "maxAiAttempts": 165,
            "completeCount": 2, "pendingCount": 53, "attentionCount": 0,
            "removedCount": 0,
            "materialsStatus": "active", "localCheckpointSaved": True}


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(PROJECT / "web", self.root / "web")
        image = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(RASTER)

    def tearDown(self):
        self.temporary.cleanup()

    def assert_fixture_not_published(self, output):
        public = json.loads((output / "data" / "catalog.json").read_text())
        self.assertEqual(public["papers"], [], "A case without a verified reusable main figure is not a public gallery entry.")
        self.assertNotIn("paper-1", public["inputSpecs"])
        self.assertEqual(json.loads((output / "data" / "assets-manifest.json").read_text()), [])

    def test_builds_static_site_and_whitelisted_data(self):
        catalog = sample()
        output = build_site(self.root, catalog, [{"status": "ok", "newCount": 1, "settings": {"token": "must-not-publish"}}])
        serialized = (output / "data" / "catalog.json").read_text()
        result = json.loads(serialized)
        self.assertNotIn("must-not-publish", serialized)
        self.assertNotIn("abstract", result["papers"][0])
        self.assertNotIn("unrelated", result["inputSpecs"])
        self.assertNotIn("raw_code", result["papers"][0]["repository"]["scripts"][0])
        self.assertTrue(result["papers"][0]["ai"]["review_required"])
        self.assertTrue((output / "assets" / "paper-1" / "fig-1.png").is_file())
        self.assertEqual(catalog, sample(), "Build must not mutate persistent source catalogue.")
        self.assertNotIn("must-not-publish", (output / "data" / "updates.json").read_text())
        self.assertEqual((output / "app.js").read_bytes(), (self.root / "web" / "app.js").read_bytes())

    def test_license_change_removes_previously_owned_image_not_user_files(self):
        catalog = sample()
        output = build_site(self.root, catalog, [])
        user_file = output / "assets" / "user-note.txt"
        user_file.write_text("leave untouched")
        catalog["papers"][0]["article"]["figures"][0]["reuseStatus"] = "link_only"
        build_site(self.root, catalog, [])
        self.assertFalse((output / "assets" / "paper-1" / "fig-1.png").exists())
        self.assertEqual(user_file.read_text(), "leave untouched")
        self.assert_fixture_not_published(output)

    def test_missing_image_cannot_be_displayed_as_available(self):
        catalog = sample()
        catalog["papers"][0]["article"]["figures"][0]["asset"] = "assets/absent.png"
        original = copy.deepcopy(catalog)
        output = build_site(self.root, catalog, [])
        self.assert_fixture_not_published(output)
        self.assertEqual(catalog, original, "Publication filtering must not remove persistent source records.")

    def test_public_catalog_remains_pure_sanitization_without_file_admission(self):
        catalog = sample()
        catalog["papers"][0]["article"]["figures"][0]["asset"] = "assets/absent.png"
        self.assertEqual(len(public_catalog(catalog)["papers"]), 1)
        self.assert_fixture_not_published(build_site(self.root, catalog, []))

    def test_valid_body_image_admission_does_not_require_ai_code_or_metrics(self):
        catalog = sample()
        paper = catalog["papers"][0]
        for field in ("ai", "repository", "metrics"):
            paper.pop(field)
        catalog["inputSpecs"] = {}
        original = copy.deepcopy(catalog)
        output = build_site(self.root, catalog, [])
        public = json.loads((output / "data" / "catalog.json").read_text())
        self.assertEqual(len(public["papers"]), 1)
        published = public["papers"][0]
        self.assertTrue(published["availability"]["hasImage"])
        self.assertFalse(published["availability"]["hasCode"])
        self.assertFalse(published["availability"]["hasAi"])
        self.assertFalse(published["availability"]["hasMetrics"])
        self.assertEqual(published["availability"]["verification"], "unverified")
        self.assertFalse(published["runPerformed"])
        self.assertFalse(public["authorCodeExecuted"])
        self.assertEqual(catalog, original)

    def test_preserves_selected_inputs_automatic_candidates_and_stale_ai_separately(self):
        catalog = sample()
        selected = copy.deepcopy(catalog["inputSpecs"]["paper-1"]["files"])
        candidates = {"tree": "unrelated/input.csv", "status": "全仓库候选", "files": [{"name": "unrelated/input.csv", "fields": [["cohort", "未知", "实际表头", "待核"]], "observedFieldReferences": [{"name": "age", "line": 12, "kind": "code_field_reference"}]}]}
        catalog["inputSpecs"]["paper-1"]["automaticInputs"] = candidates
        catalog["papers"][0]["ai"].update(status="stale", stale_reason="来源已更新", last_attempt_status="missing_key", generated_at="2026-10-10T12:00:00Z")
        output = build_site(self.root, catalog, [])
        public = json.loads((output / "data" / "catalog.json").read_text())
        self.assertEqual(public["inputSpecs"]["paper-1"]["files"], selected)
        self.assertEqual(public["inputSpecs"]["paper-1"]["automaticInputs"], candidates)
        self.assertEqual(public["papers"][0]["ai"]["status"], "stale")
        self.assertEqual(public["papers"][0]["ai"]["stale_reason"], "来源已更新")

    def test_failure_provenance_and_metric_retention_survive_public_projection(self):
        catalog = sample()
        catalog["papers"][0]["article"]["licenseURL"] = "https://creativecommons.org/licenses/by/4.0/"
        catalog["papers"][0]["repository"].update(lastError="GitHub 403", errorStatus=403, last_attempt_status="unavailable", retained_from_previous=True)
        catalog["papers"][0]["metrics"].update(retained_field_asof={"if": "2025-01-01T00:00:00Z"}, persistent_cache_hit=True, last_attempt_status="error")
        projected = public_catalog(catalog)["papers"][0]
        self.assertEqual(projected["article"]["licenseURL"], "https://creativecommons.org/licenses/by/4.0/")
        self.assertEqual(projected["repository"]["errorStatus"], 403)
        self.assertTrue(projected["repository"]["retained_from_previous"])
        self.assertEqual(projected["metrics"]["retained_field_asof"]["if"], "2025-01-01T00:00:00Z")
        self.assertTrue(projected["metrics"]["persistent_cache_hit"])

    def test_allows_only_safe_enrichment_diagnostics(self):
        for status, http in (("invalid_request", 400), ("authentication_error", 401), ("payment_required", 402),
                             ("permission_denied", 403), ("endpoint_not_found", 404), ("rate_limited", 429),
                             ("service_unavailable", 503), ("other_http_error", 418)):
            catalog = sample()
            catalog["papers"][0]["ai"].update(status="stale", last_attempt_status=status, error_reason=f"http_{http}", http_status=http)
            catalog["papers"][0]["metrics"].update(status="matched", last_attempt_status=status, error_reason=f"http_{http}", http_status=http)
            projected = public_catalog(catalog)["papers"][0]
            self.assertEqual(projected["ai"]["status"], "stale")
            self.assertEqual(projected["metrics"]["status"], "matched")
            for field in ("ai", "metrics"):
                self.assertEqual(projected[field]["last_attempt_status"], status)
                self.assertEqual(projected[field]["error_reason"], f"http_{http}")
                self.assertEqual(projected[field]["http_status"], http)
        for reason in ("network_budget_exhausted", "collection_budget_exhausted", "ai_budget_exhausted", "invalid_endpoint", "network_error", "error"):
            catalog = sample()
            catalog["papers"][0]["ai"].update(status="budget_exhausted", error_reason=reason)
            self.assertEqual(public_catalog(catalog)["papers"][0]["ai"]["error_reason"], reason)
        for status in (True, 399, 600, "401", float("nan")):
            catalog = sample()
            catalog["papers"][0]["ai"].update(status="credentials-in-exception", last_attempt_status="credentials-in-exception",
                                               error_reason="Fetch failed with credentials-in-exception", http_status=status)
            projected = public_catalog(catalog)["papers"][0]["ai"]
            self.assertEqual(projected["status"], "unknown")
            self.assertEqual(projected["last_attempt_status"], "unknown")
            self.assertNotIn("error_reason", projected)
            self.assertNotIn("http_status", projected)
            self.assertNotIn("credentials-in-exception", json.dumps(projected))

    def test_export_keeps_budget_counters_and_safe_retained_diagnostics(self):
        catalog = sample()
        catalog["papers"][0]["ai"].update(status="stale", last_attempt_status="budget_exhausted", error_reason="collection_budget_exhausted")
        catalog["papers"][0]["metrics"].update(status="matched", last_attempt_status="rate_limited", error_reason="http_429", http_status=429)
        report = {"startedAt": "2026-10-11T12:00:00Z", "status": "partial", "requests": 145, "aiAttempts": 0,
                  "reservedAiRequests": 5, "collectionRequests": 145, "maxRequests": 150, "maxAiCalls": 5,
                  "aiRepairCount": 0, "collectionBudgetExhausted": True,
                  "stageCounts": {"ai": {"budget_exhausted": 1}}, "privateDiagnostic": "credentials-in-exception"}
        (self.root / "data" / "catalog.json").write_text(json.dumps(catalog))
        (self.root / "data" / "updates.json").write_text(json.dumps([report]))
        output = export_site(self.root)
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
        self.assertEqual(projected["ai"]["error_reason"], "collection_budget_exhausted")
        self.assertEqual(projected["metrics"]["http_status"], 429)
        exported_report = json.loads((output / "data" / "updates.json").read_text())[0]
        for field in ("reservedAiRequests", "collectionRequests", "maxRequests", "maxAiCalls", "aiRepairCount", "collectionBudgetExhausted"):
            self.assertEqual(exported_report[field], report[field])
        self.assertNotIn("privateDiagnostic", exported_report)
        self.assertEqual(json.loads((self.root / "data" / "catalog.json").read_text()), catalog)
        self.assertEqual(json.loads((self.root / "data" / "updates.json").read_text()), [report])

    def test_drops_invalid_new_report_budget_counters(self):
        report = {"reservedAiRequests": True, "collectionRequests": -1, "maxRequests": 1000001,
                  "maxAiCalls": "5", "aiRepairCount": float("nan"), "collectionBudgetExhausted": "yes"}
        output = build_site(self.root, sample(), [report])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [{}])

    def test_detailed_ai_and_metric_reasons_are_public_without_provider_dumps(self):
        reasons = ("response_truncated", "response_shape_invalid", "response_finish_reason_invalid", "response_content_invalid",
                   "response_content_too_long", "response_markdown_fence", "response_json_malformed", "summary_shape_invalid",
                   "summary_fields_invalid", "summary_text_invalid", "summary_chinese_missing", "summary_list_invalid",
                   "summary_evidence_invalid", "summary_reproduction_claim", "metrics_response_shape_invalid",
                   "metrics_provider_error", "metrics_rank_shape_invalid", "metrics_not_found", "missing_journal")
        for reason in reasons:
            catalog = sample()
            for field in ("ai", "metrics"):
                catalog["papers"][0][field].update(status="invalid_response", error_reason=reason, raw_response="must-not-publish")
            projected = public_catalog(catalog)["papers"][0]
            for field in ("ai", "metrics"):
                self.assertEqual(projected[field]["error_reason"], reason)
                self.assertNotIn("raw_response", projected[field])

    def test_published_availability_uses_valid_assets_fixed_code_and_distinct_binding(self):
        catalog = sample()
        paper = catalog["papers"][0]
        commit = "a" * 40
        paper["figure"] = "Figure 1a"
        paper["repository"].update(status="inspected", commit=commit)
        paper["repository"]["scripts"][0].update(url=f"https://github.com/author/repo/blob/{commit}/plot.R", sha256="b" * 64)
        paper["automation"] = {"status": "partial", "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "authentication_error"}}
        original = copy.deepcopy(catalog)
        output = build_site(self.root, catalog, [])
        published = json.loads((output / "data" / "catalog.json").read_text())
        availability = published["papers"][0]["availability"]
        self.assertTrue(availability["browsable"])
        self.assertEqual(availability["verification"], "unverified")
        self.assertEqual(availability["health"], "error")
        self.assertTrue(availability["hasInputSchema"])
        self.assertFalse(availability["hasObservedHeaders"], "Curated notes do not prove a real file header.")
        self.assertEqual(availability["inputState"], "documented")
        self.assertEqual(published["availabilityCounts"], {"total": 1, "browsable": 1, "candidates": 0, "excluded": 0, "verified": 0, "errors": 1})
        self.assertEqual(catalog, original)
        if Path(NODE).exists():
            test = "const u=require(process.argv[1]),c=require(process.argv[2]);for(const p of c.papers){const a=u.availabilityOf(p,c.inputSpecs[p.id]);for(const [k,v] of Object.entries(p.availability))require('assert').deepEqual(a[k],v,k);}require('assert').deepEqual(u.collectionCounts(c.papers,c.inputSpecs),c.availabilityCounts);"
            subprocess.run([NODE, "-e", test, str(PROJECT / "web" / "app.js"), str(output / "data" / "catalog.json")], check=True, capture_output=True)
        for override in ({"figure": "Figure 2a"}, {"figure": "unparsed figure"}):
            paper.update(override)
            output = build_site(self.root, catalog, [])
            availability = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["availability"]
            self.assertFalse(availability["browsable"])
        paper["figure"] = "Figure 1a"
        paper["article"]["figures"][0]["asset"] = "assets/missing.png"
        output = build_site(self.root, catalog, [])
        self.assert_fixture_not_published(output)

    def test_published_observed_header_requires_digest_and_source_header_status(self):
        catalog = sample()
        source_input = {"name": "input.csv", "status": "header_observed", "headerSHA256": "a" * 64,
                        "fields": [{"name": "age", "kind": "actual_file_header"}]}
        catalog["papers"][0]["repository"]["inputs"] = [source_input]
        output = build_site(self.root, catalog, [])
        availability = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["availability"]
        self.assertTrue(availability["hasObservedHeaders"])
        self.assertEqual(availability["inputState"], "observed_header")
        source_input["headerSHA256"] = ""
        output = build_site(self.root, catalog, [])
        availability = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["availability"]
        self.assertFalse(availability["hasObservedHeaders"])

    def test_schedule_report_counts_survive_export_but_are_type_bounded(self):
        fields = ("untouchedRepairCount", "retryRepairCount", "cooldownSkippedCount", "untouchedRemainingCount", "automaticCompleteCount", "incompleteCount")
        report = {field: index for index, field in enumerate(fields)}
        report["discoveryStatus"] = "daily_quota_reached"
        output = build_site(self.root, sample(), [report])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [report])
        report.update({field: True for field in fields})
        output = build_site(self.root, sample(), [report])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [{"discoveryStatus": "daily_quota_reached"}])
        output = build_site(self.root, sample(), [{"discoveryStatus": "paper_batch_limit_reached"}])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [{"discoveryStatus": "paper_batch_limit_reached"}])

    def test_paid_retry_report_counters_and_fatal_statuses_are_controlled(self):
        fields = ("paperBatchLimit", "maxAiAttemptsPerPaper", "aiCasesAttempted", "aiRetriedCaseCount", "aiRetrySuccessCount")
        report = {"paperBatchLimit": 5, "maxAiAttemptsPerPaper": 3, "aiCasesAttempted": 5, "aiRetriedCaseCount": 2,
                  "aiRetrySuccessCount": 1, "aiStoppedStatus": "authentication_error"}
        output = build_site(self.root, sample(), [report])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [report])
        for status in ("missing_key", "missing_model", "missing_endpoint", "invalid_endpoint", "authentication_error",
                       "payment_required", "permission_denied", "invalid_request", "endpoint_not_found"):
            output = build_site(self.root, sample(), [{"aiStoppedStatus": status}])
            self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [{"aiStoppedStatus": status}])
        for invalid in (True, -1, 1000001, "secret-provider-response", float("nan")):
            output = build_site(self.root, sample(), [{**{field: invalid for field in fields}, "aiStoppedStatus": invalid}])
            self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [{}])
        for status in ([], {}, "network_error", "budget_exhausted", "secret-provider-response"):
            output = build_site(self.root, sample(), [{"aiStoppedStatus": status}])
            self.assertEqual(json.loads((output / "data" / "updates.json").read_text()), [{}])

    def test_ai_per_paper_retry_counters_are_integer_bounded_without_secrets(self):
        fields = ("ai_attempts", "max_ai_attempts", "retry_success_count")
        catalog = sample()
        catalog["papers"][0]["ai"].update(ai_attempts=3, max_ai_attempts=3, retry_success_count=1)
        ai = public_catalog(catalog)["papers"][0]["ai"]
        self.assertEqual({field: ai[field] for field in fields}, {"ai_attempts": 3, "max_ai_attempts": 3, "retry_success_count": 1})
        for invalid in (True, -1, 6, "secret-provider-response", float("nan")):
            catalog["papers"][0]["ai"].update({field: invalid for field in fields})
            ai = public_catalog(catalog)["papers"][0]["ai"]
            self.assertTrue(all(field not in ai for field in fields))
            self.assertNotIn("secret-provider-response", json.dumps(ai))

    def test_bootstrap_public_summary_preserves_progress_not_private_cohort_state(self):
        catalog = sample()
        bootstrap = {"status": "active", "totalCount": 3, "completeCount": 1, "pendingCount": 1, "attentionCount": 1,
                     "startedAt": "2026-10-11T12:00:00Z", "cohortIDs": ["must-not-publish"], "api_key": "must-not-publish",
                     "issues": [{"doi": "10.1000/test", "reasonCodes": ["figure_reuse_blocked", "input_undocumented", "must-not-publish"],
                                 "raw_response": "must-not-publish"},
                                {"doi": "https://provider.example/?secret=must-not-publish", "reasonCodes": ["ai_missing"]}]}
        catalog["bootstrap"] = copy.deepcopy(bootstrap)
        report = {"mode": "backfill", "bootstrap": bootstrap, "discoveryStatus": "bootstrap_in_progress"}
        original = copy.deepcopy(catalog)
        output = build_site(self.root, catalog, [report])
        published = json.loads((output / "data" / "catalog.json").read_text())
        exported_report = json.loads((output / "data" / "updates.json").read_text())[0]
        expected = {"status": "active", "totalCount": 3, "completeCount": 1, "pendingCount": 1, "attentionCount": 1,
                    "startedAt": "2026-10-11T12:00:00Z", "issues": [{"doi": "10.1000/test", "reasonCodes": ["figure_reuse_blocked", "input_undocumented"]}]}
        self.assertEqual(published["bootstrap"], expected)
        self.assertEqual(exported_report["bootstrap"], expected)
        self.assertNotIn("must-not-publish", json.dumps(published))
        self.assertNotIn("must-not-publish", json.dumps(exported_report))
        self.assertEqual(catalog, original)
        self.assertFalse(published["papers"][0]["runPerformed"])

    def test_bootstrap_counts_cannot_claim_completion_with_remaining_issues(self):
        invalid = [None, {}, {"status": []}, {"status": {}}, {"status": "needs_attention", "totalCount": True, "completeCount": 0, "pendingCount": 0, "attentionCount": 1},
                   {"status": "complete", "totalCount": 2, "completeCount": 1, "pendingCount": 1, "attentionCount": 0},
                   {"status": "complete", "totalCount": 2, "completeCount": 2, "pendingCount": 0, "attentionCount": 0,
                    "issues": [{"doi": "10.1000/test", "reasonCodes": ["figure_reuse_blocked"]}]},
                   {"status": "active", "totalCount": 2, "completeCount": 0, "pendingCount": 1, "attentionCount": 0},
                   {"status": "complete", "totalCount": 10001, "completeCount": 10001, "pendingCount": 0, "attentionCount": 0}]
        for bootstrap in invalid:
            catalog = sample()
            catalog["bootstrap"] = bootstrap
            output = build_site(self.root, catalog, [{"bootstrap": bootstrap}])
            published = json.loads((output / "data" / "catalog.json").read_text())
            report = json.loads((output / "data" / "updates.json").read_text())[0]
            self.assertNotIn("bootstrap", published)
            self.assertNotIn("bootstrap", report)

    def test_public_session_projects_only_progress_and_never_private_queue(self):
        catalog, session = sample(), sample_session()
        private = {"cohortDOIs": ["must-not-publish"], "plannedDOIs": ["must-not-publish"],
                   "processedDOIs": ["must-not-publish"], "inFlight": {"raw_response": "must-not-publish"},
                   "callback": "must-not-publish", "api_key": "must-not-publish", "githubCheckpointSaved": True}
        catalog["session"] = {**session, **private}
        catalog["backfillSession"] = {**session, **private}
        original = copy.deepcopy(catalog)
        output = build_site(self.root, catalog, [{"session": {**session, **private}, "backfillSession": private}])
        published = json.loads((output / "data" / "catalog.json").read_text())
        report = json.loads((output / "data" / "updates.json").read_text())[0]
        self.assertEqual(published["session"], session)
        self.assertEqual(report["session"], session)
        self.assertNotIn("must-not-publish", json.dumps(published))
        self.assertNotIn("must-not-publish", json.dumps(report))
        self.assertNotIn("githubCheckpointSaved", report["session"])
        self.assertNotIn("backfillSession", published)
        self.assertEqual(catalog, original)

    def test_public_session_rejects_invalid_counts_statuses_and_false_material_completion(self):
        invalid = [None, {}, {**sample_session(), "status": []}, {**sample_session(), "status": {}},
                   {**sample_session(), "materialsStatus": []}, {**sample_session(), "materialsStatus": "complete"},
                   {**sample_session(), "id": "https://private/?token=must-not-publish"},
                   {**sample_session(), "endReason": "raw-provider-message"},
                   {**sample_session(), "processedCount": 11}, {**sample_session(), "plannedCount": 56},
                   {**sample_session(), "targetCount": 54}, {**sample_session(), "remainingCount": 56},
                   {**sample_session(), "inFlightCount": 56}, {**sample_session(), "version": True},
                   {**sample_session(), "localCheckpointSaved": "must-not-publish"}]
        for field in ("targetCount", "plannedCount", "processedCount", "remainingCount", "batchCount",
                      "checkpointCount", "requests", "aiAttempts", "uncertainRequests", "uncertainAiAttempts",
                      "inFlightCount", "maxBatches", "maxRequests", "maxAiAttempts", "completeCount", "pendingCount", "attentionCount", "removedCount"):
            invalid.extend({**sample_session(), field: value} for value in (True, -1, 1000001, "must-not-publish", float("nan")))
        for value in invalid:
            self.assertNotIn("session", public_catalog({**sample(), "session": value}))
        output = build_site(self.root, sample(), [{"session": invalid[-1]}])
        self.assertNotIn("session", json.loads((output / "data" / "updates.json").read_text())[0])

    def test_session_finished_and_checkpoint_counts_do_not_fake_material_completion(self):
        session = {**sample_session(), "status": "finished", "endReason": "cohort_traversed",
                   "plannedCount": 55, "processedCount": 55, "remainingCount": 0,
                   "inFlightCount": 0, "checkpointCount": 30, "batchCount": 11}
        projected = public_catalog({**sample(), "session": session})["session"]
        self.assertEqual(projected, session)
        self.assertEqual(projected["materialsStatus"], "active")
        self.assertEqual(projected["pendingCount"], 53)
        # Resuming with reduced caps must not conceal historical confirmed use.
        session.update(maxRequests=100, maxAiAttempts=10, maxBatches=2)
        self.assertEqual(public_catalog({**sample(), "session": session})["session"], session)

    def test_build_omits_ready_case_when_reviewed_body_asset_is_invalid_or_missing(self):
        catalog = sample()
        paper = catalog["papers"][0]
        paper["automation"] = {"status": "ready", "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "generated"}}
        paper["repository"]["status"] = "inspected"
        paper["repository"]["commit"] = "a" * 40
        paper["repository"]["scripts"][0].update(sha256="b" * 64, url="https://github.com/author/repo/blob/" + "a" * 40 + "/plot.py")
        paper["repository"]["inputs"] = [{"path": "input.csv", "headerSHA256": "c" * 64, "fields": [{"name": "age"}], "scriptURL": paper["repository"]["scripts"][0]["url"]}]
        paper["ai"]["status"] = "generated"
        image_hash = hashlib.sha256((self.root / "data" / "assets" / "paper-1" / "fig-1.png").read_bytes()).hexdigest()
        paper["article"]["figures"][0]["sha256"] = image_hash
        paper["mappingReview"] = {"approved": True, "figureId": "fig-1", "figureSHA256": image_hash, "reviewer": "private reviewer", "reviewedAt": "2026-10-11T00:00:00Z", "inputSchemaVerified": True, "commit": "a" * 40, "scriptPath": "plot.py", "scriptSHA256": "b" * 64, "inputPath": "input.csv", "inputSHA256": "c" * 64}
        output = build_site(self.root, catalog, [])
        self.assertEqual(json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["automation"]["status"], "ready")
        source = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        original = source.read_bytes()
        source.write_bytes(original + b"tampered local asset")
        build_site(self.root, catalog, [])
        self.assert_fixture_not_published(output)
        self.assertFalse((output / "assets" / "paper-1" / "fig-1.png").exists())
        source.write_bytes(original)
        source.unlink()
        build_site(self.root, catalog, [])
        self.assert_fixture_not_published(output)
        self.assertEqual(catalog["papers"][0]["automation"]["status"], "ready", "No source catalogue mutation.")

    def test_tampered_asset_digest_is_not_published_and_old_output_removed(self):
        catalog = sample()
        source = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        figure = catalog["papers"][0]["article"]["figures"][0]
        figure["sha256"] = expected
        output = build_site(self.root, catalog, [])
        self.assertTrue((output / figure["asset"]).is_file())
        source.write_bytes(source.read_bytes() + b"changed local image content")
        build_site(self.root, catalog, [])
        self.assert_fixture_not_published(output)
        self.assertFalse((output / figure["asset"]).exists())
        self.assertEqual(figure["sha256"], expected, "Source hash must never be replaced by local edited bytes.")

    def test_derivative_asset_uses_derivative_hash_preserving_original_source_hash(self):
        catalog = sample()
        source = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        figure = catalog["papers"][0]["article"]["figures"][0]
        figure.update(sha256="a" * 64, assetSHA256=hashlib.sha256(source.read_bytes()).hexdigest(), modification="TIFF converted to PNG; whole image retained")
        output = build_site(self.root, catalog, [])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
        self.assertEqual(projected["reuseStatus"], "allowed")
        self.assertTrue((output / figure["asset"]).is_file())
        self.assertEqual(projected["sha256"], "a" * 64)
        self.assertNotEqual(projected["assetSHA256"], projected["sha256"])
        figure["assetSHA256"] = ""
        build_site(self.root, catalog, [])
        self.assert_fixture_not_published(output)
        self.assertFalse((output / figure["asset"]).exists())
        self.assertEqual(figure["sha256"], "a" * 64)

    def test_invalid_provided_asset_hash_fails_closed(self):
        for digest in (None, "", "not-a-hash", "a" * 63, "a" * 65, 123):
            catalog = sample()
            catalog["papers"][0]["article"]["figures"][0]["sha256"] = digest
            output = build_site(self.root, catalog, [])
            self.assert_fixture_not_published(output)

    def test_missing_asset_digest_fails_closed_even_for_a_real_png(self):
        catalog = sample()
        catalog["papers"][0]["article"]["figures"][0].pop("sha256")
        self.assert_fixture_not_published(build_site(self.root, catalog, []))

    def test_build_downgrades_ready_with_invalid_current_stage(self):
        for stage, status in (("metadata", "metadata_unavailable"), ("repository", "unavailable"), ("ai", "stale")):
            catalog = sample()
            paper = catalog["papers"][0]
            paper["automation"] = {"status": "ready", "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "generated"}}
            paper["automation"]["stages"][stage] = status
            paper["repository"]["status"] = "inspected"
            paper["ai"]["status"] = "generated"
            output = build_site(self.root, catalog, [])
            projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
            self.assertEqual(projected["automation"]["status"], "partial")

    def test_suggestions_scores_declaration_links_and_report_stages_are_public_not_raw_text(self):
        catalog = sample()
        paper = catalog["papers"][0]
        paper["suggestedAnalysisFunctions"] = ["prediction"]
        paper["suggestedGraphForms"] = [{"id": "heatmap", "label": "热图", "status": "candidate"}]
        paper["selectionScore"] = {"total": 50, "components": {"usability": 25}, "meaning": "选文偏好"}
        paper["article"]["codeURLEvidence"] = [{"url": "https://github.com/author/repo", "sourceURL": "https://example.org/article.xml", "scope": "author_code_availability", "status": "source_declared", "text": "must-not-publish"}]
        report = {"startedAt": "2026-10-11T00:00:00Z", "stageCounts": {"ai": {"missing_key": 1}}, "missingKeys": ["LLM_API_KEY"]}
        output = build_site(self.root, catalog, [report])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
        self.assertEqual(projected["selectionScore"]["total"], 50)
        self.assertEqual(projected["suggestedAnalysisFunctions"], ["prediction"])
        self.assertEqual(projected["analysisFunctions"], ["association"], "Proposal must not alter approved tags.")
        self.assertNotIn("text", projected["article"]["codeURLEvidence"][0])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text())[0], report)

    def test_keeps_newest_ninety_reports_for_prepend_and_append_history(self):
        reports = [{"startedAt": f"2026-10-{day:02d}T00:00:{second:02d}Z", "newCount": 0} for day in range(1,5) for second in range(30)]
        for ordered in (reports, list(reversed(reports))):
            output = build_site(self.root, sample(), ordered)
            public = json.loads((output / "data" / "updates.json").read_text())
            self.assertEqual(len(public), 90)
            self.assertEqual(public[0]["startedAt"], "2026-10-04T00:00:29Z")
            self.assertEqual(public[-1]["startedAt"], "2026-10-02T00:00:00Z")

    def test_rejects_unsafe_assets_symlinks_and_fake_image_types(self):
        for path in ("assets/../secret.png", "assets/%2e%2e/secret.png", "assets//secret.png", "//server/image.png", "https://example.org/image.png", "assets/image.svg"):
            catalog = sample()
            catalog["papers"][0]["article"]["figures"][0]["asset"] = path
            output = build_site(self.root, catalog, [])
            self.assert_fixture_not_published(output)
        file = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        file.write_text("<html>not a PNG</html>")
        output = build_site(self.root, sample(), [])
        self.assert_fixture_not_published(output)

    def test_matching_digest_cannot_admit_a_signature_only_png(self):
        catalog = sample()
        corrupt = b"\x89PNG\r\n\x1a\nfixture-image-header"
        (self.root / "data" / "assets" / "paper-1" / "fig-1.png").write_bytes(corrupt)
        catalog["papers"][0]["article"]["figures"][0]["sha256"] = hashlib.sha256(corrupt).hexdigest()
        self.assert_fixture_not_published(build_site(self.root, catalog, []))

    def test_source_parent_symlink_cannot_admit_an_otherwise_valid_image(self):
        parent = self.root / "data" / "assets" / "paper-1"
        original_parent = parent.with_name("original-paper-1")
        parent.rename(original_parent)
        parent.symlink_to(original_parent, target_is_directory=True)
        self.assert_fixture_not_published(build_site(self.root, sample(), []))
        self.assertEqual((original_parent / "fig-1.png").read_bytes(), RASTER)

    def test_refuses_execution_claims_duplicates_and_nan_before_build(self):
        for mutation in (lambda c: c.update(authorCodeExecuted=True), lambda c: c["papers"].append(copy.deepcopy(c["papers"][0])), lambda c: c["papers"][0].update(runPerformed=True), lambda c: c["papers"][0].update(year=float("nan"))):
            catalog = sample()
            mutation(catalog)
            with self.assertRaises(ValueError):
                build_site(self.root, catalog, [])

    def test_refuses_output_symlinks(self):
        (self.root / "site").symlink_to(self.root / "web", target_is_directory=True)
        with self.assertRaises(ValueError):
            build_site(self.root, sample(), [])

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_syntax_and_security_and_many_to_many_filters(self):
        subprocess.run([NODE, "--check", str(PROJECT / "web" / "app.js")], check=True, capture_output=True)
        test_code = r'''
const assert = require('assert');
const ui = require(process.argv[1]);
for (const url of ['javascript:alert(1)', 'http://example.org', 'https://user:pass@example.org', 'https://example.org/?api_key=private', 'https://example.org:8080']) assert.equal(ui.safeURL(url), '');
assert.equal(ui.safeURL('https://example.org/paper#fig1'), 'https://example.org/paper#fig1');
for (const path of ['assets/../secret.png','assets/%2e%2e/secret.png','assets//x.png','https://example.org/x.png','assets/./x.png']) assert.equal(ui.safeAsset(path), '');
assert.equal(ui.safeAsset('assets/paper-1/fig-1.png'), 'assets/paper-1/fig-1.png');
const p = {graphForms:['scatter','heatmap'],formClassificationStatus:'approved',analysisFunctions:['association','integration'],functionClassificationStatus:'approved'};
assert(ui.matchAxis(p,'forms','scatter') && ui.matchAxis(p,'functions','integration'));
assert(!ui.matchAxis(p,'functions','prediction'));
assert.deepEqual(ui.approvedTags({...p,formClassificationStatus:'pending'},'forms'), []);
assert(ui.matchAxis({...p,formClassificationStatus:'pending'},'forms','__pending'));
assert.equal(ui.citationCount({}),null);
assert.equal(ui.citationCount({citation_count:0}),0);
assert(ui.metricLabel({metrics:{if:'5.0',jcr:'Q1'}}).includes('接口未提供'));
assert(ui.metricLabel({metrics:{if:'5.0',metric_year:'unknown'}}).includes('接口未提供'));
assert.equal(ui.safeFigures({article:{figures:[{reuseStatus:'link_only',asset:'assets/a.png'}]}}).length,0);
assert.equal(ui.safeFigures({article:{figures:[{reuseStatus:'allowed',asset:'assets/../a.png'}]}}).length,0);
assert.equal(ui.statusOf({}), 'seed');
assert.equal(ui.statusOf({automation:{status:'excluded'}}), 'excluded');
const sections=ui.inputSections({files:[{name:'selected.csv'}],automaticInputs:{files:[{name:'unrelated.csv'}]}},[{name:'fallback.csv'}]);
assert.equal(sections.selected[0].name,'selected.csv');
assert.equal(sections.automatic[0].name,'unrelated.csv');
assert(ui.aiStateLabel({ai:{status:'stale',summary_zh:'旧说明'}}).includes('来源已更新'));
assert(ui.aiStateLabel({ai:{status:'generated',review_required:true}}).includes('待核'));
const pending={graphForms:['suggested-but-unverified'],formClassificationStatus:'pending',analysisFunctions:[],functionClassificationStatus:'pending'};
const counts=ui.facetCounts([p,pending],'forms');
assert.equal(counts.total,2);assert.equal(counts.pending,1);assert.equal(counts.counts.get('scatter'),1);
assert(!counts.counts.has('suggested-but-unverified'));
const related=ui.facetCounts([p,pending],'forms','association');
assert.equal(related.total,1);assert.equal(related.pending,0);
assert.equal(ui.figureNumber('Figure 1c'),1);
assert.equal(ui.figureNumber('Fig. 12a'),12);
const unselected={label:'Figure 2',reuseStatus:'allowed',asset:'assets/fig2.png'};
const correct={label:'Figure 1',reuseStatus:'allowed',asset:'assets/fig1.png'};
assert.equal(ui.cardFigure({figure:'Figure 1c',article:{figures:[unselected]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 1c',article:{figures:[unselected,correct]}}),correct);
assert.equal(ui.cardFigure({figure:'Figure 1c',article:{figures:[{...correct,label:'Figure 10'}]}}),null);
assert.equal(ui.figureNumber('Figure 1c（CD4 T-cell子面板）'),1);
assert.equal(ui.cardFigure({figure:'Figure 1c（CD4 T-cell子面板）',article:{figures:[unselected]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 1c（CD4 T-cell子面板）',article:{figures:[unselected,correct]}}),correct);
assert.deepEqual(ui.figureReference('Extended Data Fig. 6a'),{scope:'extended',number:'6',panel:'a'});
assert.deepEqual(ui.figureReference('Supplementary Fig. S6'),{scope:'supplementary',number:'6',panel:''});
const main6={label:'Figure 6',reuseStatus:'allowed',asset:'assets/main6.png'}, extended6={label:'Extended Data Fig. 6',reuseStatus:'allowed',asset:'assets/ext6.png'};
assert.equal(ui.cardFigure({figure:'Extended Data Fig. 6a',article:{figures:[main6]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 6a',article:{figures:[extended6]}}),null);
assert.equal(ui.cardFigure({figure:'Extended Data Fig. 6a',article:{figures:[main6,extended6]}}),extended6);
assert.equal(ui.cardFigure({figure:'Figure one（编号解析失败）',article:{figures:[main6]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 6',article:{figures:[{...main6,reference:{scope:'supplementary',number:'6',panel:''}}]}}),null);
assert(ui.metricLabel({metrics:{if:'5.2',retained_field_asof:{if:'2025-01-01T00:00:00Z'}}}).includes('沿用 2025-01-01'));
assert.equal(ui.viewFromHash('#gallery','papers'),'gallery');
assert.equal(ui.viewFromHash('#updates','gallery'),'updates');
assert.equal(ui.viewFromHash('#main','papers'),'papers');
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)
        source = (PROJECT / "web" / "app.js").read_text()
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("window.openai", source)
        html = (PROJECT / "web" / "index.html").read_text()
        self.assertIn('<div id="content"></div>', html)
        self.assertIn('<span id="result-count" aria-live="polite">', html)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_safe_chinese_diagnostics_and_budget_log(self):
        test_code = r'''
const assert = require('assert');
const ui = require(process.argv[1]);
for (const status of ['budget_exhausted','authentication_error','payment_required','permission_denied','invalid_request','endpoint_not_found','rate_limited','service_unavailable','other_http_error','network_error','invalid_endpoint','missing_key','missing_model','missing_evidence','missing_endpoint','invalid_response','generated','stale','deferred_until_selection']) {
  assert(/[\u4e00-\u9fff]/.test(ui.statusLabel(status)),status);
  assert(!ui.statusLabel(status).includes(status),status);
}
assert.equal(ui.statusLabel('credentials-in-exception'),'状态待核');
assert.equal(ui.statusLabel('toString'),'状态待核');
assert.equal(ui.stageLabel('toString'),'未分类阶段');
assert.equal(ui.stageLabel('ai'),'AI 中文注释');
assert(ui.errorLabel('ai: invalid_response').includes('接口响应未通过校验'));
assert(!ui.errorLabel('Fetch failed with credentials-in-exception').includes('credentials-in-exception'));
assert(!ui.failureLabel({doi:'https://private.example/?secret=value',stage:'credentials-in-exception',error:'credentials-in-exception'}).includes('credentials-in-exception'));
assert(ui.diagnosticLabel({status:'stale',last_attempt_status:'budget_exhausted',error_reason:'ai_budget_exhausted'}).includes('本轮本地 AI 尝试上限'));
assert(ui.diagnosticLabel({status:'matched',last_attempt_status:'rate_limited',http_status:429,error_reason:'http_429'}).includes('HTTP 429'));
assert(ui.diagnosticLabel({status:'payment_required',error_reason:'http_402'}).includes('HTTP 402'));
assert(!ui.diagnosticLabel({status:'budget_exhausted',error_reason:'network_budget_exhausted'}).includes('余额不足'));
assert(!ui.diagnosticLabel({status:'network_error',error_reason:'credentials-in-exception',http_status:'secret'}).includes('credentials-in-exception'));
assert.equal(ui.diagnosticLabel({status:'network_error',error_reason:'toString',http_status:true}),ui.statusLabel('network_error'));
const skipped={requests:145,collectionRequests:145,aiAttempts:0,maxRequests:150,maxAiCalls:5,reservedAiRequests:5,aiRepairCount:2,collectionBudgetExhausted:true,stageCounts:{ai:{budget_exhausted:2}}};
const lines=ui.updateBudgetLines(skipped).join('\n');
assert(lines.includes('145 / 150') && lines.includes('采集 GET：145') && lines.includes('AI HTTP 尝试：0 / 5'));
assert(lines.includes('预留 5') && lines.includes('上限为 145') && lines.includes('预留不代表已经调用或扣费'));
assert(lines.includes('修补成功：2') && lines.includes('未发起付费 AI 请求'));
assert(lines.includes('本轮已匹配正式文献、已检查全文与作者代码'));
const exhaustedAfterAI=ui.updateBudgetLines({...skipped,requests:150,aiAttempts:5}).join('\n');
assert(exhaustedAfterAI.includes('150 / 150') && exhaustedAfterAI.includes('AI HTTP 尝试：5 / 5'));
assert(exhaustedAfterAI.includes('实际使用情况见 AI HTTP 尝试计数'));
assert(!exhaustedAfterAI.includes('仍可用于') && !exhaustedAfterAI.includes('未发起付费 AI 请求'));
assert(!ui.updateBudgetLines({...skipped,aiAttempts:1}).join('\n').includes('未发起付费 AI 请求'));
assert(!ui.updateBudgetLines({...skipped,stageCounts:{ai:{payment_required:2}}}).join('\n').includes('未发起付费 AI 请求'));
assert(!ui.updateBudgetLines({requests:0,aiAttempts:0}).join('\n').includes('预留 5'));
assert(!ui.updateBudgetLines({reservedAiRequests:true,maxRequests:'credentials-in-exception'}).join('\n').includes('credentials-in-exception'));
assert.deepEqual(ui.updateBudgetLines({}),[]);
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_separates_available_cases_candidates_binding_and_runtime_errors(self):
        test_code = r'''
const assert=require('assert'), ui=require(process.argv[1]);
const commit='a'.repeat(40), hash='b'.repeat(64);
const image={id:'fig1',label:'Figure 1',reuseStatus:'allowed',asset:'assets/paper/fig1.png'};
const script={path:'plot.R',sha256:hash,url:'https://github.com/author/repo/blob/'+commit+'/plot.R'};
const paper={id:'case',figure:'Figure 1a',article:{status:'inspected',figures:[image]},repository:{status:'inspected',commit,scripts:[script]},automation:{status:'partial',stages:{metadata:'matched',article:'inspected',repository:'inspected',ai:'generated',metrics:'not_found'}},ai:{status:'generated',summary_zh:'中文学习笔记'}};
const available=ui.availabilityOf(paper);
assert(available.browsable && available.hasImage && available.hasCode && available.hasAi);
assert.equal(available.verification,'unverified');assert.equal(available.health,'ok');
assert.equal(ui.availabilityOf({...paper,automation:{...paper.automation,status:'ready'}}).verification,'verified');
assert(!ui.availabilityOf({...paper,figure:'Figure 2a'}).browsable);
assert(!ui.availabilityOf({...paper,figure:'unparsed selection'}).hasImage);
assert(!ui.availabilityOf({...paper,article:{figures:[{...image,reuseStatus:'link_only'}]}}).browsable);
assert(!ui.availabilityOf({...paper,repository:{...paper.repository,commit:''}}).browsable);
assert(!ui.availabilityOf({...paper,repository:{...paper.repository,scripts:[{...script,url:'https://evil.example/blob/'+commit+'/plot.R'}]}}).hasCode);
assert(!ui.availabilityOf({...paper,repository:{...paper.repository,scripts:[{...script,sha256:''}]}}).hasCode);
assert(!ui.availabilityOf({id:'seed',codeURL:'https://github.com/author/repo',description:'人工候选介绍'}).browsable);
const failed={...paper,ai:{status:'authentication_error'},automation:{...paper.automation,stages:{...paper.automation.stages,ai:'authentication_error'}}};
assert(ui.availabilityOf(failed).browsable);assert.equal(ui.availabilityOf(failed).health,'error');assert.equal(ui.availabilityOf(failed).hasAi,false);
const deferred={...paper,automation:{...paper.automation,stages:{...paper.automation.stages,ai:'budget_exhausted'}}};
assert.equal(ui.availabilityOf(deferred).health,'deferred');
assert.equal(ui.availabilityOf({...paper,automation:{...paper.automation,stages:{...paper.automation.stages,ai:'missing_key'}}}).health,'deferred');
const old={...paper,ai:{status:'stale',summary_zh:'旧笔记',last_attempt_status:'invalid_response'}};
assert.equal(ui.availabilityOf(old).hasAi,false);assert.equal(ui.availabilityOf(old).hasOldAi,true);
assert.equal(ui.availabilityOf(old).health,'ok','Current stage evidence takes precedence over legacy old-error data.');
const candidate={id:'seed',automation:{status:'seed'}};
const excluded={...paper,id:'excluded',automation:{...paper.automation,status:'excluded'}};
assert.equal(ui.matchesCollection(paper,'browsable'),true);assert.equal(ui.matchesCollection(candidate,'browsable'),false);
assert.equal(ui.matchesCollection(candidate,'candidates'),true);assert.equal(ui.matchesCollection(excluded,'browsable'),false);
assert.deepEqual(ui.collectionCounts([paper,candidate,excluded]),{total:3,browsable:1,candidates:1,excluded:1,verified:0,errors:0});
assert.equal(ui.contentBadges({...paper,article:{figures:[{...image,reuseStatus:'link_only'}]}})[0].label,'仅官方链接');
assert.equal(ui.contentBadges(paper,{automaticInputs:{files:[{name:'data.csv',fields:[],observedFieldReferences:[{name:'age'}]}]}})[2].label,'读取候选待核');
assert.equal(ui.contentBadges(paper,{files:[{name:'input.csv',fields:[['age','numeric','真实表头','年龄']]}]})[2].label,'字段 / 结构笔记 · 表头待核');
const headerPaper={...paper,repository:{...paper.repository,inputs:[{status:'header_observed',headerSHA256:hash,fields:[{name:'age',kind:'actual_file_header'}]}]}};
assert.equal(ui.availabilityOf(headerPaper).hasObservedHeaders,true);
assert.equal(ui.availabilityOf({...headerPaper,repository:{...headerPaper.repository,inputs:[{status:'header_observed',fields:[{name:'age',kind:'actual_file_header'}]}]}}).hasObservedHeaders,false);
assert(ui.contentBadges(headerPaper)[2].label.includes('候选表头已观察'));
assert.equal(ui.detailFigures({...paper,figure:'Figure 2'}).selected,null);
assert.equal(ui.detailFigures({...paper,figure:'Figure 2'}).other[0],image);
assert(ui.updateBudgetLines({untouchedRepairCount:3,retryRepairCount:2,cooldownSkippedCount:1,untouchedRemainingCount:40,automaticCompleteCount:4,incompleteCount:1}).join('\n').includes('尚未首次自动核查：40'));
assert.equal(ui.statusLabel('daily_quota_reached'),'今日新收录名额已用完');
assert.equal(ui.statusLabel('paper_batch_limit_reached'),'达到本轮文章处理上限');
assert(ui.reasonLabel('response_truncated').includes('截断'));
assert(ui.reasonLabel('summary_evidence_invalid').includes('证据'));
assert(ui.reasonLabel('metrics_provider_error').includes('业务错误'));
assert.equal(ui.reasonLabel('secret-provider-message'),'');
assert(!ui.diagnosticLabel({status:'invalid_response',error_reason:'summary_reproduction_claim'}).includes('summary_reproduction_claim'));
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)
        html = (PROJECT / "web" / "index.html").read_text()
        self.assertNotIn('id="collection-filter"', html)
        self.assertNotIn('value="candidates"', html)
        self.assertNotIn('value="all"', html)
        self.assertIn('id="form-filter"', html)
        self.assertIn('id="function-filter"', html)
        self.assertNotIn('<option value="partial">', html)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_body_preview_has_separate_gate_from_selected_figure_binding(self):
        test_code = r'''
const assert=require('assert'),ui=require(process.argv[1]);
const main={id:'fig-1',label:'Figure 1',reference:{scope:'main',number:'1'},reuseStatus:'allowed',asset:'assets/paper/main.png',assetSHA256:'a'.repeat(64),sourceURL:'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML'};
const supplement={...main,id:'supp-12',label:'Supplementary Figure 12',reference:{scope:'supplementary',number:'12'},asset:'assets/paper/supp.png'};
const paper={id:'paper',figure:'Supplementary Figure 12',article:{figures:[supplement,main]},displayFigure:{id:'fig-1',label:'Figure 1',reference:{scope:'main',number:'1'}},repository:{status:'not_found'},ai:{status:'invalid_response'},automation:{status:'partial'}};
assert.equal(ui.bodyFigure(paper),main);assert(ui.bodyDisplayEligible(paper));
assert.equal(ui.cardFigure(paper),supplement);assert(!ui.availabilityOf(paper).hasCode);
assert.equal(ui.bodyPreviewNote(paper),'正文预览：Figure 1；原选案例：Supplementary Figure 12。这不是同一选图，不把原脚本、输入或分类自动绑定到此正文预览。');
assert.equal(ui.bodyPreviewNote({...paper,figure:'Figure 1a'}),'');
assert.equal(ui.bodyPreviewNote({...paper,figure:'Figure 01a'}),'');
assert.equal(ui.bodyPreviewNote({...paper,figure:'Figure 1a',article:{figures:[{...main,reference:{scope:'main',number:'01'}}]}}),'');
assert(ui.bodyPreviewNote({...paper,figure:'Figure one'}).includes('尚未核实为同一图'));
assert.equal(ui.bodyFigure({...paper,displayFigure:undefined}),null);
assert.equal(ui.bodyFigure({...paper,displayFigure:{id:'supp-12',reference:{scope:'main',number:'12'}}}),null);
assert.equal(ui.bodyFigure({...paper,displayFigure:{id:'missing',reference:{scope:'main',number:'1'}}}),null);
assert.equal(ui.bodyFigure({...paper,displayFigure:{id:'fig-1',reference:{scope:'supplementary',number:'1'}}}),null);
for(const bad of [{...main,reuseStatus:'link_only'},{...main,asset:'assets/../private.png'},{...main,assetSHA256:'invalid'},{...main,sourceURL:'http://example.org/img.png'},{...main,reference:{scope:'extended',number:'1'}},{...main,label:'Supplementary Figure 1'},{...main,label:'Extended Data Fig. 1'},{...main,label:'Supplementary plate'},{...main,label:'Extended unknown'},{...main,label:'Figure 2'},{...main,reference:{scope:'main',number:'2'}},{...main,reference:{scope:'main',number:['1']}}]){
  assert.equal(ui.bodyFigure({...paper,article:{figures:[bad]}}),null);
}
const onlySI={...paper,id:'si-only',displayFigure:undefined,article:{figures:[supplement]}};
const noImage={...paper,id:'missing',displayFigure:undefined,article:{figures:[]}};
const failed=new Set(['paper']);assert(!ui.bodyDisplayEligible(paper,failed));
assert.deepEqual(ui.bodyCounts([paper,onlySI,noImage]),{published:1,visible:1,failed:0,verified:0});
assert.deepEqual(ui.bodyCounts([paper,onlySI,noImage],failed),{published:1,visible:0,failed:1,verified:0});
const tags={...paper,graphForms:['heatmap'],formClassificationStatus:'approved',analysisFunctions:['integration'],functionClassificationStatus:'approved'};
assert.deepEqual(ui.approvedTags(tags,'forms'),['heatmap']);assert.deepEqual(ui.approvedTags(tags,'functions'),['integration']);
assert.deepEqual(ui.facetCounts([tags,onlySI],'forms','',record=>ui.bodyDisplayEligible(record)).total,1);
const excluded={...paper,id:'excluded',automation:{status:'excluded'}};
assert(!ui.bodyDisplayEligible(excluded));
assert.deepEqual(ui.bodyCounts([paper,excluded,onlySI,noImage]),{published:1,visible:1,failed:0,verified:0});
assert.equal(ui.bodyReference({...main,label:'Supplemental Figure 1'}),null);
assert.equal(ui.bodyReference({...main,label:'Figure 1',reference:{scope:'main',number:'01'}}).number,'1');
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_body_image_failure_hides_gallery_and_index_without_mutating_catalog(self):
        test_code = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this.attributes={};this.className='';this.value='';this.open=false;this._text='';this.isConnected=true;this.classList={add:(...names)=>{this.className=[...new Set(this.className.split(' ').filter(Boolean).concat(names))].join(' ');},remove:(...names)=>{this.className=this.className.split(' ').filter(name=>!names.includes(name)).join(' ');},toggle:(name,forced)=>{const has=this.className.split(' ').includes(name),next=forced===undefined?!has:!!forced;if(next)this.classList.add(name);else this.classList.remove(name);return next;}};}
  set textContent(value){this._text=String(value);this.children=[];} get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  get options(){return this.children.filter(child=>child.tagName==='option');}
  append(...children){for(const child of children){child.parent=this;this.children.push(child);}}
  replaceChildren(...children){this.children=[];this._text='';this.append(...children);}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  set src(value){this._src=value;if(this.onSrcAssigned)this.onSrcAssigned(value);}get src(){return this._src;}
  dispatch(name,event={}){for(const fn of this.listeners[name]||[])fn({...event,target:event.target||this});}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);this.isConnected=false;}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  showModal(){this.open=true;}close(){this.open=false;this.dispatch('close');}focus(){}
}
const descendants=node=>[node,...node.children.flatMap(descendants)];
async function check(startInIndex,loadFailsImmediately=false,viewerFromGallery=false){
  const ids=new Map(),created=[],nav=['gallery','papers','updates'].map(view=>{const node=new Element('button');node.dataset.view=view;return node;}),tabs=['inputs','explanation','source','notes'].map(tab=>{const node=new Element('button');node.dataset.tab=tab;return node;});
  const document={activeElement:null,createElement:tag=>{const node=new Element(tag);created.push(node);if(tag==='img'&&loadFailsImmediately)node.onSrcAssigned=value=>{if(value.endsWith('/main.png'))node.dispatch('error');};return node;},getElementById:id=>{if(!ids.has(id))ids.set(id,new Element(['detail','image-viewer'].includes(id)?'dialog':'div'));return ids.get(id);},querySelectorAll:selector=>selector==='[data-view]'?nav:selector==='[data-tab]'?tabs:[]};
  const main={id:'fig-1',label:'Figure 1',reference:{scope:'main',number:'1'},reuseStatus:'allowed',asset:'assets/paper/main.png',assetSHA256:'a'.repeat(64),sourceURL:'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML'};
  const supplement={...main,id:'supp-12',label:'Supplementary Figure 12',reference:{scope:'supplementary',number:'12'},asset:'assets/paper/supp.png'};
  const paper={id:'paper',paper:'Body article',figure:'Supplementary Figure 12',displayFigure:{id:'fig-1',label:'Figure 1',reference:{scope:'main',number:'1'}},article:{figures:[main,supplement]},automation:{status:'partial'}};
  const catalog={authorCodeExecuted:false,papers:[paper,{id:'no-body',paper:'Not shown',article:{figures:[]}},{...paper,id:'excluded',paper:'Excluded article',automation:{status:'excluded'}}],inputSpecs:{},dimensions:{},generatedAt:'2026-10-11T12:00:00Z',bootstrap:{status:'active',totalCount:55,completeCount:2,pendingCount:53,attentionCount:0,issues:[]}},before=JSON.stringify(catalog);
  const updates=[{startedAt:'2026-10-11T12:00:00Z',status:'partial',newCount:0}];
  const sandbox={document,URL,console,location:{hash:startInIndex?'#papers':''},history:{replaceState(){}},window:{addEventListener(){}},fetch:url=>Promise.resolve({ok:true,json:()=>Promise.resolve(url.includes('catalog')?catalog:updates)}),module:{exports:{}}};
  vm.runInNewContext(source,sandbox);await new Promise(setImmediate);await new Promise(setImmediate);
  assert(document.getElementById('result-count').textContent.includes(loadFailsImmediately?'0 / 0':'1 / 1'));
  assert(!document.getElementById('content').textContent.includes('Not shown'));
  assert(!document.getElementById('content').textContent.includes('Excluded article'));
  const fold=document.getElementById('collection-phase').children[0];assert.equal(fold.tagName,'details');assert(!fold.open);
  const image=startInIndex||loadFailsImmediately?created.find(node=>node.tagName==='img' && node.src===main.asset):descendants(document.getElementById('content')).find(node=>node.tagName==='img');
  assert(image);assert.equal(image.src,main.asset);
  if(!loadFailsImmediately){
    if(viewerFromGallery){
      const zoom=descendants(document.getElementById('content')).find(node=>node.className==='figure-zoom');assert(zoom);zoom.dispatch('click');
      assert(document.getElementById('image-viewer').open);assert(!document.getElementById('detail').open);
      document.getElementById('image-viewer').dispatch('close'); // Late close event from a previous dialog instance must not forget the open viewer.
      const viewerImage=document.getElementById('image-viewer-stage').children[0];assert.equal(viewerImage.src,main.asset);viewerImage.dispatch('error');
    }else{
      const open=descendants(document.getElementById('content')).find(node=>node.tagName==='button'&&node.textContent.startsWith('查看输入'));
      assert(open);open.dispatch('click');
      const zooms=descendants(document.getElementById('detail-figure')).filter(node=>node.className==='figure-zoom');assert.equal(zooms.length,2);
      zooms[1].dispatch('click');const oldImage=document.getElementById('image-viewer-stage').children[0];assert.equal(oldImage.src,supplement.asset);
      zooms[0].dispatch('click');const currentImage=document.getElementById('image-viewer-stage').children[0];assert.equal(currentImage.src,main.asset);
      oldImage.dispatch('error');assert.equal(document.getElementById('image-viewer-stage').children[0],currentImage);
      image.dispatch('error');
    }
    await new Promise(setImmediate);
    assert(!document.getElementById('detail').open);assert(!document.getElementById('image-viewer').open);
  }
  assert(document.getElementById('notice').textContent.includes('临时隐藏'));
  assert(document.getElementById('notice').textContent.includes('刷新页面可重新尝试'));
  assert(!descendants(document.getElementById('content')).some(node=>node.className==='case-card' || node.className==='paper-row' || node.className==='figure-placeholder'));
  nav.find(node=>node.dataset.view===(startInIndex?'gallery':'papers')).dispatch('click');
  assert(document.getElementById('result-count').textContent.includes('0 / 0'));
  assert(!document.getElementById('content').textContent.includes('Body article'));
  nav.find(node=>node.dataset.view==='updates').dispatch('click');assert.equal(document.getElementById('result-count').textContent,'1 次运行记录');
  assert.equal(JSON.stringify(catalog),before);assert.equal(updates.length,1);
}
(async()=>{await check(false);await check(true);await check(false,true);await check(true,true);await check(false,false,true);})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_bootstrap_phase_progress_is_safe_and_not_false_completion(self):
        from paper_plot_library.bootstrap import BOOTSTRAP_REASON_CODES
        test_code = r'''
const assert=require('assert'),ui=require(process.argv[1]);
const active={status:'active',totalCount:50,completeCount:4,pendingCount:44,attentionCount:2,startedAt:'2026-10-11T12:00:00Z',issues:[{doi:'10.1000/test',reasonCodes:['figure_reuse_blocked','input_undocumented','secret-provider-message'],raw_response:'secret-provider-message'}]};
const safe=ui.bootstrapSummary(active);
assert.equal(safe.completeCount,4);assert.equal(safe.pendingCount,44);assert.equal(safe.attentionCount,2);
assert.deepEqual(safe.issues[0].reasonCodes,['figure_reuse_blocked','input_undocumented']);
assert(!JSON.stringify(safe).includes('secret-provider-message'));
const lines=ui.bootstrapLines(safe).join('\n');assert(lines.includes('暂停新文献发现') && lines.includes('4 / 50') && lines.includes('需要检查 2'));
assert(lines.includes('许可受限或缺少输入不会自动改成完成'));
assert(ui.bootstrapIssueLabel(safe.issues[0]).includes('原图许可'));
assert(!ui.bootstrapIssueLabel({doi:'https://private/?secret=value',reasonCodes:['figure_reuse_blocked']}).includes('private'));
assert.equal(ui.bootstrapSummary({...active,status:'complete'}),null);
assert.equal(ui.bootstrapSummary({...active,totalCount:51}),null);
assert.equal(ui.bootstrapSummary({...active,totalCount:true}),null);
const complete={status:'complete',totalCount:50,completeCount:50,pendingCount:0,attentionCount:0,issues:[]};
assert(ui.bootstrapLines(complete).join('\n').includes('下次运行进入日更'));
assert(ui.bootstrapLines(complete).join('\n').includes('不代表人工精确对应已核'));
assert(ui.bootstrapLines(complete,'daily')[0].includes('每日自动发现'));
assert.equal(ui.bootstrapSummary({...complete,issues:[{doi:'10.1000/test',reasonCodes:['figure_reuse_blocked']}]}),null);
assert.equal(ui.latestBootstrap({papers:[]},[]),null);
assert.equal(ui.latestBootstrap({bootstrap:active},[]).summary.status,'active');
assert.equal(ui.latestBootstrap({},[{startedAt:'2026-10-11T12:00:00Z',mode:'backfill',bootstrap:active},{startedAt:'2026-10-11T13:00:00Z',mode:'daily',bootstrap:complete}]).summary.status,'complete');
assert(ui.statusLabel('bootstrap_in_progress').includes('暂停'));
assert(ui.statusLabel('bootstrap_needs_attention').includes('需要处理'));
assert(ui.statusLabel('bootstrap_complete_next_run').includes('下次'));
assert.deepEqual(Object.keys(ui.BOOTSTRAP_REASONS).sort(),JSON.parse(process.argv[2]).sort());
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js"), json.dumps(sorted(BOOTSTRAP_REASON_CODES))], check=True, capture_output=True)
        self.assertIn('id="collection-phase"', (PROJECT / "web" / "index.html").read_text())

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_session_progress_and_safe_projection_match_controlled_contract(self):
        from paper_plot_library.session import SESSION_COUNTERS, SESSION_END_REASONS, SESSION_STATUSES
        session = sample_session()
        test_code = r'''
const assert=require('assert'),ui=require(process.argv[1]),value=JSON.parse(process.argv[2]);
const safe=ui.sessionSummary({...value,cohortDOIs:['must-not-publish'],inFlight:{raw_response:'must-not-publish'},githubCheckpointSaved:true,callback:'must-not-publish'});
assert.deepEqual(safe,value);assert(!JSON.stringify(safe).includes('must-not-publish'));assert(!Object.hasOwn(safe,'githubCheckpointSaved'));
const lines=ui.sessionLines(safe).join('\n');
assert(lines.includes('8 / 55') && lines.includes('已结束批次 2 / 12'));
assert(lines.includes('本地 JSON 检查点：已写入') && lines.includes('不能由本地写入推定'));
assert(lines.includes('本会话确认的实际 HTTP：190 / 1800') && lines.includes('AI HTTP 尝试：17 / 165'));
assert(lines.includes('保守占用 HTTP 150') && lines.includes('不是已确认实际请求或扣费次数'));
assert(lines.includes('达到本次会话时间限制') && lines.includes('不代表原图、许可、代码、输入已齐全'));
assert(lines.includes('完成 2 / 55') && lines.includes('待处理 53'));
assert(lines.includes('不是正在运行会话的实时监控'));
const finished={...value,status:'finished',endReason:'cohort_traversed',plannedCount:55,processedCount:55,remainingCount:0,inFlightCount:0};
assert(ui.sessionLines(finished)[0].includes('不等于材料已全部收齐'));assert.equal(ui.sessionSummary(finished).pendingCount,53);
assert(ui.sessionLines({...value,localCheckpointSaved:false}).join('\n').includes('尚未确认写入'));
assert.equal(ui.sessionSummary({...value,materialsStatus:'complete'}),null);
assert.equal(ui.sessionSummary({...value,materialsStatus:[]}),null);
assert.equal(ui.sessionSummary({...value,status:[]}),null);
assert.equal(ui.sessionSummary({...value,endReason:'must-not-publish'}),null);
assert.equal(ui.sessionSummary({...value,processedCount:11}),null);
assert.equal(ui.sessionSummary({...value,plannedCount:56}),null);
assert.equal(ui.sessionSummary({...value,targetCount:54}),null);
assert.equal(ui.sessionSummary({...value,checkpointCount:true}),null);
assert.equal(ui.sessionSummary({...value,id:'https://private/?token=must-not-publish'}),null);
assert.equal(ui.sessionSummary({...value,localCheckpointSaved:'yes'}),null);
assert.equal(ui.sessionSummary({...value,version:true}),null);
assert(ui.sessionSummary({...value,maxRequests:100,maxAiAttempts:10,maxBatches:1}));
assert.equal(ui.latestSession({},[]),null);
assert.deepEqual(ui.latestSession({session:value},[]),value);
const newer={...value,updatedAt:'2026-10-11T13:00:00Z',processedCount:9};
assert.deepEqual(ui.latestSession({session:value},[{session:newer}]),newer);
assert.deepEqual(ui.SESSION_COUNTERS.slice().sort(),JSON.parse(process.argv[3]).sort());
assert.deepEqual(Object.keys(ui.SESSION_END_REASONS).sort(),JSON.parse(process.argv[4]).sort());
assert.deepEqual(ui.SESSION_STATUSES.slice().sort(),JSON.parse(process.argv[5]).sort());
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js"), json.dumps(session),
                        json.dumps(sorted(SESSION_COUNTERS)), json.dumps(sorted(SESSION_END_REASONS)),
                        json.dumps(sorted(SESSION_STATUSES))], check=True, capture_output=True)
        self.assertIn('.session-progress progress', (PROJECT / "web" / "styles.css").read_text())

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_paid_retry_budget_and_fatal_configuration_circuit_are_explicit(self):
        test_code = r'''
const assert=require('assert'),ui=require(process.argv[1]);
const report={requests:135,collectionRequests:126,aiAttempts:9,maxRequests:150,maxAiCalls:15,reservedAiRequests:15,paperBatchLimit:5,maxAiAttemptsPerPaper:3,checkedCount:5,aiCasesAttempted:4,aiRetriedCaseCount:2,aiRetrySuccessCount:1};
const lines=ui.updateBudgetLines(report).join('\n');
assert(lines.includes('AI HTTP 尝试：9 / 15'));
assert(lines.includes('预留 15') && lines.includes('上限为 135'));
assert(lines.includes('论文处理上限：5 篇') && lines.includes('实际核查：5 条'));
assert(lines.includes('每篇 AI 最多 3 次') && lines.includes('传输重试、响应校验重试都计数'));
assert(lines.includes('实际发起 AI HTTP 的论文：4 篇') && lines.includes('至少 2 次 HTTP 尝试：2 篇') && lines.includes('重试后注释生成成功：1 篇'));
assert(lines.includes('AI 成功不等于原图、代码、输入材料已齐全'));
assert(lines.includes('不是每日额度') && lines.includes('重新运行可能再次调用并增加费用'));
for(const status of ['missing_key','missing_model','missing_endpoint','invalid_endpoint','authentication_error','payment_required','permission_denied','invalid_request','endpoint_not_found']){
  const fatal=ui.updateBudgetLines({...report,aiStoppedStatus:status}).join('\n');
  assert(fatal.includes('本轮已停止余下论文的付费 AI 队列'),status);assert(fatal.includes('修正后再运行 backfill'),status);
}
for(const status of ['network_error','budget_exhausted','secret-provider-response',{},[]]){
  const safe=ui.updateBudgetLines({...report,aiStoppedStatus:status}).join('\n');
  assert(!safe.includes('本轮已停止余下论文') && !safe.includes('secret-provider-response'));
}
assert.deepEqual(ui.updateBudgetLines({paperBatchLimit:true,maxAiAttemptsPerPaper:'secret-provider-response',aiCasesAttempted:-1,aiRetriedCaseCount:NaN,aiRetrySuccessCount:1000001}),[]);
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)

    def test_large_figure_responsive_layout_rules(self):
        css = (PROJECT / "web" / "styles.css").read_text()
        self.assertIn("max-width:1660px", css)
        self.assertIn("repeat(2,minmax(360px,1fr))", css)
        self.assertNotIn("repeat(3,", css)
        self.assertNotIn("repeat(4,", css)
        self.assertIn("@media(max-width:820px){.gallery{grid-template-columns:1fr}", css)
        self.assertIn("width:min(1500px,92vw)", css)
        self.assertIn(".figure-preview.has-image{aspect-ratio:auto", css)
        self.assertIn(".figure-zoom{position:static", css)
        self.assertIn(".figure-actions .image-status{position:static", css)
        self.assertIn(".full-image-stage.native-size img{max-width:none;max-height:none", css)
        # Check every declaration line, not just overall balance: two malformed
        # media rules can have mutually cancelling parenthesis errors.
        for line in css.splitlines():
            self.assertEqual(line.count("("), line.count(")"), line)
        self.assertEqual(css.count("{"), css.count("}"))


if __name__ == "__main__":
    unittest.main()
