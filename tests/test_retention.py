"""Transactional maintenance acceptance, using only synthetic records/assets."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import complete_summary, inspect_record, persistent_catalog, _can_summarize
from paper_plot_library.pruning import prune_uncollectable
from paper_plot_library.retention import admission_is_valid, protected_library_record
from paper_plot_library.scope import POLICY, assess_scope
from paper_plot_library.site import has_publishable_body_figure, public_catalog
import test_bootstrap as bootstrap_fixtures
from test_bootstrap import NOW, RASTER
from test_pipeline_audit import Client, adapters


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-retention-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.settings = Settings(repair_limit=0, citation_enrichment=False)

    def case(self):
        result = bootstrap_fixtures.BootstrapTests.full_case(self)
        result["article"]["figures"][0]["sourceURL"] = "https://example.org/fixture.png"
        return result

    def inspect(self, case, **changes):
        active = self.successful_adapters(case)
        active.update(changes)
        return inspect_record(Client(), case, self.root, self.settings, active)

    def successful_adapters(self, case, **changes):
        return adapters(article=lambda *args: deepcopy(case["article"]),
                        repository=lambda *args: deepcopy(case["repository"]), **changes)

    def test_success_and_failure_do_not_mutate_nested_original(self):
        case, snapshot = self.case(), deepcopy(self.case())
        def bad_article(client, current, *args):
            current["article"]["figures"].clear()
            current["repository"]["scripts"].clear()
            raise RuntimeError("synthetic")
        with self.assertRaises(RuntimeError):
            self.inspect(case, article=bad_article)
        self.assertEqual(case, snapshot)
        result = inspect_record(Client(), case, self.root, self.settings, self.successful_adapters(case))
        result["repository"]["scripts"].clear()
        self.assertEqual(case, snapshot)

    def test_mutating_terminal_adapter_cannot_erase_original_or_retained_copy(self):
        case = self.case()
        def missing_repo(client, current):
            current["repository"]["scripts"].clear()
            return {"status": "no_scripts_found", "scripts": [], "inputs": []}
        result = self.inspect(case, repository=missing_repo)
        self.assertTrue(case["repository"]["scripts"])
        self.assertTrue(result["repository"]["scripts"])

    def test_terminal_source_gaps_retain_last_good_without_new_ai(self):
        for stage, status in (("article", "not_found"), ("article", "no_final_version"),
                              ("article", "endpoint_not_found"), ("repository", "not_found"),
                              ("repository", "no_scripts_found"), ("repository", "endpoint_not_found")):
            with self.subTest(stage=stage, status=status):
                case = self.case()
                active = self.successful_adapters(case,
                    summary=lambda *args: self.fail("Retained sources must not incur AI"))
                active[stage] = lambda *args: {"status": status, "figures": [], "scripts": [], "inputs": []}
                result = inspect_record(Client(), case, self.root, self.settings, active)
                self.assertTrue(has_publishable_body_figure(self.root, result))
                self.assertTrue(result[stage]["retained_from_previous"])
                self.assertEqual(result[stage]["last_attempt_status"], status)
                self.assertEqual(result["automation"]["stages"][stage], status)
                self.assertEqual(result["ai"]["status"], "stale")
                self.assertFalse(_can_summarize(result))

    def test_metadata_failure_preserves_old_body_and_summary_without_fresh_claim(self):
        case = self.case()
        result = self.inspect(case, lookup=lambda *args: {"status": "metadata_unavailable"},
                              summary=lambda *args: self.fail("Unexpected AI"))
        self.assertTrue(has_publishable_body_figure(self.root, result))
        self.assertEqual(result["automation"]["stages"], {"metadata": "metadata_unavailable"})
        self.assertEqual(result["ai"]["status"], "stale")
        self.assertFalse(_can_summarize(result))

    def test_missing_scope_abstract_does_not_erase_previous_verified_decision(self):
        case = self.case()
        case["paper"] = "Single-cell RNA-seq aging clock"
        case["researchScope"] = assess_scope(case, POLICY, source="verified-metadata")
        decision = deepcopy(case["researchScope"])
        settings = Settings(research_scope=POLICY)
        active = adapters(lookup=lambda *args: {"status": "matched", "doi": case["doi"],
                    "paper": "Shortened metadata title"}, article=lambda *args: self.fail("No source refresh"),
                    summary=lambda *args: self.fail("No AI"))
        result = inspect_record(Client(), case, self.root, settings, active)
        self.assertEqual(result["paper"], case["paper"])
        self.assertEqual(result["researchScope"], decision)
        self.assertEqual(result["automation"]["stages"]["researchScope"], "metadata_unavailable")
        self.assertTrue(has_publishable_body_figure(self.root, result))

    def test_empty_new_inspected_figures_or_input_headers_keep_old_evidence_stale(self):
        for stage in ("article", "repository"):
            with self.subTest(stage=stage):
                case = self.case()
                new_value = deepcopy(case[stage])
                new_value["figures" if stage == "article" else "inputs"] = []
                active = self.successful_adapters(case, summary=lambda *args: self.fail("No stale AI"))
                active[stage] = lambda *args: deepcopy(new_value)
                result = inspect_record(Client(), case, self.root, self.settings, active)
                self.assertTrue(result[stage]["retained_from_previous"])
                self.assertEqual(result["automation"]["stages"][stage], "inspected")
                self.assertFalse(_can_summarize(result))
                self.assertTrue(has_publishable_body_figure(self.root, result))

    def test_code_commit_and_headers_are_retained_atomically_not_mixed(self):
        case = self.case()
        new_repo = deepcopy(case["repository"])
        new_repo.update(commit="f" * 40, inputs=[])
        result = self.inspect(case, repository=lambda *args: new_repo)
        self.assertEqual(result["repository"]["commit"], case["repository"]["commit"])
        self.assertEqual(result["repository"]["inputs"], case["repository"]["inputs"])
        self.assertTrue(result["repository"]["retained_from_previous"])

    def test_ai_and_metrics_failure_keep_last_good_values_and_real_attempt(self):
        for status in ("network_error", "authentication_error", "invalid_response", "budget_exhausted"):
            with self.subTest(status=status):
                case = self.case()
                active = self.successful_adapters(case, summary=lambda *args: {"status": status},
                    metrics=lambda *args: {"status": status})
                result = inspect_record(Client(), case, self.root, self.settings, active)
                self.assertEqual(result["ai"]["summary_zh"], case["ai"]["summary_zh"])
                self.assertEqual(result["ai"]["status"], "stale")
                self.assertEqual(result["metrics"]["if"], case["metrics"]["if"])
                self.assertEqual(result["metrics"]["last_attempt_status"], status)
                self.assertEqual(result["automation"]["stages"]["ai"], status)
                self.assertNotEqual(result["automation"]["status"], "ready")

    def test_complete_summary_retains_good_output_on_retry_failure(self):
        case = self.case()
        result = complete_summary(Client(), case, self.settings,
                                  {"summary": lambda *args: {"status": "invalid_response"}})
        self.assertEqual(result["ai"]["status"], "stale")
        self.assertEqual(result["automation"]["stages"]["ai"], "invalid_response")

    def test_empty_success_enrichment_is_not_reported_as_current_success(self):
        case = self.case()
        active = self.successful_adapters(case, summary=lambda *args: {"status": "generated"},
                                         metrics=lambda *args: {"status": "matched"})
        result = inspect_record(Client(), case, self.root, self.settings, active)
        self.assertEqual(result["automation"]["stages"]["ai"], "invalid_response")
        self.assertEqual(result["automation"]["stages"]["metrics"], "invalid_response")
        self.assertEqual(result["ai"]["summary_zh"], case["ai"]["summary_zh"])
        self.assertEqual(result["metrics"]["if"], case["metrics"]["if"])

    def test_explicit_open_figure_license_remains_valid_under_restricted_article(self):
        case = self.case()
        new_article = deepcopy(case["article"])
        new_article["license"] = "CC BY-NC-SA 4.0"
        new_article["figures"][0]["license"] = "CC BY 4.0"
        active = self.successful_adapters(case)
        active["article"] = lambda *args: new_article
        result = inspect_record(Client(), case, self.root, self.settings, active)
        self.assertNotEqual(result["automation"]["status"], "excluded")
        self.assertTrue(has_publishable_body_figure(self.root, result))

    def test_malicious_summary_exception_cannot_mutate_existing_output(self):
        case, snapshot = self.case(), deepcopy(self.case())
        def broken(client, current, *args):
            current["ai"].clear()
            current["article"]["figures"].clear()
            raise RuntimeError("synthetic")
        with self.assertRaises(RuntimeError):
            complete_summary(Client(), case, self.settings, {"summary": broken})
        self.assertEqual(case, snapshot)

    def test_retraction_and_doi_mismatch_quarantine_without_public_old_images(self):
        for metadata in ({"status": "matched", "doi": "10.1234/wrong"},
                         {"status": "matched", "isRetracted": True}, {"status": "excluded"}):
            with self.subTest(metadata=metadata):
                case = self.case()
                result = self.inspect(case, lookup=lambda *args: metadata,
                    article=lambda *args: self.fail("Quarantine before source calls"))
                self.assertEqual(result["automation"]["status"], "excluded")
                self.assertFalse(has_publishable_body_figure(self.root, result))
                self.assertEqual(result["article"]["figures"], [])
                self.assertEqual(result["retentionArchive"]["doi"], case["doi"])
                self.assertTrue(result["retentionArchive"]["article"]["figures"])
                public = public_catalog({"authorCodeExecuted": False, "papers": [result], "inputSpecs": {}})
                self.assertNotIn("retentionArchive", public["papers"][0])

    def test_article_safety_or_known_license_change_is_not_fallback(self):
        for kind in ("retracted", "doi_mismatch", "excluded", "license", "figure_license", "digest"):
            with self.subTest(kind=kind):
                case = self.case()
                new_article = deepcopy(case["article"])
                if kind == "license":
                    new_article["license"] = "CC BY-NC 4.0"
                elif kind == "figure_license":
                    new_article["figures"][0]["license"] = "CC BY-ND 4.0"
                elif kind == "digest":
                    new_article["figures"][0]["sha256"] = "0" * 64
                else:
                    new_article["status"] = kind
                result = self.inspect(case, article=lambda *args: new_article,
                                      summary=lambda *args: self.fail("No unsafe AI"))
                self.assertEqual(result["automation"]["status"], "excluded")
                self.assertFalse(has_publishable_body_figure(self.root, result))
                self.assertTrue(result["retentionArchive"]["article"]["figures"])

    def test_old_corrupt_asset_is_never_made_publishable_by_retention(self):
        case = self.case()
        (self.root / "data" / "assets" / "fixture.png").write_bytes(b"corrupt")
        result = self.inspect(case, article=lambda *args: {"status": "not_found"})
        self.assertFalse(has_publishable_body_figure(self.root, result))

    def test_archive_sanitizes_source_excerpts_and_unlicensed_caption(self):
        case = self.case()
        result = self.inspect(case, lookup=lambda *args: {"status": "matched", "isRetracted": True})
        archive = persistent_catalog({"papers": [result]})["papers"][0]["retentionArchive"]
        self.assertNotIn("caption", archive["article"]["figures"][0])
        self.assertNotIn("text", archive["repository"]["scripts"][0]["evidence"][0])
        original = deepcopy(archive)
        next_result = self.inspect(result, lookup=lambda *args: {"status": "matched", "isRetracted": True})
        self.assertEqual(next_result["retentionArchive"], original)

    def test_persistent_catalog_projects_preexisting_nested_archive_before_public_git(self):
        case = self.case()
        result = self.inspect(case, lookup=lambda *args: {"status": "matched", "isRetracted": True})
        archive = result["retentionArchive"]
        private = {"abstract": "ARCHIVE_RAW_SENTINEL", "text": "ARCHIVE_RAW_SENTINEL",
                   "caption": "ARCHIVE_RAW_SENTINEL", "rawXML": "ARCHIVE_RAW_SENTINEL",
                   "privateCache": {"text": "ARCHIVE_RAW_SENTINEL"}}
        archive.update(deepcopy(private))
        archive["article"].update(deepcopy(private))
        archive["article"]["figures"][0].update(deepcopy(private))
        archive["article"]["supplements"] = [{"label": "Supplement 1", **deepcopy(private)}]
        archive["article"]["codeURLEvidence"][0].update(deepcopy(private))
        archive["repository"].update(deepcopy(private))
        archive["repository"]["scripts"][0].update(deepcopy(private))
        archive["repository"]["scripts"][0]["evidence"][0].update(deepcopy(private))
        archive["repository"]["inputs"][0].update(deepcopy(private))
        archive["repository"]["inputs"][0]["fields"][0].update(deepcopy(private))
        archive["ai"].update(deepcopy(private))
        archive["metrics"].update(deepcopy(private))
        archive["mappingReview"] = {"approved": False, **deepcopy(private)}
        original = deepcopy(result)
        saved = persistent_catalog({"papers": [result]})["papers"][0]["retentionArchive"]
        self.assertNotIn("ARCHIVE_RAW_SENTINEL", json.dumps(saved))
        self.assertEqual(saved["repository"]["scripts"][0]["sha256"], case["repository"]["scripts"][0]["sha256"])
        self.assertEqual(saved["repository"]["inputs"][0]["fields"][0]["name"], "age")
        self.assertEqual(saved["article"]["figures"][0]["asset"], "assets/fixture.png")
        self.assertEqual(result, original, "Persistence projection must not mutate the in-memory source")

    def test_bad_archive_binding_shape_or_future_date_is_not_committed(self):
        case = self.case()
        result = self.inspect(case, lookup=lambda *args: {"status": "matched", "isRetracted": True})
        for key, bad in (("doi", "10.1234/other"), ("archivedAt", "2099-01-01T00:00:00Z"),
                         ("version", True), ("reason", "ARCHIVE_RAW_SENTINEL")):
            with self.subTest(key=key):
                changed = deepcopy(result)
                changed["retentionArchive"][key] = bad
                self.assertNotIn("retentionArchive", persistent_catalog({"papers": [changed]})["papers"][0])

    def test_valid_admission_is_deletion_protection_not_permission_bypass(self):
        case = self.case()
        case["admittedAt"] = "2026-10-10T12:00:00Z"
        self.assertTrue(admission_is_valid(case, NOW))
        case["article"]["figures"][0]["reuseStatus"] = "link_only"
        self.assertTrue(protected_library_record(case, self.root, NOW))
        self.assertFalse(has_publishable_body_figure(self.root, case))
        for invalid in (True, "2026-10-10", "tomorrow", "2026-10-99T12:00:00Z", "2099-01-01T00:00:00Z"):
            case["admittedAt"] = invalid
            self.assertFalse(admission_is_valid(case, NOW))
        case["admittedAt"] = "2026-10-10T12:00:00Z"
        case["doi"] = "NOT_A_DOI"
        self.assertFalse(admission_is_valid(case, NOW))

    def test_admitted_and_legacy_real_body_records_survive_repeated_code_input_gaps(self):
        for admission in (False, True):
            with self.subTest(admission=admission):
                case = self.case()
                if admission:
                    case["admittedAt"] = "2026-10-10T12:00:00Z"
                case["repository"]["inputs"] = []
                case["automation"]["bootstrap"] = {"unsuccessfulScans": 10, "lastAttemptAt": NOW,
                    "sourceGapScans": {"input_basis_unavailable": 10}}
                catalog = {"papers": [case], "inputSpecs": {case["id"]: {"status": "pending"}}}
                result = prune_uncollectable(catalog, {"days": {}, "candidates": {}}, self.root, NOW)
                self.assertEqual(result["removedCount"], 0)
                self.assertEqual(catalog["papers"], [case])
                self.assertIn(case["id"], catalog["inputSpecs"])


if __name__ == "__main__":
    unittest.main()
