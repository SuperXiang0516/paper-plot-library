import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from paper_plot_library.sources import discover, lookup_paper, normalize_doi


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


if __name__ == "__main__":
    unittest.main()
