import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from PIL import Image

from paper_plot_library.figures import CLOUD, cloud_url, crop_approved_box, inspect_article, reuse_allowed


def png():
    stream = io.BytesIO()
    Image.new("RGB", (128, 96), "white").save(stream, "PNG")
    return stream.getvalue()


XML = b'<article article-type="research-article" xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta><article-id pub-id-type="doi">10.1234/example</article-id><permissions><copyright-holder>Authors</copyright-holder></permissions></article-meta></front><body><fig id="F1"><label>Fig. 1</label><caption><title>Overview</title><p>a, Inputs; b, results; c, plots.</p></caption><graphic xlink:href="image-g001"/></fig><fig id="F2"><label>Fig. 2</label><caption><p>Reproduced from someone with permission.</p></caption><graphic xlink:href="image-g002"/></fig><supplementary-material id="S1"><label>Supplementary table</label><media xlink:href="table-s001.csv"/></supplementary-material><sec id="code"><title>Code availability</title><p>Our source code is available at <ext-link xlink:href="https://github.com/a/b">GitHub</ext-link>.</p></sec><ref-list><ref><ext-link xlink:href="https://github.com/other/tool">Cited tool</ext-link></ref></ref-list></body></article>'


class FakeClient:
    def __init__(self, license="CC BY", retracted=False, mismatch=False, badimage=False):
        self.license, self.retracted, self.mismatch, self.badimage = license, retracted, mismatch, badimage
        self.calls = []

    def get_text(self, url, **kwargs):
        self.calls.append(url)
        return '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Prefix>PMC123.</Prefix><CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes></ListBucketResult>'

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        version = 2 if ".2.json" in url else 1
        return {"version": version, "doi": "10.1234/other" if self.mismatch else "10.1234/example",
                "is_manuscript": version == 2, "is_retracted": self.retracted,
                "license_code": self.license, "xml_url": f"s3://pmc-oa-opendata/PMC123.{version}/PMC123.{version}.xml",
                "media_urls": [f"s3://pmc-oa-opendata/PMC123.{version}/image-g001.png",
                               f"s3://pmc-oa-opendata/PMC123.{version}/image-g002.png",
                               f"s3://pmc-oa-opendata/PMC123.{version}/table-s001.csv"]}

    def get_bytes(self, url, **kwargs):
        self.calls.append(url)
        if ".xml" in url:
            return XML
        if ".csv" in url:
            return b"sample,age\na,25\n"
        return b"<html>not image</html>" if self.badimage else png()


