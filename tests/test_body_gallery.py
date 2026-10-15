"""Publication gate acceptance: real local rasters, never provider traffic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from paper_plot_library.site import build_site, has_publishable_body_figure
from test_bootstrap import NOW, RASTER


PROJECT = Path(__file__).resolve().parents[1]


class BodyGalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-body-gallery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / "web", self.root / "web")
        self.assets = self.root / "data" / "assets"
        self.assets.mkdir(parents=True)

    def case(self, index=1, label="Figure 1", raw=RASTER, checked=True):
        asset = f"assets/body-{index}.png"
        (self.root / "data" / asset).write_bytes(raw)
        figure = {"id": "F1", "label": label, "asset": asset,
                  "reuseStatus": "allowed", "sha256": hashlib.sha256(raw).hexdigest(),
                  "sourceURL": "https://example.org/synthetic/body-figure.png",
                  "license": "CC BY 4.0", "downloadStatus": "downloaded"}
        return {"id": f"case-{index}", "doi": f"10.1234/body-{index}", "paper": "Synthetic body figure fixture",
                "runPerformed": False, "figure": label,
                "article": {"status": "inspected", "license": "CC BY 4.0", "figures": [figure]},
                "automation": {"status": "partial", "lastCheckedAt": NOW if checked else "",
                               "stages": {"metadata": "matched", "article": "inspected"} if checked else {}},
                "repository": {"status": "not_found", "scripts": []}, "ai": {"status": "missing_key"}}

    def build(self, papers, reports=None, bootstrap=None):
        catalog = {"authorCodeExecuted": False, "papers": papers,
                   "inputSpecs": {paper["id"]: {"tree": "synthetic input note", "files": []} for paper in papers}}
        if bootstrap is not None:
            catalog["bootstrap"] = bootstrap
        original = deepcopy(catalog)
        eligible = [paper["id"] for paper in papers if has_publishable_body_figure(self.root, paper)]
        self.assertEqual(catalog, original, "Admission image checks must be read-only.")
        reports = reports or []
        original_reports = deepcopy(reports)
        output = build_site(self.root, catalog, reports)
        self.assertEqual(catalog, original, "Publishing must not physically prune private source records.")
        self.assertEqual(reports, original_reports, "Internal failure/recovery reports must remain intact.")
        public = json.loads((output / "data" / "catalog.json").read_text())
        self.assertEqual([paper["id"] for paper in public["papers"]], eligible,
                         "A counted admission and the gallery must use exactly the same image gate.")
        manifest = json.loads((output / "data" / "assets-manifest.json").read_text())
        return public, manifest, output

    def assert_hidden(self, case):
        public, manifest, _ = self.build([case])
        self.assertEqual(public["papers"], [])
        self.assertEqual(public["inputSpecs"], {})
        self.assertEqual(manifest, [])

    def test_verified_body_images_remain_visible_despite_ai_and_metrics_failure(self):
        case = self.case()
        case["ai"].update(status="authentication_error", error_reason="http_401", http_status=401)
        case["metrics"] = {"status": "missing_key"}
        public, manifest, output = self.build([case])
        self.assertEqual([paper["id"] for paper in public["papers"]], [case["id"]])
        self.assertEqual(public["papers"][0]["ai"]["status"], "authentication_error")
        self.assertTrue(public["papers"][0]["availability"]["hasImage"])
        self.assertEqual(manifest, [case["article"]["figures"][0]["asset"]])
        self.assertTrue((output / manifest[0]).is_file())
        self.assertEqual(public["publicationPolicy"], "body-images-only")
        self.assertEqual(public["publicationSummary"], {"publishedCount": 1, "hiddenCount": 0, "totalCount": 1})
        self.assertEqual(public["papers"][0]["displayFigure"],
                         {"id": "F1", "label": "Figure 1", "reference": {"scope": "main", "number": "1"}})

    def test_main_preview_does_not_replace_original_supplementary_selection(self):
        case = self.case()
        case["figure"] = "Supplementary Figure 12"
        public, _, _ = self.build([case])
        paper = public["papers"][0]
        self.assertEqual(paper["figure"], case["figure"])
        self.assertEqual(paper["displayFigure"]["reference"]["scope"], "main")
        self.assertFalse(paper["availability"]["hasImage"], "The original SI selection is still missing.")
        self.assertEqual(paper["availability"]["verification"], "unverified")

    def test_prefer_available_original_main_selection_over_another_main_image(self):
        case = self.case()
        another = deepcopy(case["article"]["figures"][0])
        another.update(id="F2", label="Figure 2")
        case["article"]["figures"].append(another)
        case["figure"] = "Figure 2b"
        public, _, _ = self.build([case])
        self.assertEqual(public["papers"][0]["displayFigure"]["id"], "F2")

    def test_missing_or_unsafe_original_source_url_does_not_publish(self):
        for url in (None, "", "http://example.org/image.png", "https://example.org/image.png?token=private"):
            with self.subTest(url=url):
                case = self.case()
                case["article"]["figures"][0]["sourceURL"] = url
                self.assert_hidden(case)

    def test_conflicting_label_or_number_cannot_be_promoted_to_main(self):
        for label, number in (("Supplementary Figure 1", "1"), ("Extended Data Fig. 1", "1"),
                              ("Supplementary plate", "1"), ("Extended unknown", "1"), ("Figure 1", "2")):
            with self.subTest(label=label, number=number):
                case = self.case(label=label)
                case["article"]["figures"][0]["reference"] = {"scope": "main", "number": number}
                self.assert_hidden(case)

    def test_unchecked_and_missing_images_are_not_a_public_candidate_pool(self):
        for checked in (False, True):
            with self.subTest(checked=checked):
                case = self.case(checked=checked)
                case["article"]["figures"] = []
                case["figureURL"] = "https://example.org/official-figure"
                self.assert_hidden(case)

    def test_supplementary_and_extended_images_cannot_stand_in_for_body_images(self):
        for label in ("Supplementary Fig. S1", "Supporting Figure 1", "Extended Data Fig. 1"):
            with self.subTest(label=label):
                self.assert_hidden(self.case(label=label))

    def test_jats_ancestor_supplementary_scope_overrides_ordinary_figure_label(self):
        case = self.case()
        case["article"]["figures"][0]["reference"] = {"scope": "supplementary", "number": "1", "panel": ""}
        self.assert_hidden(case)

    def test_automatic_unselected_case_can_publish_a_verified_main_image(self):
        case = self.case()
        case["figure"] = ""
        public, _, _ = self.build([case])
        self.assertEqual(len(public["papers"]), 1)
        self.assertTrue(public["papers"][0]["availability"]["hasImage"])

    def test_png_magic_without_a_decodable_image_is_not_published(self):
        self.assert_hidden(self.case(raw=b"\x89PNG\r\n\x1a\nnot-a-real-png"))

    def test_missing_or_mismatched_asset_digest_is_not_published(self):
        for digest in (None, "a" * 64, "invalid", True):
            with self.subTest(digest=digest):
                case = self.case()
                figure = case["article"]["figures"][0]
                if digest is None:
                    figure.pop("sha256")
                else:
                    figure["sha256"] = digest
                self.assert_hidden(case)

    def test_derivative_asset_hash_not_original_source_hash_proves_published_bytes(self):
        case = self.case()
        figure = case["article"]["figures"][0]
        figure["assetSHA256"] = figure["sha256"]
        figure["sha256"] = "a" * 64  # Different legitimate original TIFF/source bytes.
        public, manifest, _ = self.build([case])
        self.assertEqual(len(public["papers"]), 1)
        self.assertEqual(manifest, [figure["asset"]])
        figure["assetSHA256"] = "b" * 64
        self.assert_hidden(case)

    def test_non_reusable_and_excluded_records_are_not_public_results(self):
        for excluded in (False, True):
            with self.subTest(excluded=excluded):
                case = self.case()
                if excluded:
                    case["automation"]["status"] = "excluded"
                else:
                    case["article"]["figures"][0]["reuseStatus"] = "link_only"
                self.assert_hidden(case)

    def test_reuse_flag_cannot_override_restricted_or_third_party_license(self):
        for license, caption in (("CC BY-NC 4.0", ""), ("CC BY-ND 4.0", ""),
                                 ("All rights reserved", ""), ("CC BY 4.0", "Reproduced from a third-party source with permission")):
            with self.subTest(license=license, caption=caption):
                case = self.case()
                case["article"]["license"] = license
                figure = case["article"]["figures"][0]
                figure["license"] = license
                figure["caption"] = caption
                self.assert_hidden(case)
        case = self.case()
        case["article"]["license"] = "All rights reserved"
        case["article"]["figures"][0].pop("license")
        self.assert_hidden(case)

    def test_existing_malformed_manifest_does_not_silently_forget_owned_images(self):
        case = self.case()
        _, manifest, output = self.build([case])
        for damaged in ("invalid JSON", '{"unexpected": true}', '["assets/../unsafe.png"]'):
            with self.subTest(damaged=damaged):
                (output / "data" / "assets-manifest.json").write_text(damaged)
                with self.assertRaisesRegex(ValueError, "manifest"):
                    self.build([case])
                self.assertTrue((output / manifest[0]).is_file(), "Failure must not unlink an untrusted target.")

    def test_malformed_report_types_and_failure_metadata_cannot_export_hidden_queue(self):
        visible, hidden = self.case(1), self.case(2)
        hidden["article"]["figures"] = []
        secret_queue = {"doi": hidden["doi"], "id": hidden["id"]}
        malformed = {field: deepcopy(secret_queue) for field in
                     ("failures", "missingKeys", "status", "mode", "checkedCount", "date", "stageCounts")}
        nested = {"failures": [{"doi": visible["doi"], "stage": "ai", "status": "error", "extra": secret_queue,
                                "error": hidden["doi"], "error_reason": hidden["doi"]}],
                  "missingKeys": [hidden["doi"], "LLM_API_KEY", secret_queue],
                  "stageCounts": {"ai": {hidden["doi"]: 1, "error": 1}, hidden["id"]: {"error": 1}}}
        public, _, output = self.build([visible, hidden], [malformed, nested])
        exported = (output / "data" / "updates.json").read_text()
        self.assertNotIn(hidden["doi"], exported + json.dumps(public))
        self.assertNotIn(hidden["id"], exported + json.dumps(public))
        self.assertIn("LLM_API_KEY", exported)

    def test_intermediate_source_symlink_even_inside_asset_root_is_rejected(self):
        case = self.case()
        actual = self.assets / "real"
        actual.mkdir()
        (actual / "body.png").write_bytes(RASTER)
        (self.assets / "alias").symlink_to(actual, target_is_directory=True)
        case["article"]["figures"][0]["asset"] = "assets/alias/body.png"
        self.assert_hidden(case)

    def test_symlinked_data_parent_cannot_publish_outside_project_bytes(self):
        case = self.case()
        outside = self.root / "outside-data"
        (self.root / "data").rename(outside)
        (self.root / "data").symlink_to(outside, target_is_directory=True)
        self.assert_hidden(case)

    def test_only_retained_result_inputs_survive_projection(self):
        visible = self.case(1)
        hidden = self.case(2)
        hidden["article"]["figures"][0]["asset"] = "assets/absent.png"
        public, _, _ = self.build([visible, hidden])
        self.assertEqual([paper["id"] for paper in public["papers"]], [visible["id"]])
        self.assertEqual(set(public["inputSpecs"]), {visible["id"]})
        self.assertEqual(public["publicationSummary"], {"publishedCount": 1, "hiddenCount": 1, "totalCount": 2})

    def test_private_queue_details_cannot_reappear_via_bootstrap_or_historical_reports(self):
        visible = self.case(1)
        hidden = self.case(2)
        hidden["article"]["figures"] = []
        bootstrap = {"status": "active", "totalCount": 2, "completeCount": 1,
                     "pendingCount": 1, "attentionCount": 0,
                     "issues": [{"doi": hidden["doi"], "reasonCodes": ["selected_figure_missing"]}]}
        report = {"date": NOW[:10], "status": "partial", "checkedCount": 2,
                  "bootstrap": bootstrap,
                  "failures": [{"doi": hidden["doi"], "stage": "article", "status": "no_final_version"}],
                  "records": [deepcopy(hidden)], "results": [deepcopy(hidden)], "processed": [deepcopy(hidden)]}
        public, _, output = self.build([visible, hidden], [report], bootstrap)
        serialized = json.dumps(public) + (output / "data" / "updates.json").read_text()
        self.assertNotIn(hidden["doi"], serialized)
        self.assertNotIn(hidden["id"], serialized)
        self.assertEqual(public["bootstrap"]["totalCount"], 2, "Aggregate processing progress may remain public.")

    def test_invalidated_previous_public_body_image_is_removed_without_touching_user_files(self):
        case = self.case()
        _, manifest, output = self.build([case])
        user_file = output / "assets" / "user-note.txt"
        user_file.write_text("not builder-owned")
        case["article"]["figures"][0]["reuseStatus"] = "link_only"
        public, next_manifest, _ = self.build([case])
        self.assertEqual(public["papers"], [])
        self.assertEqual(next_manifest, [])
        self.assertFalse((output / manifest[0]).exists())
        self.assertEqual(user_file.read_text(), "not builder-owned")


if __name__ == "__main__":
    unittest.main()
