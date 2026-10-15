"""Actual five-paper admission must see every eligible research lane."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_plot_library.config import Settings
from paper_plot_library.pipeline import run_pipeline
from paper_plot_library.scope import POLICY
from paper_plot_library.storage import read_json, write_json
from test_bootstrap import NOW, RASTER
from test_bootstrap_pipeline import materials
from test_pipeline_audit import Client, adapters, record


class FairProcessingBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ppl-lanes-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data/assets").mkdir(parents=True)
        (self.root / "data/assets/fixture.png").write_bytes(RASTER)
        write_json(self.root / "data/catalog.json", {
            "authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dimensions": {}})
        self.settings = Settings(research_scope=POLICY, require_complete_new_cases=True,
                                 repair_limit=0, max_papers_per_run=5, citation_enrichment=False)
        self.source = materials(complete=True)
        self.core = [dict(record(i), paper=f"Aging multiomics in human tissue {i}") for i in range(1, 9)]
        self.expansion = [dict(record(20), paper="Diabetes transcriptomics in human islets")]
        self.tools = [dict(record(30 + i), paper=f"Genomics software framework {i}") for i in range(2)]
        self.visits = []

    def batch(self, candidates):
        by_doi = {p["doi"]: p for p in candidates}
        def lookup(client, doi):
            self.visits.append(doi)
            return dict(by_doi[doi], status="matched", abstract="Formal publication metadata.")
        active = adapters(discover=lambda *args: deepcopy(candidates), lookup=lookup,
            article=lambda *args: deepcopy(self.source["article"]),
            repository=lambda *args: deepcopy(self.source["repository"]),
            metrics=lambda *args: deepcopy(self.source["metrics"]),
            summary=lambda *args: deepcopy(self.source["ai"]))
        with patch("paper_plot_library.pipeline.stamp", return_value=NOW), \
                patch("paper_plot_library.pipeline.build_site"):
            return run_pipeline(self.root, self.settings, Client(), active)

    def papers(self):
        return read_json(self.root / "data/catalog.json")["papers"]

    def test_full_core_page_cannot_starve_tools_or_metabolic_lane(self):
        report = self.batch(self.core + self.expansion + self.tools)
        self.assertEqual((report["checkedCount"], report["newCount"]), (5, 5))
        self.assertEqual(Counter(p["researchScope"]["tier"] for p in self.papers()),
                         {"primary": 3, "expansion": 1, "tools": 1})
        self.assertEqual(len(set(self.visits)), 5)

    def test_cooling_duplicate_and_invalid_candidates_cannot_take_reserved_tool_slot(self):
        cool = self.tools[0]
        write_json(self.root / "data/ledger.json", {"days": {}, "candidates": {
            cool["doi"]: {"checkedAt": NOW, "status": "pending", "retryClass": "transient"}}})
        invalid = [dict(record(40), paper="Genomics software framework", isPreprint=True),
                   dict(record(41), paper="Genomics software framework", isRetracted=True),
                   dict(record(42), paper="Genomics software framework", date="2099-01-01")]
        report = self.batch(self.core * 2 + self.expansion + [cool] * 3 + invalid + [self.tools[1]])
        self.assertEqual((report["checkedCount"], report["newCount"]), (5, 5))
        self.assertNotIn(cool["doi"], self.visits)
        self.assertIn(self.tools[1]["doi"], self.visits)
        self.assertEqual(len(set(self.visits)), len(self.visits))

    def test_absent_lanes_donate_slots_without_fabricating_a_tool(self):
        self.assertEqual(self.batch(self.core)["newCount"], 5)
        self.assertEqual({p["researchScope"]["tier"] for p in self.papers()}, {"primary"})

    def test_rerun_does_not_duplicate_or_relabel_existing_admission(self):
        candidates = self.core + self.expansion + self.tools
        self.batch(candidates)
        before = deepcopy(self.papers())
        old_visits = len(self.visits)
        report = self.batch(candidates)
        self.assertEqual((report["newCount"], report["dailyNewCount"]), (0, 5))
        self.assertEqual(self.papers(), before)
        self.assertEqual(len(self.visits), old_visits)


if __name__ == "__main__":
    unittest.main()