class FiguresTests(unittest.TestCase):
    def test_cloud_strict_url_and_licenses(self):
        self.assertEqual(cloud_url("s3://pmc-oa-opendata/PMC123.1/a.jpg?md5=abc"), CLOUD + "/PMC123.1/a.jpg?md5=abc")
        self.assertEqual(cloud_url("https://evil.example/a.jpg"), "")
        for license in ["CC BY-NC-ND", "CC BY-SA", "TDM", "unknown"]:
            self.assertFalse(reuse_allowed(license))
        self.assertFalse(reuse_allowed("CC BY", "Adapted from prior figure"))
        self.assertTrue(reuse_allowed("CC0"))

    def test_final_version_exact_graphic_and_private_si(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory) / "site" / "assets"
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123", "paper": "Example"}, assets)
            self.assertEqual(result["status"], "inspected")
            self.assertEqual(result["version"], "1")
            self.assertTrue(result["figures"][0]["asset"].startswith("assets/"))
            self.assertEqual(result["figures"][1]["reuseStatus"], "link_only")
            self.assertFalse(any("g002.png" in url for url in client.calls))
            self.assertEqual(result["supplements"][0]["status"], "downloaded_private")
            self.assertEqual(result["codeURLs"], ["https://github.com/a/b"])
            self.assertEqual(result["codeURLEvidence"][0]["scope"], "author_code_availability")
            self.assertEqual(len(list(assets.glob("*.png"))), 1)

    def test_nc_nd_never_image_or_si_download(self):
        client = FakeClient(license="CC BY-NC-ND")
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123"}, Path(directory) / "site" / "assets")
        self.assertEqual(result["figures"][0]["reuseStatus"], "link_only")
        self.assertFalse(any(url.endswith(".png") or url.endswith(".csv") for url in client.calls))

    def test_retracted_or_doi_mismatch_no_article_fetch(self):
        for client in [FakeClient(retracted=True), FakeClient(mismatch=True)]:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123"}, "/unused")
            self.assertIn(result["status"], {"retracted", "no_final_version"})
            self.assertFalse(any(url.endswith(".xml") for url in client.calls))

    def test_bad_image_failure_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(FakeClient(badimage=True), {"doi": "10.1234/example", "pmcid": "PMC123"}, Path(directory) / "site" / "assets", include_supplements=False)
        self.assertEqual(result["status"], "inspected")
        self.assertEqual(result["figures"][0]["downloadStatus"], "failed")
        self.assertEqual(result["figures"][0]["asset"], "")
        self.assertEqual(result["supplements"][0]["status"], "not_requested")

    def test_requested_figure_never_substituted(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123", "figure": "Figure 9"}, Path(directory) / "data" / "assets", False)
        self.assertFalse(any(f["asset"] for f in result["figures"]))
        self.assertFalse(any(url.endswith(".png") for url in client.calls))
        self.assertTrue(any("no substitute" in note for note in result["limitations"]))

    def test_missing_label_cannot_prove_requested_number(self):
        class NoLabelClient(FakeClient):
            def get_bytes(self, url, **kwargs):
                raw = super().get_bytes(url, **kwargs)
                return raw.replace(b"<label>Fig. 1</label>", b"") if url.endswith(".xml") else raw
        client = NoLabelClient()
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123", "figure": "Figure 1c"}, Path(directory) / "data" / "assets", False)
        self.assertIsNone(result["figures"][0]["reference"])
        self.assertEqual(result["figures"][0]["labelStatus"], "unresolved")
        self.assertFalse(any(f["asset"] for f in result["figures"]))
        self.assertFalse(any(url.endswith(".png") for url in client.calls))

    def test_subpanel_selects_whole_parent_not_other_figure(self):
        class MoreFiguresClient(FakeClient):
            def get_json(self, url, **kwargs):
                value = super().get_json(url, **kwargs)
                value["media_urls"].append(value["media_urls"][0].replace("g001", "g006"))
                return value
            def get_bytes(self, url, **kwargs):
                raw = super().get_bytes(url, **kwargs)
                if url.endswith(".xml"):
                    return raw.replace(b"</body>", b'<fig id="F6"><label>Fig. 6</label><caption><p>a, b, c, d, e, f, g, h, compound figure.</p></caption><graphic xlink:href="image-g006"/></fig></body>')
                return raw
        client = MoreFiguresClient()
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123", "figure": "Figure 1c（CD4 T-cell子面板）"}, Path(directory) / "data" / "assets", False)
        downloaded = [f for f in result["figures"] if f["asset"]]
        self.assertEqual([f["label"] for f in downloaded], ["Fig. 1"])
        self.assertEqual(downloaded[0]["requestedReference"]["panel"], "c")
        self.assertIn("not cropped", downloaded[0]["selectionNote"])
        self.assertFalse(any("g006.png" in url for url in client.calls))

    def test_extended_and_supplementary_not_main_number(self):
        from paper_plot_library.figures import figure_reference
        self.assertEqual(figure_reference("Extended Data Fig. 6")["scope"], "extended")
        self.assertEqual(figure_reference("Supplementary Fig. S1")["scope"], "supplementary")
        self.assertEqual(figure_reference("Figure 1c（子面板）"), {"scope": "main", "number": "1", "panel": "c"})
        self.assertEqual(figure_reference("Figure 1 comparison")["panel"], "")
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123", "figure": "Extended Data Fig. 1"}, Path(directory) / "data" / "assets", False)
        self.assertFalse(any(f["asset"] for f in result["figures"]))
        self.assertFalse(any(url.endswith(".png") for url in client.calls))

    def test_code_url_period_and_duplicate_declarations(self):
        class PunctuationClient(FakeClient):
            def get_bytes(self, url, **kwargs):
                raw = super().get_bytes(url, **kwargs)
                if url.endswith(".xml"):
                    return raw.replace(b'Our source code is available at <ext-link xlink:href="https://github.com/a/b">GitHub</ext-link>.', b'Our source code is available at https://github.com/a/PRC2-AgeIndex. <ext-link xlink:href="https://github.com/a/PRC2-AgeIndex">GitHub</ext-link>.')
                return raw
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(PunctuationClient(), {"doi": "10.1234/example", "pmcid": "PMC123"}, Path(directory) / "data" / "assets", False)
        self.assertEqual(result["codeURLs"], ["https://github.com/a/PRC2-AgeIndex"])
        self.assertEqual(len(result["codeURLEvidence"]), 1)

    def test_code_section_third_party_tool_not_author_repository(self):
        from paper_plot_library.figures import _declared_repositories
        import xml.etree.ElementTree as ET
        mixed = '<p xmlns:xlink="http://www.w3.org/1999/xlink">The package used in this study is available at <ext-link xlink:href="https://github.com/tool/external">GitHub</ext-link>. Our analysis code is available at <ext-link xlink:href="https://github.com/author/paper">GitHub</ext-link>.</p>'
        urls = [url for url, _ in _declared_repositories(ET.fromstring(mixed), "Code availability")]
        self.assertEqual(urls, ["https://github.com/author/paper"])

    def test_ambiguous_two_repo_statement_remains_unconfirmed(self):
        from paper_plot_library.figures import _declared_repositories
        import xml.etree.ElementTree as ET
        node = ET.fromstring('<p>Our code is available at https://github.com/author/paper using the package https://github.com/tool/library.</p>')
        self.assertEqual(_declared_repositories(node, "Code availability"), [])

    def test_freely_accessible_scripts_author_declaration(self):
        from paper_plot_library.figures import _declared_repositories
        import xml.etree.ElementTree as ET
        node = ET.fromstring('<p xmlns:xlink="http://www.w3.org/1999/xlink">Our scripts are freely accessible at <ext-link xlink:href="https://github.com/author/paper">GitHub</ext-link>.</p>')
        self.assertEqual([url for url, _ in _declared_repositories(node)], ["https://github.com/author/paper"])

    def test_license_source_url_retained_exact_version(self):
        from paper_plot_library.figures import _license_url
        self.assertEqual(_license_url("http://creativecommons.org/licenses/by/4.0/"), "https://creativecommons.org/licenses/by/4.0/")
        self.assertEqual(_license_url("CC BY"), "")

    def test_actual_author_names_and_collaboration_credited_with_bound(self):
        class AuthorClient(FakeClient):
            def get_bytes(self, url, **kwargs):
                raw = super().get_bytes(url, **kwargs)
                if url.endswith(".xml"):
                    names = b'<contrib-group><contrib contrib-type="author"><name><surname>Moqri</surname><given-names>Mahdi</given-names></name></contrib><contrib contrib-type="author"><collab>Example Consortium</collab></contrib>'
                    names += b''.join(f'<contrib contrib-type="author"><name><surname>Author{n}</surname><given-names>Given{n}</given-names></name></contrib>'.encode() for n in range(1, 7))
                    names += b'<contrib contrib-type="editor"><name><surname>NotAnAuthor</surname></name></contrib></contrib-group>'
                    return raw.replace(b'<permissions>', names + b'<permissions>')
                return raw
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(AuthorClient(), {"doi": "10.1234/example", "pmcid": "PMC123", "paper": "Source title"}, Path(directory) / "data" / "assets", False)
        credit = result["figures"][0]["attribution"]
        for expected in ["Mahdi Moqri", "Example Consortium", "et al.", "Source title", "10.1234/example", "Copyright holder: Authors"]:
            self.assertIn(expected, credit)
        self.assertNotIn("NotAnAuthor", credit)
        self.assertNotIn("Given6 Author6", credit)

    def test_forged_or_unsafe_zip_rejected_without_extracting(self):
        from paper_plot_library.figures import _supplement_format
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            _supplement_format(b"PK\x03\x04fake", "https://example.org/supp.zip")
        for name, content in [("../escape.txt", b"tiny"), ("large.txt", b"A" * 100000)]:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(name, content)
            with self.assertRaises(ValueError):
                _supplement_format(stream.getvalue(), "https://example.org/supp.zip")

    def test_malformed_pdf_never_written_as_verified(self):
        class PdfClient(FakeClient):
            def get_json(self, url, **kwargs):
                value = super().get_json(url, **kwargs)
                value["media_urls"] = [u.replace("s001.csv", "s001.pdf") for u in value["media_urls"]]
                return value
            def get_bytes(self, url, **kwargs):
                if url.endswith(".pdf"):
                    self.calls.append(url)
                    return b"%PDF-1.7\nThis is not a PDF object graph\n%%EOF"
                raw = super().get_bytes(url, **kwargs)
                return raw.replace(b"s001.csv", b"s001.pdf") if url.endswith(".xml") else raw
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = inspect_article(PdfClient(), {"doi": "10.1234/example", "pmcid": "PMC123"}, root / "data" / "assets")
            self.assertEqual(result["supplements"][0]["status"], "failed")
            self.assertFalse(list((root / ".ppl" / "downloads").glob("**/*.pdf")))

    def test_configured_supplement_zero_and_file_bound_honored(self):
        class BoundedClient(FakeClient):
            def get_bytes(self, url, **kwargs):
                if url.endswith(".png"):
                    self.image_limit = kwargs["max_bytes"]
                return super().get_bytes(url, **kwargs)
        client = BoundedClient()
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_article(client, {"doi": "10.1234/example", "pmcid": "PMC123", "maxSupplementDownloads": 0, "maxFileBytes": 2048}, Path(directory) / "data" / "assets")
        self.assertEqual(client.image_limit, 2048)
        self.assertFalse(any(url.endswith(".csv") for url in client.calls))
        self.assertEqual(result["supplements"][0]["status"], "deferred_count_limit")

    def test_md5_mismatch_blocked(self):
        from paper_plot_library.figures import _verify_digest
        with self.assertRaises(ValueError):
            _verify_digest(b"wrong", "https://example.org/x?md5=" + hashlib.md5(b"right").hexdigest())

    def test_crop_needs_actual_approval_and_valid_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.png", Path(directory) / "crop.png"
            source.write_bytes(png())
            with self.assertRaises(ValueError):
                crop_approved_box(source, output, [0, 0, 64, 64], {}, "CC BY")
            approved = {"approved": True, "reviewer": "fixture reviewer", "reason": "labels retained"}
            result = crop_approved_box(source, output, [0, 0, 64, 64], approved, "CC BY")
            self.assertEqual(result["width"], 64)
            with self.assertRaises(ValueError):
                crop_approved_box(source, output, [0, 0, 1000, 1000], approved, "CC BY")


if __name__ == "__main__":
    unittest.main()
