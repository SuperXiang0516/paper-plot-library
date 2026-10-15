"""Offline eligibility regressions: scope is not a journal/image score."""
import unittest

from paper_plot_library.scope import POLICY, assess_scope, eligible_daily_dois, has_verified_scope, matches_scope, query_constraint, scope_priority


class ScopeTests(unittest.TestCase):
    def paper(self, title, abstract=""):
        return {"doi": "10.1234/fixture", "paper": title, "abstract": abstract}

    def test_imaging_is_not_aging(self):
        record = self.paper("Imaging and transcriptomics in human tissue")
        self.assertFalse(matches_scope(record))
        self.assertNotIn("aging", assess_scope(record)["topicMatches"])

    def test_ordinary_cohort_age_is_not_an_aging_topic(self):
        record = self.paper("Transcriptomics of acute infection in humans",
                            "We enrolled patients aged 18 to 65 and performed RNA sequencing.")
        self.assertFalse(matches_scope(record))
        self.assertTrue(matches_scope(self.paper("Transcriptomics of aged mice")))

    def test_aging_sequencing_and_omics_primary(self):
        for title in ("Aging nanopore sequencing in mice", "Long-read RNA-seq of human senescence",
                      "Multi-omics of longevity", "Epigenetic clocks from methylome profiling",
                      "衰老相关三代测序与多组学研究"):
            with self.subTest(title=title):
                assessment = assess_scope(self.paper(title))
                self.assertEqual((assessment["status"], assessment["tier"]), ("included", "primary"))

    def test_diabetes_metabolic_omics_expansion(self):
        for title in ("Diabetes transcriptomics in patients", "Insulin resistance multiomics",
                      "Obesity proteomics", "MASLD genomics", "糖尿病单细胞测序"):
            with self.subTest(title=title):
                self.assertEqual(assess_scope(self.paper(title))["tier"], "expansion")

    def test_both_research_topic_and_omics_required(self):
        for title in ("Diabetes clinical outcomes", "Aging photographic imaging",
                      "A general-purpose genomics plotting toolkit", "Cancer transcriptomics"):
            with self.subTest(title=title):
                self.assertFalse(matches_scope(self.paper(title)))

    def test_histone_biochemistry_alone_is_not_omics(self):
        record = self.paper("Histone methylation dynamics regulate cellular senescence",
                            "We used western blotting and microscopy to measure histone methylation.")
        self.assertFalse(matches_scope(record))
        self.assertEqual(assess_scope(record)["reason"], "omics_not_supported")
        for prefix in ("DNA", "CpG", "Genome-wide", "Whole-genome"):
            self.assertTrue(matches_scope(self.paper(prefix + " methylation dynamics during human aging")))

    def test_single_cell_imaging_and_protein_expression_alone_are_not_rna_profiling(self):
        record = self.paper("Single-cell spatial atlas of aging human tissue",
                            "Imaging mass cytometry profiles spatial expression of 40 proteins.")
        self.assertFalse(matches_scope(record))
        self.assertNotIn("single_cell_profile", assess_scope(record)["methodMatches"])

    def test_marine_plant_ecology_is_not_biomedical_aging(self):
        for title in ("Marine biodiversity imaging and DNA sequencing", "Plant aging transcriptomics",
                      "Coral longevity genomics"):
            with self.subTest(title=title):
                self.assertFalse(matches_scope(self.paper(title)))

    def test_ai_tags_journal_and_notes_cannot_manufacture_scope(self):
        record = self.paper("General visualization software")
        record.update(topic="aging multiomics", description="Diabetes genomics", journal="Aging",
                      ai={"summary_zh": "衰老三代测序"}, evidence="Aging nanopore sequencing")
        self.assertFalse(matches_scope(record))

    def test_title_and_abstract_combined_and_html_decoded(self):
        record = self.paper("Human diabetes", "<p>Single-cell RNA-seq &amp; transcriptomics.</p>")
        self.assertTrue(matches_scope(record))
        self.assertEqual(assess_scope(record)["tier"], "expansion")

    def test_omics_profile_descriptions_and_sequencing_abbreviations(self):
        for title, abstract in (
            ("Human muscle aging atlas", "We profiled single cells and single nuclei to identify genes in a tissue atlas."),
            ("Mammalian lifespan and methylation dynamics", "Methylation data across chromatin states."),
            ("Aging of human muscle", "scRNA-seq and snATAC-seq reveal molecular alterations."),
        ):
            with self.subTest(title=title):
                self.assertTrue(matches_scope(self.paper(title, abstract)))

    def test_current_abstract_does_not_erase_matching_verified_proof(self):
        record = self.paper("Human tissue signals", "Aging genomics")
        record["admittedAt"] = "2026-10-15T01:00:00Z"
        record["researchScope"] = assess_scope(record, source="verified-metadata")
        self.assertTrue(has_verified_scope(record))
        self.assertEqual(eligible_daily_dois({"papers": [record]}, [], "2026-10-15", POLICY), {record["doi"]})
        record["abstract"] = "Marine imaging"
        self.assertFalse(has_verified_scope(record))
        self.assertEqual(eligible_daily_dois({"papers": [record]}, [], "2026-10-15", POLICY), set())

    def test_primary_precedes_metabolic_expansion(self):
        self.assertGreater(scope_priority(self.paper("Aging genomics")),
                           scope_priority(self.paper("Diabetes multiomics")))

    def test_saved_decision_requires_verified_metadata_title_doi_binding(self):
        record = self.paper("Human diabetes", "Transcriptomics identifies candidate mechanisms")
        record["researchScope"] = assess_scope(record, source="verified-metadata")
        record.pop("abstract")
        self.assertTrue(matches_scope(record))
        record["doi"] = "10.1234/other"
        self.assertFalse(matches_scope(record))
        record["doi"] = "10.1234/fixture"
        record["researchScope"]["source"] = "ai"
        self.assertFalse(matches_scope(record))

    def test_unknown_policy_fails_closed(self):
        with self.assertRaises(ValueError):
            matches_scope(self.paper("Aging genomics"), "typo")
        self.assertTrue(matches_scope(self.paper("Unrelated marine images"), "unrestricted"))

    def test_queries_use_title_abstract_fields(self):
        self.assertIn("TITLE_ABS:", query_constraint())
        self.assertIn("[Title/Abstract]", query_constraint("ncbi"))
        self.assertNotIn("diabetes", query_constraint("epmc", "primary"))
        self.assertIn("diabetes", query_constraint("epmc", "expansion"))
        for term in ("T1D", "T2D", "prediabetes", '"metabolic disorder"', "MASLD"):
            self.assertIn(term, query_constraint("epmc", "expansion"))
            self.assertIn(term + "[Title/Abstract]", query_constraint("ncbi", "expansion"))


if __name__ == "__main__":
    unittest.main()
