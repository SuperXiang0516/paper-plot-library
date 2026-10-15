"""Offline eligibility regressions: scope is not a journal/image score."""
import unittest

from paper_plot_library.scope import POLICY, assess_scope, eligible_daily_dois, has_verified_scope, matches_scope, query_constraint, scope_priority, select_scope_candidates


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

    def test_topic_or_reusable_tool_and_omics_required(self):
        for title in ("Diabetes clinical outcomes", "Aging photographic imaging",
                      "A general-purpose plotting toolkit", "Cancer transcriptomics"):
            with self.subTest(title=title):
                self.assertFalse(matches_scope(self.paper(title)))

    def test_domain_specific_genomics_plotting_toolkit_is_eligible(self):
        assessment = assess_scope(self.paper("A general-purpose genomics plotting toolkit"))
        self.assertEqual((assessment["status"], assessment["tier"]), ("included", "tools"))

    def test_experimental_sequencing_development_is_methodology_not_only_software(self):
        record = self.paper("Quantifying DNA replication speeds by a new assay",
            "To measure replication, we developed single-cell nucleotide-label sequencing to detect nascent DNA.")
        self.assertEqual(assess_scope(record)["tier"], "tools")
        self.assertIn("method_tool", assess_scope(record)["methodMatches"])

    def test_using_sequencing_to_develop_a_disease_score_is_not_new_sequencing_method(self):
        record = self.paper("A cancer prognostic score",
            "We developed a risk score using RNA sequencing. Tumour markers predict survival.")
        self.assertFalse(matches_scope(record))

    def test_transferable_omics_tools_need_no_aging_or_diabetes_terms(self):
        # Synthetic metadata fixtures, not claims that these are verbatim titles
        # or abstracts. They exercise recognizable real tool names without an
        # allowlist: a brand name alone cannot grant research eligibility.
        examples = (
            ("ArchR is a scalable software package for single-cell chromatin accessibility analysis",
             "We introduce a software package for integrative analysis of single-cell ATAC-seq."),
            ("MultiVI: deep generative model for the integration of multi-omic data",
             "We present a model that integrates single-cell RNA-seq and chromatin accessibility."),
            ("CellRank for directed single-cell fate mapping",
             "We introduce a framework for estimating cell fates from RNA sequencing and RNA velocity."),
            ("SCENIC+: inference of enhancers and gene regulatory networks",
             "We develop a computational method for gene regulatory network inference from multiomic profiles."),
            ("scXMatch for unpaired single-cell matching",
             "We introduce a method for integration of single-cell multi-omics data."),
            ("DeCoRE reconstructs regulatory programs",
             "We present a computational framework for gene regulatory network reconstruction from transcriptomics."),
            ("A framework for nanopore transcript sequencing",
             "We present a method for accurate long-read isoform reconstruction."),
            ("A computational method for spatial transcriptomics integration", ""),
        )
        for title, abstract in examples:
            with self.subTest(title=title):
                assessment = assess_scope(self.paper(title, abstract))
                self.assertEqual((assessment["status"], assessment["tier"]), ("included", "tools"))
                self.assertIn("method_tool", assessment["methodMatches"])

    def test_brand_name_or_generic_method_mention_does_not_admit_discovery_papers(self):
        for title, abstract in (
            ("ArchR", ""),
            ("Cancer transcriptomics reveals resistance", "We used standard methods and software for analysis."),
            ("CellRank identifies cancer cell states", "RNA-seq reveals a disease-specific marker."),
            ("A visualization framework", "We introduce a general plotting toolkit with attractive figures."),
            ("A clinical diabetes prediction model", "We introduce a model using body mass index and blood glucose."),
        ):
            with self.subTest(title=title):
                self.assertFalse(matches_scope(self.paper(title, abstract)))

    def test_non_biomedical_tool_is_not_rescued_by_software_evidence(self):
        for title in ("Marine metagenomics software for biodiversity monitoring",
                      "Plant transcriptomics integration framework", "Coral genomics toolkit"):
            self.assertFalse(matches_scope(self.paper(title, "We introduce a framework for analysis.")))

    def test_named_contributions_and_method_benchmarks_across_sentences(self):
        for title, abstract in (
            ("CellRank for directed single-cell fate mapping.",
             "Computational trajectory inference reconstructs dynamics from single-cell RNA sequencing. "
             "Here, we present CellRank for fate mapping in diverse scenarios. CellRank predicts cell fates."),
            ("Quantifying distribution shifts in single-cell data with scXMatch.",
             "We present scXMatch. scXMatch is based on a non-parametric graph-based test. "
             "We evaluated scXMatch on gene expression and chromatin accessibility. Python source code is available."),
            ("Benchmarking atlas-level data integration in single-cell genomics.",
             "We benchmarked 68 method and preprocessing combinations on gene expression and chromatin accessibility. "
             "We evaluated methods using 14 metrics. Our Python module and benchmarking pipeline are freely available."),
        ):
            with self.subTest(title=title):
                self.assertEqual(assess_scope(self.paper(title, abstract))["tier"], "tools")

    def test_generic_integration_disease_discovery_is_not_a_method_contribution(self):
        for title, abstract in (
            ("Integration of cancer transcriptomics reveals resistance", "We analyzed patient cohorts using standard methods."),
            ("Inference of tumor evolution from single-cell RNA sequencing", "We report new cancer markers."),
            ("Reconstruction of the cancer genome", "Existing software identified rearrangements."),
            ("Marine biodiversity genomics toolkit", "We introduce a software framework, compared against human studies."),
            ("A software framework for marine biodiversity sequencing", "We present a pipeline for coral reefs; unlike human genomics this monitors marine biodiversity."),
            ("Plant senescence transcriptomics software", "We benchmarked methods and discuss human aging."),
        ):
            with self.subTest(title=title):
                self.assertFalse(matches_scope(self.paper(title, abstract)))

    def test_version_two_exclusions_are_also_eligible_for_metadata_recheck(self):
        record = self.paper("CellRank for directed single-cell fate mapping")
        old = assess_scope(record, source="verified-metadata")
        old["version"] = 2
        record["researchScope"] = old
        record.pop("abstract")
        self.assertFalse(has_verified_scope(record))

    def test_old_tool_and_marine_inclusions_do_not_bypass_stricter_contribution_checks(self):
        for title, abstract in (
            ("Integration of cancer transcriptomics", "We present a software package for analysis."),
            ("Marine longevity genomics toolkit", "We present a toolkit applied to human data as well."),
        ):
            record = self.paper(title, abstract)
            record["researchScope"] = dict(assess_scope(record, source="verified-metadata"), version=2)
            if "Marine" in title:
                record["researchScope"].update(status="included", reason="eligible", tier="primary")
            record.pop("abstract")
            self.assertFalse(has_verified_scope(record))

    def test_migration_rechecks_old_exclusions_but_keeps_old_verified_inclusions(self):
        record = self.paper("A general-purpose genomics plotting toolkit")
        old = assess_scope(record, source="verified-metadata")
        old.update(version=1, status="excluded", tier="none", reason="topic_not_supported",
                   methodMatches=[method for method in old["methodMatches"] if method != "method_tool"])
        record["researchScope"] = old
        record.pop("abstract")
        self.assertFalse(has_verified_scope(record), "v1 exclusions must trigger a fresh metadata review")
        self.assertEqual(assess_scope(record)["tier"], "tools")
        record = self.paper("Aging genomics")
        record["researchScope"] = dict(assess_scope(record, source="verified-metadata"), version=1)
        record.pop("abstract")
        self.assertTrue(has_verified_scope(record))
        self.assertTrue(matches_scope(record))

    def test_current_tool_decision_survives_abstract_redaction_and_is_bound(self):
        record = self.paper("DeCoRE", "We present a framework for transcriptomics analysis.")
        record["researchScope"] = assess_scope(record, source="verified-metadata")
        record.pop("abstract")
        self.assertTrue(has_verified_scope(record))
        self.assertEqual(assess_scope(record)["tier"], "tools")
        record["researchScope"]["methodMatches"].remove("method_tool")
        self.assertFalse(has_verified_scope(record))

    def test_bounded_batch_access_for_tools_without_displacing_core_priority(self):
        records = [dict(self.paper("Aging genomics"), doi="10.1234/core" + str(index)) for index in range(10)]
        records += [dict(self.paper("Diabetes genomics"), doi="10.1234/metabolic"),
                    dict(self.paper("A genomics toolkit"), doi="10.1234/tool")]
        chosen = select_scope_candidates(records, 5)
        self.assertEqual([assess_scope(p)["tier"] for p in chosen],
                         ["primary", "primary", "primary", "expansion", "tools"])
        self.assertEqual(len({p["doi"] for p in select_scope_candidates(records + records, 100)}), 12)
        self.assertEqual(select_scope_candidates(records, 0), [])
        self.assertEqual([assess_scope(p)["tier"] for p in select_scope_candidates(records, 2)], ["primary", "tools"])

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
        self.assertIn("TITLE_ABS:(toolkit", query_constraint("epmc", "tools"))
        self.assertNotIn("diabetes", query_constraint("epmc", "tools"))
        self.assertIn("toolkit[Title/Abstract]", query_constraint("ncbi"))


if __name__ == "__main__":
    unittest.main()
