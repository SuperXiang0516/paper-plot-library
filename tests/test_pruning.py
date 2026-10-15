"""User-authorized source pruning; synthetic state/assets and no real transport."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from paper_plot_library.bootstrap import (bootstrap_report, ensure_bootstrap,
    note_bootstrap_attempt, safe_removed_dois, _safe_record_state)
from paper_plot_library.pruning import prune_uncollectable, uncollectable_reasons
import test_bootstrap as bootstrap_fixtures
from test_bootstrap import NOW, RASTER
from test_pipeline_audit import record


class PruningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-prune-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.ledger = {"days": {}, "candidates": {}}

    def full_case(self, index=1):
        result = bootstrap_fixtures.BootstrapTests.full_case(self, index)
        result["article"]["figures"][0]["sourceURL"] = "https://example.org/fixture.png"
        return result

    def catalog(self, papers):
        return {"authorCodeExecuted": False, "papers": papers,
                "inputSpecs": {paper["id"]: {"privateFixture": "DO_NOT_RETAIN"} for paper in papers}}

    def scans(self, case, count=3, spec=None):
        for _ in range(count):
            note_bootstrap_attempt(case, spec, NOW, self.root)

    def test_three_clean_same_gap_scans_remove_record_and_input_not_asset(self):
        case = self.full_case()
        case["repository"]["inputs"] = []
        # This is an unadmitted failed candidate, not a previously displayable
        # legacy library record. Real-image records are protected below.
        case["article"]["figures"][0]["sha256"] = "0" * 64
        catalog = self.catalog([case, self.full_case(2)])
        ensure_bootstrap(catalog, self.ledger, NOW)
        self.ledger["candidates"][case["doi"]] = {"title": "DO_NOT_RETAIN"}
        self.scans(case, 2)
        self.assertEqual(prune_uncollectable(catalog, self.ledger, self.root, NOW)["removedCount"], 0)
        self.scans(case, 1)
        result = prune_uncollectable(catalog, self.ledger, self.root, NOW)
        self.assertEqual(result, {"deletedDOIs": [case["doi"]], "reasons": {case["doi"]: "input_basis_unavailable"}, "removedCount": 1})
        self.assertEqual([item["id"] for item in catalog["papers"]], [record(2)["id"]])
        self.assertNotIn(case["id"], catalog["inputSpecs"])
        self.assertNotIn(case["doi"], self.ledger["candidates"])
        self.assertEqual(set(self.ledger["removedPapers"][case["doi"]]), {"reasonCode", "removedAt"})
        self.assertEqual((self.root / "data" / "assets" / "fixture.png").read_bytes(), RASTER)
        report = bootstrap_report(catalog, self.ledger, self.root, NOW)
        self.assertEqual((report["totalCount"], report["completeCount"], report["pendingCount"], report["attentionCount"], report["removedCount"]), (2, 1, 0, 0, 1))
        self.assertEqual(report["status"], "complete")
        self.assertEqual(self.ledger["bootstrap"]["cohortDOIs"], [case["doi"], record(2)["doi"]])

    def test_known_material_source_reasons_are_controlled(self):
        for gap, expected in (("formal", "formal_version_unavailable"), ("figure", "selected_figure_unavailable"),
                              ("reuse", "figure_reuse_unavailable"), ("scripts", "author_scripts_unavailable"),
                              ("private", "author_scripts_unavailable")):
            with self.subTest(gap=gap):
                case = self.full_case()
                if gap == "formal":
                    case["article"] = {"status": "no_final_version", "figures": []}
                    case["automation"]["stages"]["article"] = "no_final_version"
                elif gap == "figure":
                    case["figure"] = "Figure 2"
                elif gap == "reuse":
                    case["article"]["figures"][0]["reuseStatus"] = "link_only"
                else:
                    status = "link_only_private" if gap == "private" else "no_scripts_found"
                    case["repository"] = {"status": status, "scripts": [], "inputs": []}
                    case["automation"]["stages"]["repository"] = status
                catalog, ledger = self.catalog([case]), {"days": {}, "candidates": {}}
                ensure_bootstrap(catalog, ledger, NOW)
                self.scans(case)
                result = prune_uncollectable(catalog, ledger, self.root, NOW)
                if gap in {"figure", "scripts", "private"}:
                    self.assertEqual(result["removedCount"], 0)
                    self.assertEqual(len(catalog["papers"]), 1)
                    continue
                self.assertEqual(result["reasons"][case["doi"]], expected)
                self.assertEqual(catalog["papers"], [])
                self.assertEqual(bootstrap_report(catalog, ledger, self.root, NOW)["removedCount"], 1)

    def test_unchecked_stub_never_removed_from_old_or_forged_total_scan_count(self):
        case = record(1)
        case["automation"]["bootstrap"] = {"attempts": 10, "unsuccessfulScans": 10,
            "lastAttemptAt": NOW, "sourceGapScans": {"input_basis_unavailable": 10}}
        catalog = self.catalog([case])
        self.assertEqual(prune_uncollectable(catalog, self.ledger, self.root, NOW)["removedCount"], 0)
        self.assertEqual(len(catalog["papers"]), 1)

    def test_prior_ai_failures_do_not_prove_new_input_gap(self):
        case = self.full_case()
        case["ai"] = {"status": "invalid_response"}
        case["automation"]["stages"]["ai"] = "invalid_response"
        self.scans(case)
        self.assertEqual(case["automation"]["bootstrap"]["unsuccessfulScans"], 3)
        self.assertEqual(case["automation"]["bootstrap"]["sourceGapScans"], {})
        case["repository"]["inputs"] = []
        self.scans(case, 1)
        self.assertEqual(case["automation"]["bootstrap"]["sourceGapScans"]["input_basis_unavailable"], 1)
        catalog = self.catalog([case])
        self.assertEqual(prune_uncollectable(catalog, self.ledger, self.root, NOW)["removedCount"], 0)

    def test_ai_metrics_or_keys_alone_never_remove_article(self):
        for stage, status in (("ai", "authentication_error"), ("ai", "payment_required"),
                              ("ai", "invalid_response"), ("ai", "network_error"),
                              ("metrics", "missing_key"), ("metrics", "network_error")):
            with self.subTest(stage=stage, status=status):
                case = self.full_case()
                case[stage] = {"status": status}
                case["automation"]["stages"][stage] = status
                self.scans(case)
                catalog = self.catalog([case])
                self.assertEqual(prune_uncollectable(catalog, self.ledger, self.root, NOW)["removedCount"], 0)
                self.assertEqual(uncollectable_reasons(case, root=self.root), ())

    def test_network_source_and_retained_data_cannot_delete(self):
        for value in ("unavailable", "network_error", "rate_limited", "budget_exhausted", "authentication_error", "payment_required"):
            with self.subTest(value=value):
                case = self.full_case()
                case["repository"]["inputs"] = []
                self.scans(case)
                case["article"]["retained_from_previous"] = True
                case["article"]["last_attempt_status"] = value
                case["automation"]["stages"]["article"] = value
                catalog = self.catalog([case])
                self.assertEqual(prune_uncollectable(catalog, self.ledger, self.root, NOW)["removedCount"], 0)

    def test_budget_or_time_skip_never_adds_removal_source_allowance(self):
        case = self.full_case()
        case["repository"]["inputs"] = []
        for stage in ("article", "metrics", "ai"):
            with self.subTest(stage=stage):
                active = deepcopy(case)
                active["automation"]["stages"][stage] = "budget_exhausted"
                self.scans(active, 20)
                self.assertEqual(active["automation"]["bootstrap"]["sourceGapScans"], {})
                self.assertEqual(active["automation"]["bootstrap"]["unsuccessfulScans"], 0)

    def test_subresource_failures_and_tree_limit_block_pruning(self):
        for kind in ("figure_failed", "figure_pending", "source_failed", "header_failed", "header_limited", "tree_limited", "source_count_limited"):
            with self.subTest(kind=kind):
                case = self.full_case()
                case["repository"]["inputs"] = []
                if kind.startswith("figure_"):
                    case["article"]["figures"][0]["downloadStatus"] = kind.split("_", 1)[1]
                elif kind == "source_failed":
                    case["repository"]["limitations"] = ["Source unavailable: DO_NOT_LEAK (BudgetExceeded)"]
                elif kind == "header_failed":
                    case["repository"]["inputs"] = [{"status": "header_fetch_failed"}]
                elif kind == "header_limited":
                    case["repository"]["inputs"] = [{"status": "repository_file_found", "path": "large.csv"}]
                elif kind == "tree_limited":
                    case["repository"]["limitations"] = ["GitHub tree truncated: not all repository files were inspected."]
                else:
                    case["repository"]["sourceScanLimited"] = True
                self.scans(case)
                catalog = self.catalog([case])
                self.assertEqual(prune_uncollectable(catalog, self.ledger, self.root, NOW)["removedCount"], 0)

    def test_source_gap_allowance_resets_after_true_material_recovery(self):
        case = self.full_case()
        good = deepcopy(case["repository"]["inputs"])
        case["repository"]["inputs"] = []
        self.scans(case, 2)
        case["repository"]["inputs"] = good
        self.scans(case, 1)
        self.assertEqual(case["automation"]["bootstrap"]["sourceGapScans"], {})
        case["repository"]["inputs"] = []
        self.scans(case, 1)
        self.assertEqual(case["automation"]["bootstrap"]["sourceGapScans"], {"input_basis_unavailable": 1})

    def test_noncohort_real_image_is_retained_without_expanding_original_cohort(self):
        original = self.full_case(1)
        catalog = self.catalog([original])
        ensure_bootstrap(catalog, self.ledger, NOW)
        later = self.full_case(2)
        later["repository"]["inputs"] = []
        catalog["papers"].append(later)
        catalog["inputSpecs"][later["id"]] = {}
        self.scans(later)
        result = prune_uncollectable(catalog, self.ledger, self.root, NOW)
        self.assertEqual(result["deletedDOIs"], [])
        self.assertEqual(safe_removed_dois(self.ledger), frozenset())
        self.assertEqual(len(catalog["papers"]), 2)
        report = bootstrap_report(catalog, self.ledger, self.root, NOW)
        self.assertEqual((report["totalCount"], report["completeCount"], report["removedCount"]), (1, 1, 0))

    def test_external_deletion_without_tombstone_is_still_attention(self):
        catalog = self.catalog([self.full_case()])
        ensure_bootstrap(catalog, self.ledger, NOW)
        catalog["papers"] = []
        report = bootstrap_report(catalog, self.ledger, self.root, NOW)
        self.assertEqual((report["attentionCount"], report["removedCount"], report["status"]), (1, 0, "needs_attention"))

    def test_malformed_tombstones_fail_closed_before_mutation(self):
        doi = record(1)["doi"]
        malformed = [None, [], {"not_a_doi": {"reasonCode": "input_basis_unavailable", "removedAt": NOW}},
                     {doi: {"reasonCode": "DO_NOT_LEAK", "removedAt": NOW}},
                     {doi: {"reasonCode": "input_basis_unavailable", "removedAt": "yesterday"}},
                     {doi: {"reasonCode": "input_basis_unavailable", "removedAt": NOW, "response": "DO_NOT_LEAK"}}]
        for tombstones in malformed:
            with self.subTest(tombstones=tombstones):
                catalog = self.catalog([self.full_case()])
                ledger = {"removedPapers": tombstones}
                before = deepcopy(catalog)
                with self.assertRaises(ValueError):
                    prune_uncollectable(catalog, ledger, self.root, NOW)
                self.assertEqual(catalog, before)

    def test_cohort_tombstone_membership_and_backing_record_fail_closed(self):
        catalog = self.catalog([self.full_case()])
        ensure_bootstrap(catalog, self.ledger, NOW)
        for removed in ([record(2)["doi"]], [record(1)["doi"]], [record(1)["doi"], record(1)["doi"]], None):
            with self.subTest(removed=removed):
                ledger = deepcopy(self.ledger)
                ledger["bootstrap"]["removedDOIs"] = removed
                with self.assertRaises(ValueError):
                    ensure_bootstrap(catalog, ledger, NOW)

    def test_valid_tombstone_cannot_hide_still_present_article(self):
        case = self.full_case()
        catalog = self.catalog([case])
        ensure_bootstrap(catalog, self.ledger, NOW)
        self.ledger["removedPapers"] = {case["doi"]: {"reasonCode": "input_basis_unavailable", "removedAt": NOW}}
        self.ledger["bootstrap"]["removedDOIs"] = [case["doi"]]
        with self.assertRaises(ValueError):
            bootstrap_report(catalog, self.ledger, self.root, NOW)

    def test_safe_state_filters_untrusted_source_gap_counter_values(self):
        case = self.full_case()
        case["automation"]["bootstrap"] = {"sourceGapScans": {"input_basis_unavailable": True, "DO_NOT_LEAK": 3,
            "formal_version_unavailable": [], "selected_figure_unavailable": 3}}
        state = _safe_record_state(case)
        self.assertEqual(state["sourceGapScans"], {"selected_figure_unavailable": 3})
        self.assertNotIn("DO_NOT_LEAK", json.dumps(state))


if __name__ == "__main__":
    unittest.main()
