import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from paper_plot_library.sources import discover, lookup_paper, lookup_scope_metadata, normalize_doi
from paper_plot_library.scope import POLICY


class FakeClient:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or [], fail, []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "europepmc" in url:
            if self.fail:
                raise OSError("offline")
            return {"resultList": {"result": self.rows}}
        if "esearch" in url:
            return {"esearchresult": {"idlist": ["123"]}}
        if "crossref" in url:
            return {"message": {"DOI": "10.1234/example", "title": ["Example"], "type": "journal-article",
                                "container-title": ["Journal"], "published": {"date-parts": [[2025, 1, 2]]}}}
        raise AssertionError(url)

    def get_text(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return '<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>Example</ArticleTitle><Journal><Title>Journal</Title><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal><Abstract><AbstractText>Code https://github.com/a/b</AbstractText></Abstract><PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/example</ArticleId><ArticleId IdType="pmc">PMC123</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'


def row(doi="10.1234/example", **kwargs):
    return {"doi": doi, "title": "Example", "pubYear": "2025", "source": "MED", "id": "123",
            "pmcid": "PMC123", "abstractText": "<p>Code: https://github.com/a/b</p>",
            "journalInfo": {"journal": {"title": "Journal", "issn": "1234-5678"}}, **kwargs}


class SourcesTests(unittest.TestCase):
    def test_normalization_rejects_non_doi(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1234/ABC"), "10.1234/abc")
        self.assertEqual(normalize_doi("file:///tmp/a"), "")

    def test_unique_formal_records_only(self):
        client = FakeClient([row(), row("10.1234/EXAMPLE"), row("10.1234/pre", source="PPR"),
                             row("10.1234/retract", pubTypeList={"pubType": ["Retracted Publication"]}),
                             row("10.1234/review", pubTypeList={"pubType": ["Review"]})])
        records = discover(client, SimpleNamespace(discovery_limit=5))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["codeURLs"], ["https://github.com/a/b"])
        self.assertNotIn("<p>", records[0]["abstract"])

    def test_exact_doi_not_first_result(self):
        record = lookup_paper(FakeClient([row("10.1234/other"), row()]), "10.1234/example")
        self.assertEqual(record["doi"], "10.1234/example")
        self.assertEqual(record["status"], "matched")

    def test_ncbi_metadata_fallback_bounded(self):
        client = FakeClient(fail=True)
        records = discover(client, SimpleNamespace(discovery_limit=3, ncbi_api_key="redacted-key", ncbi_email=""))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["metadataSource"], "NCBI PubMed")
        self.assertEqual(next(kwargs for url, kwargs in client.calls if "esearch" in url)["params"]["retmax"], 3)

    def test_crossref_fallback(self):
        record = lookup_paper(FakeClient(), "10.1234/example")
        self.assertEqual(record["date"], "2025-01-02")
        self.assertEqual(record["pmcid"], "")

    def test_future_ceiling_applied_before_request_and_rows_defensively_filtered(self):
        today = datetime.now(timezone.utc).date()
        older = today - timedelta(days=2)
        future = today + timedelta(days=35)
        client = FakeClient([row("10.1234/future", firstPublicationDate=future.isoformat(), pubYear=str(future.year)),
                             row("10.1234/eligible", firstPublicationDate=older.isoformat(), pubYear=str(older.year))])
        records = discover(client, SimpleNamespace(discovery_limit=10, discovery_query="aging AND HAS_FT:Y"))
        self.assertEqual([record["doi"] for record in records], ["10.1234/eligible"])
        query = client.calls[0][1]["params"]["query"]
        self.assertIn("FIRST_PDATE:[1900-01-01 TO " + today.isoformat() + "]", query)
        self.assertIn("aging AND HAS_FT:Y", query)

    def test_ncbi_fallback_has_same_current_date_ceiling(self):
        today = datetime.now(timezone.utc).date()
        client = FakeClient(fail=True)
        discover(client, SimpleNamespace(discovery_limit=3))
        term = next(kwargs for url, kwargs in client.calls if "esearch" in url)["params"]["term"]
        self.assertIn(today.strftime("%Y/%m/%d"), term)
        self.assertIn("[Date - Publication]", term)

    def test_formal_doi_eligibility_in_request_not_only_local_filter(self):
        client = FakeClient([row("", source="PMC"), row("10.1234/review", pubTypeList={"pubType": ["Review"]}),
                             row("10.1234/formal")])
        records = discover(client, SimpleNamespace(discovery_limit=10, discovery_query="bioinformatics AND HAS_FT:Y"))
        self.assertEqual([record["doi"] for record in records], ["10.1234/formal"])
        query = client.calls[0][1]["params"]["query"]
        self.assertIn("HAS_DOI:Y", query)
        self.assertIn("SRC:MED", query)
        self.assertIn('NOT PUB_TYPE:"Review"', query)
        self.assertIn("bioinformatics AND HAS_FT:Y", query)

    def test_pages_past_known_or_cooling_first_page_without_following_provider_url(self):
        class Paged(FakeClient):
            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                cursor = kwargs["params"]["cursorMark"]
                if cursor == "*":
                    return {"resultList": {"result": [row("10.1234/old")]},
                            "nextCursorMark": "opaque-next", "nextPageUrl": "https://private.example/never-follow"}
                return {"resultList": {"result": [row("10.1234/new")]}, "nextCursorMark": "opaque-next"}
        client = Paged()
        result = discover(client, SimpleNamespace(discovery_limit=1), exclude_dois={"10.1234/old"})
        self.assertEqual([record["doi"] for record in result], ["10.1234/new"])
        self.assertEqual([kwargs["params"]["cursorMark"] for _, kwargs in client.calls], ["*", "opaque-next"])
        self.assertTrue(all(url.startswith("https://www.ebi.ac.uk/") for url, _ in client.calls))

    def test_metadata_page_limit_bounds_all_known_results(self):
        class Many(FakeClient):
            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return {"resultList": {"result": [row("10.1234/old")]}, "nextCursorMark": str(len(self.calls))}
        client = Many()
        self.assertEqual(discover(client, SimpleNamespace(discovery_limit=1), {"10.1234/old"}), [])
        self.assertEqual(len(client.calls), 5)

    def test_repeated_or_invalid_cursor_stops_without_network_loop(self):
        for cursor in ("*", [], "secret\nvalue", "a" * 8193):
            class Invalid(FakeClient):
                def get_json(self, url, **kwargs):
                    self.calls.append((url, kwargs))
                    return {"resultList": {"result": [row()]}, "nextCursorMark": cursor}
            client = Invalid()
            discover(client, SimpleNamespace(discovery_limit=2))
            self.assertEqual(len(client.calls), 1)

    def test_scoped_discovery_rejects_marine_and_imaging_false_positive(self):
        client = FakeClient([row("10.1234/marine", title="Marine biodiversity imaging with DNA sequencing"),
                             row("10.1234/imaging", title="Imaging genomics in healthy human tissues")])
        records = discover(client, SimpleNamespace(discovery_limit=5, research_scope=POLICY))
        self.assertEqual(records, [])
        self.assertEqual(len(client.calls), 3)

    def test_scoped_discovery_aging_precedes_newer_metabolic_work(self):
        client = FakeClient([row("10.1234/diabetes", title="Diabetes multiomics in patients", pubYear="2025"),
                             row("10.1234/aging", title="Aging nanopore sequencing in mice", pubYear="2020")])
        records = discover(client, SimpleNamespace(discovery_limit=2, research_scope=POLICY))
        self.assertEqual([record["doi"] for record in records], ["10.1234/aging", "10.1234/diabetes"])
        queries = [kwargs["params"]["query"] for _, kwargs in client.calls]
        self.assertIn("TITLE_ABS:(aging", queries[0])
        self.assertNotIn("TITLE_ABS:(diabetes", queries[0])
        self.assertIn("TITLE_ABS:(diabetes", queries[1])

    def test_scoped_expansion_when_primary_lane_empty(self):
        class Expansion(FakeClient):
            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                rows = [row("10.1234/diabetes", title="Single-cell RNA-seq in type 2 diabetes patients")]
                return {"resultList": {"result": rows if "TITLE_ABS:(diabetes" in kwargs["params"]["query"] else []}}
        client = Expansion()
        records = discover(client, SimpleNamespace(discovery_limit=2, research_scope=POLICY))
        self.assertEqual([record["doi"] for record in records], ["10.1234/diabetes"])
        self.assertEqual(len(client.calls), 3)

    def test_scoped_custom_query_cannot_remove_title_abstract_gate(self):
        client = FakeClient([row("10.1234/marine", title="Marine biodiversity DNA sequencing")])
        discover(client, SimpleNamespace(discovery_limit=2, discovery_query="marine OR all", research_scope=POLICY))
        for url, kwargs in client.calls:
            query = kwargs["params"]["query"]
            self.assertEqual(url, "https://www.ebi.ac.uk/europepmc/webservices/rest/search")
            self.assertIn("(marine OR all)", query)
            self.assertIn(") AND (TITLE_ABS:(", query)
            self.assertIn(") AND TITLE_ABS:(sequencing", query)

    def test_scoped_known_cooling_metadata_pages_share_five_page_ceiling(self):
        class Cooling(FakeClient):
            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                expansion = "TITLE_ABS:(diabetes" in kwargs["params"]["query"]
                title = "Diabetes genomics in patients" if expansion else "Aging long-read sequencing in mice"
                return {"resultList": {"result": [row("10.1234/known", title=title)]},
                        "nextCursorMark": "opaque-" + str(len(self.calls)),
                        "nextPageUrl": "https://untrusted.example/do-not-follow"}
        client = Cooling()
        records = discover(client, SimpleNamespace(discovery_limit=1, research_scope=POLICY), {"10.1234/known"})
        self.assertEqual(records, [])
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(["TITLE_ABS:(diabetes" in kwargs["params"]["query"] for _, kwargs in client.calls],
                         [False, False, True, False, False])
        self.assertEqual([kwargs["params"]["cursorMark"] for _, kwargs in client.calls],
                         ["*", "opaque-1", "*", "*", "opaque-4"])

    def test_tool_lane_is_queried_even_when_primary_page_is_full(self):
        class ThreeLanes(FakeClient):
            def get_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                query = kwargs["params"]["query"]
                if "TITLE_ABS:(toolkit" in query:
                    rows = [row("10.1234/tool", title="A genomics toolkit")]
                elif "TITLE_ABS:(diabetes" in query:
                    rows = [row("10.1234/diabetes", title="Diabetes genomics")]
                else:
                    rows = [row("10.1234/core" + str(number), title="Aging genomics") for number in range(5)]
                return {"resultList": {"result": rows}}
        client = ThreeLanes()
        records = discover(client, SimpleNamespace(discovery_limit=5, research_scope=POLICY))
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([record["doi"] for record in records],
                         ["10.1234/core0", "10.1234/core1", "10.1234/core2", "10.1234/diabetes", "10.1234/tool"])

    def test_ncbi_fallback_accepts_omics_tools_not_unrelated_plotting(self):
        for title, abstract, expected in (
            ("A genomics toolkit", "Software for genomics analysis.", ["10.1234/example"]),
            ("CellRank", "We introduce a framework for single-cell RNA-seq fate mapping.", ["10.1234/example"]),
            ("A general plotting toolkit", "We introduce visualization software.", []),
            ("Marine metagenomics software", "We introduce a toolkit for biodiversity.", []),
        ):
            class NCBI(FakeClient):
                def get_text(self, url, **kwargs):
                    return super().get_text(url, **kwargs).replace("<ArticleTitle>Example</ArticleTitle>",
                        "<ArticleTitle>" + title + "</ArticleTitle>").replace(
                        "<AbstractText>Code https://github.com/a/b</AbstractText>",
                        "<AbstractText>" + abstract + "</AbstractText>")
            records = discover(NCBI(fail=True), SimpleNamespace(discovery_limit=5, research_scope=POLICY))
            self.assertEqual([record["doi"] for record in records], expected)

    def test_scoped_ncbi_fallback_never_loosens_domain(self):
        for title, expected in (("Marine biodiversity imaging with DNA sequencing", []),
                                ("Diabetes transcriptomics in patients", ["10.1234/example"])):
            class NCBI(FakeClient):
                def get_text(self, url, **kwargs):
                    return super().get_text(url, **kwargs).replace("<ArticleTitle>Example</ArticleTitle>",
                                                                 "<ArticleTitle>" + title + "</ArticleTitle>")
            client = NCBI(fail=True)
            settings = SimpleNamespace(discovery_limit=3, research_scope=POLICY, ncbi_query="marine OR all[Filter]")
            records = discover(client, settings)
            self.assertEqual([record["doi"] for record in records], expected)
            term = next(kwargs for url, kwargs in client.calls if "esearch" in url)["params"]["term"]
            self.assertIn("(marine OR all[Filter]) AND (", term)
        self.assertIn(") AND (sequencing[Title/Abstract]", term)

    def test_unknown_research_policy_fails_before_any_network_call(self):
        client = FakeClient([row()])
        with self.assertRaises(ValueError):
            discover(client, SimpleNamespace(discovery_limit=5, research_scope="unknown-policy"))
        self.assertEqual(client.calls, [])

    def test_scope_migration_one_bounded_doi_only_metadata_request(self):
        client = FakeClient([row("10.1234/fixture-0", title="Canonical publication title")])
        papers = [{"doi": "https://doi.org/10.1234/FIXTURE-0"}, {"doi": "10.1234/fixture-0"}]
        papers.extend({"doi": "10.1234/fixture-" + str(number)} for number in range(1, 70))
        records = lookup_scope_metadata(client, papers)
        self.assertEqual(set(records), {"10.1234/fixture-0"})
        self.assertEqual(records["10.1234/fixture-0"]["paper"], "Canonical publication title")
        self.assertEqual(records["10.1234/fixture-0"]["status"], "matched")
        self.assertEqual(len(client.calls), 1)
        url, kwargs = client.calls[0]
        self.assertEqual(url, "https://www.ebi.ac.uk/europepmc/webservices/rest/search")
        self.assertEqual(kwargs["params"]["pageSize"], 50)
        self.assertEqual(kwargs["params"]["resultType"], "core")
        self.assertEqual(kwargs["max_bytes"], 4000000)
        query = kwargs["params"]["query"]
        self.assertEqual(query.count('DOI:"'), 50)
        self.assertNotIn('fixture-50"', query)

    def test_scope_migration_rejects_query_syntax_not_just_normalizes_doi(self):
        client = FakeClient([row()])
        records = lookup_scope_metadata(client, [{"doi": '10.1234/x"OR"y'}, {"doi": "10.1234/x\\y"},
                                                  {"doi": "10.1234/x\x00y"}, {"doi": "10.1234/x\x7fy"},
                                                  {"doi": "10.1234/" + "x" * 600}, {"doi": "not-a-doi"}, None,
                                                  {"doi": "DOI:10.1234/EXAMPLE"}])
        self.assertEqual(set(records), {"10.1234/example"})
        self.assertEqual(client.calls[0][1]["params"]["query"], 'DOI:"10.1234/example"')

    def test_scope_migration_malformed_unmatched_preprints_and_future_are_not_results(self):
        future = datetime.now(timezone.utc).date() + timedelta(days=30)
        client = FakeClient([None, row("10.1234/unrequested"), row("10.1234/malformed", title=[]),
                             row("10.1234/preprint", source="PPR"),
                             row("10.1234/future", firstPublicationDate=future.isoformat()),
                             row("10.1234/review", pubTypeList={"pubType": ["Review"]}),
                             row("10.1234/formal", source="PPR"), row("10.1234/formal", source="MED")])
        records = lookup_scope_metadata(client, [{"doi": "10.1234/" + suffix} for suffix in
            ("malformed", "preprint", "future", "review", "formal", "missing")])
        self.assertEqual(set(records), {"10.1234/formal"})
        self.assertEqual(records["10.1234/formal"]["status"], "matched")
        self.assertEqual(len(client.calls), 1)

    def test_scope_migration_conflicting_formal_duplicates_fail_closed(self):
        client = FakeClient([row("10.1234/title", title="Aging genomics"),
                             row("10.1234/title", title="Marine genomics"),
                             row("10.1234/title", title="Aging genomics"),
                             row("10.1234/abstract", abstractText="Aging sequencing in mice"),
                             row("10.1234/abstract", abstractText="Diabetes sequencing in patients"),
                             row("10.1234/same", pmcid=""), row("10.1234/same", pmcid="PMC456")])
        records = lookup_scope_metadata(client, [{"doi": "10.1234/" + suffix} for suffix in ("title", "abstract", "same")])
        self.assertEqual(set(records), {"10.1234/same"})
        self.assertEqual(records["10.1234/same"]["pmcid"], "PMC456")

    def test_scope_migration_empty_or_invalid_inputs_do_not_request(self):
        client = FakeClient()
        self.assertEqual(lookup_scope_metadata(client, []), {})
        self.assertEqual(lookup_scope_metadata(client, [{"doi": "bad"}]), {})
        self.assertEqual(client.calls, [])

    def test_scope_migration_bad_response_shape_is_not_resolved(self):
        for response in (None, [], {"resultList": []}, {"resultList": {"result": {}}}):
            class Malformed(FakeClient):
                def get_json(self, url, **kwargs):
                    self.calls.append((url, kwargs))
                    return response
            client = Malformed()
            self.assertEqual(lookup_scope_metadata(client, [{"doi": "10.1234/example"}]), {})
            self.assertEqual(len(client.calls), 1)

    def test_scope_migration_transport_error_has_no_per_paper_fallback(self):
        client = FakeClient(fail=True)
        with self.assertRaises(OSError):
            lookup_scope_metadata(client, [{"doi": "10.1234/example"}])
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
