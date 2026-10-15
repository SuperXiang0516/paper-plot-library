"""Offline scope gates at collection, paid annotation and publication boundaries."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import editorial_score, inspect_record, refresh_legacy_scope, run_pipeline
from paper_plot_library.scope import POLICY, assess_scope, matches_scope
from paper_plot_library.site import build_site
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_pipeline_audit import Client, adapters, record


PROJECT = Path(__file__).resolve().parents[1]


class ScopePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-scope-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data" / "assets").mkdir(parents=True)
        (self.root / "data" / "assets" / "fixture.png").write_bytes(RASTER)
        self.catalog([])
        self.settings = Settings(research_scope=POLICY, require_complete_new_cases=True,
            repair_limit=0, citation_enrichment=False)
        self.source = materials(complete=True)

    def catalog(self, papers):
        value = {"authorCodeExecuted": False, "papers": papers, "inputSpecs": {}, "dimensions": {}}
        write_json(self.root / "data" / "catalog.json", value)
        return value

    def candidate(self, index=1, title="Long-read sequencing of human aging", abstract=""):
        return dict(record(index), paper=title, abstract=abstract)

    def active(self, candidates, **changes):
        by_doi = {p["doi"]: p for p in candidates}
        value = adapters(discover=lambda *args: deepcopy(candidates),
            lookup=lambda client, doi: dict(by_doi[doi], status="matched"),
            article=lambda *args: deepcopy(self.source["article"]),
            repository=lambda *args: deepcopy(self.source["repository"]),
            metrics=lambda *args: deepcopy(self.source["metrics"]),
            summary=lambda *args: deepcopy(self.source["ai"]))
        value.update(changes)
        return value

    def batch(self, active):
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), \
                patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, Client(), active)

    def test_marine_imaging_never_downloads_or_calls_ai(self):
        marine = self.candidate(title="Marine biodiversity monitoring by DNA metabarcoding",
                                abstract="Comparison of imaging and sequencing in Mediterranean reefs.")
        calls = []
        report = self.batch(self.active([marine], lookup=lambda *args: calls.append("lookup"),
            article=lambda *args: calls.append("article"), summary=lambda *args: calls.append("AI")))
        self.assertEqual((report["newCount"], report["checkedCount"], report["scopeRejectedCount"]), (0, 0, 1))
        self.assertEqual(calls, [])
        self.assertEqual(editorial_score(marine)["components"]["topic"], 0)

    def test_primary_beats_higher_scoring_expansion(self):
        aging = self.candidate()
        diabetes = self.candidate(2, "Diabetes multiomics in humans")
        diabetes.update(repository={"scripts": [1], "inputs": [{"fields": [1]}]},
                        journal="Nature", graphForms=["heatmap"])
        self.assertGreater(editorial_score(diabetes)["total"], editorial_score(aging)["total"])
        self.settings.max_new_per_day = 1
        report = self.batch(self.active([diabetes, aging]))
        current = read_json(self.root / "data" / "catalog.json")
        self.assertEqual(report["newCount"], 1)
        self.assertEqual([p["doi"] for p in current["papers"]], [aging["doi"]])

    def test_diabetes_omics_can_fill_a_success_slot(self):
        report = self.batch(self.active([self.candidate(title="Diabetes single-cell RNA-seq in human islets")]))
        current = read_json(self.root / "data" / "catalog.json")
        self.assertEqual((report["newCount"], report["dailyNewCount"]), (1, 1))
        self.assertEqual(current["papers"][0]["researchScope"]["tier"], "expansion")

    def test_verified_metadata_can_reject_a_misleading_discovery_payload(self):
        candidate = self.candidate()
        calls = []
        current = dict(candidate, paper="Marine DNA sequencing and imaging", abstract="Biodiversity monitoring")
        report = self.batch(self.active([candidate], lookup=lambda *args: dict(current, status="matched"),
            article=lambda *args: calls.append("article"), summary=lambda *args: calls.append("AI")))
        self.assertEqual((report["checkedCount"], report["newCount"]), (1, 0))
        self.assertEqual(calls, [])

    def test_explicit_repair_cannot_bypass_scope(self):
        marine = self.candidate(title="Marine DNA sequencing and imaging")
        calls = []
        revised = inspect_record(Client(), marine, self.root, self.settings,
            self.active([marine], article=lambda *args: calls.append("article"),
                        summary=lambda *args: calls.append("AI")))
        self.assertEqual(revised["automation"]["status"], "excluded")
        self.assertEqual(calls, [])

    def test_scope_is_rechecked_after_summary_before_admission(self):
        candidate = self.candidate()
        def changed_summary(client, selected, *args):
            selected.update(paper="Marine imaging and DNA metabarcoding", abstract="Biodiversity monitoring")
            return deepcopy(self.source["ai"])
        report = self.batch(self.active([candidate], summary=changed_summary))
        self.assertEqual((report["newCount"], report["dailyNewCount"]), (0, 0))

    def test_abstract_based_verified_decision_survives_public_redaction(self):
        candidate = self.candidate(title="Molecular mechanisms in human tissues",
                                   abstract="Nanopore sequencing reveals cellular aging.")
        self.assertEqual(self.batch(self.active([candidate]))["newCount"], 1)
        current = read_json(self.root / "data" / "catalog.json")
        self.assertNotIn("abstract", current["papers"][0])
        self.assertTrue(matches_scope(current["papers"][0]))
        self.assertEqual(current["papers"][0]["researchScope"]["source"], "verified-metadata")

    def test_deploy_only_applies_scope_and_removes_obsolete_public_asset(self):
        shutil.copytree(PROJECT / "web", self.root / "web")
        marine = dict(deepcopy(self.source), paper="Marine DNA sequencing and imaging")
        raw_catalog = self.catalog([marine])
        before = deepcopy(raw_catalog)
        output = build_site(self.root, raw_catalog, [])
        asset = output / "assets" / "fixture.png"
        self.assertTrue(asset.exists())
        write_json(self.root / "config.json", {"research_scope": POLICY})
        build_site(self.root, raw_catalog, [])
        public = read_json(output / "data" / "catalog.json")
        self.assertEqual(public["papers"], [])
        self.assertEqual(public["publicationSummary"], {"publishedCount": 0, "hiddenCount": 1, "totalCount": 1})
        self.assertEqual(read_json(output / "data" / "assets-manifest.json"), [])
        self.assertFalse(asset.exists())
        self.assertEqual(raw_catalog, before)
        self.assertTrue((self.root / "data" / "assets" / "fixture.png").exists())

    def test_site_uses_original_abstract_but_does_not_publish_it(self):
        shutil.copytree(PROJECT / "web", self.root / "web")
        case = dict(deepcopy(self.source), paper="Human tissue mechanisms",
                    abstract="Transcriptomics of cellular senescence")
        catalog = dict(self.catalog([case]), researchScopePolicy=POLICY)
        output = build_site(self.root, catalog, [])
        public = read_json(output / "data" / "catalog.json")
        self.assertEqual(len(public["papers"]), 1)
        self.assertNotIn("abstract", public["papers"][0])
        self.assertEqual(public["papers"][0]["researchScope"]["tier"], "primary")

    def test_legacy_ai_and_curated_tags_do_not_rescue_unrelated_body_images(self):
        shutil.copytree(PROJECT / "web", self.root / "web")
        case = dict(deepcopy(self.source), paper="Marine biodiversity imaging",
                    topic="衰老多组学", description="三代测序", ai={"summary_zh": "衰老研究"})
        catalog = dict(self.catalog([case]), researchScopePolicy=POLICY)
        output = build_site(self.root, catalog, [])
        self.assertEqual(read_json(output / "data" / "catalog.json")["papers"], [])

    def test_legacy_metadata_migration_rescues_related_atlas_without_source_work(self):
        atlas = self.candidate(title="Human skeletal muscle aging atlas")
        atlas.pop("abstract")
        catalog = self.catalog([atlas])
        calls = []
        def metadata(client, papers):
            calls.append(len(papers))
            return {atlas["doi"]: dict(atlas, status="matched", abstract="Single-cell RNA sequencing of aging human muscle.")}
        self.assertEqual(refresh_legacy_scope(Client(), catalog, self.settings, {"scope_metadata": metadata}), 1)
        self.assertTrue(matches_scope(catalog["papers"][0]))
        self.assertNotIn("abstract", catalog["papers"][0])
        self.assertEqual(catalog["papers"][0]["automation"], atlas["automation"])
        self.assertEqual(refresh_legacy_scope(Client(), catalog, self.settings, {"scope_metadata": metadata}), 0)
        self.assertEqual(calls, [1])

    def test_failed_or_wrong_doi_migration_cannot_manufacture_scope(self):
        atlas = self.candidate(title="Human skeletal muscle aging atlas")
        atlas.pop("abstract")
        catalog = self.catalog([atlas])
        original = deepcopy(catalog)
        wrong = {atlas["doi"]: dict(atlas, doi="10.1234/wrong", status="matched", abstract="Aging genomics")}
        self.assertEqual(refresh_legacy_scope(Client(), catalog, self.settings,
                                            {"scope_metadata": lambda *args: wrong}), 0)
        self.assertEqual(catalog, original)

    def test_public_legacy_decision_with_no_current_abstract_is_not_lost(self):
        shutil.copytree(PROJECT / "web", self.root / "web")
        atlas = dict(deepcopy(self.source), paper="Human skeletal muscle aging atlas")
        catalog = dict(self.catalog([atlas]), researchScopePolicy=POLICY)
        canonical = dict(atlas, status="matched", abstract="Single-cell RNA sequencing in human aging muscle.")
        refresh_legacy_scope(Client(), catalog, self.settings,
                             {"scope_metadata": lambda *args: {atlas["doi"]: canonical}})
        output = build_site(self.root, catalog, [])
        public = read_json(output / "data" / "catalog.json")
        self.assertEqual(len(public["papers"]), 1)
        self.assertEqual(public["papers"][0]["researchScope"]["tier"], "primary")

    def test_metadata_migration_rotates_past_unresolved_head_and_prioritizes_today(self):
        candidates = [self.candidate(i, "Unresolved tissue atlas") for i in range(1, 62)]
        candidates[-1]["admittedAt"] = NOW
        catalog = self.catalog(candidates)
        selected = []
        def missing(client, papers):
            selected.append([p["doi"] for p in papers])
            return {}
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW):
            refresh_legacy_scope(Client(), catalog, self.settings, {"scope_metadata": missing})
            refresh_legacy_scope(Client(), catalog, self.settings, {"scope_metadata": missing})
        self.assertEqual([len(batch) for batch in selected], [50, 50])
        self.assertEqual([batch[0] for batch in selected], [candidates[-1]["doi"]] * 2)
        self.assertEqual(set(selected[0]) | set(selected[1]), {p["doi"] for p in candidates})


if __name__ == "__main__":
    unittest.main()
