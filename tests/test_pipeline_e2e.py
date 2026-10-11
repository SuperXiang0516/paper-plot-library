"""Offline integration: actual HTTPS transport/adapters/pipeline/site, synthetic IO.

Every generated fixture and fetched byte remains in TemporaryDirectory. This is
not publication evidence or a network test, and never runs scientific code.
"""
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

from PIL import Image

from paper_plot_library.config import Settings
from paper_plot_library.http import HttpClient
from paper_plot_library.pipeline import run_pipeline
from paper_plot_library.storage import read_json, write_json

PROJECT = Path(__file__).resolve().parents[1]
COMMIT = "b" * 40
SECRETS = ["fixture-deepseek-secret-DO-NOT-PUBLISH", "fixture-easyscholar-secret-DO-NOT-PUBLISH",
           "fixture-github-token-DO-NOT-PUBLISH", "fixture-ncbi-key-DO-NOT-PUBLISH"]
SCRIPT = b'raise RuntimeError("AUTHOR_CODE_EXECUTION_FORBIDDEN")\nimport pandas as pd\nimport matplotlib.pyplot as plt\ndf = pd.read_csv("../inputs/plot.csv")\nplt.scatter(df["age"], df["clock"])\nplt.savefig("Figure_1.png")\n'
CSV = b"sample,age,clock\nfixture-private-row,50,52\n"


def _json(value):
    return json.dumps(value, ensure_ascii=False).encode()


def _png():
    stream = io.BytesIO()
    Image.new("RGB", (160, 120), "white").save(stream, "PNG")
    return stream.getvalue()


def _zip():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("README.txt", "Synthetic fixture SI; no executable content.")
    return stream.getvalue()


def _xml(number):
    return (f'<article article-type="research-article" xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<front><article-meta><article-id pub-id-type="doi">10.9999/ppl-fixture-{number}</article-id>'
        '<permissions><copyright-holder>Synthetic fixture authors</copyright-holder>'
        '<license xlink:href="https://creativecommons.org/licenses/by/4.0/"><license-p>CC BY 4.0</license-p></license>'
        '</permissions></article-meta></front><body><fig id="F1"><label>Figure 1</label><caption>'
        '<title>Synthetic fixture multi-panel diagram</title><p>a, inputs; b, predictions; c, association.</p>'
        '</caption><graphic xlink:href="case-g001"/></fig><sec id="code-availability"><title>Code availability</title>'
        f'<p>Our source code is available at <ext-link xlink:href="https://github.com/fixture-owner/ppl-case-{number}">GitHub</ext-link>.</p>'
        '</sec><supplementary-material id="S1"><label>Supplementary synthetic schema table</label>'
        '<media xlink:href="case-s001.csv"/></supplementary-material>'
        '<supplementary-material id="S2"><label>Supplementary synthetic archive</label>'
        '<media xlink:href="case-s002.zip"/></supplementary-material></body></article>').encode()


class FixtureResponse:
    def __init__(self, url, raw):
        self.url, self.raw = url, io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.raw.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.close()


class OfficialApiFixture:
    """Fake opener, not fake adapters: routes the exact official URL contracts."""
    def __init__(self):
        self.events = []
        self.image = _png()
        self.supplement_archive = _zip()

    def media_url(self, number, name, raw):
        return f"s3://pmc-oa-opendata/PMC9900{number}.1/{name}?md5={hashlib.md5(raw).hexdigest()}"

    def publication(self, number):
        return {"doi": f"10.9999/ppl-fixture-{number}", "title": f"Synthetic aging multi-omics fixture {number}",
                "pubYear": "2025", "firstPublicationDate": "2025-01-01", "source": "MED",
                "id": str(99000 + number), "pmcid": f"PMC9900{number}",
                "journalInfo": {"journal": {"title": "Synthetic Journal", "issn": "9999-9999"}},
                "pubTypeList": {"pubType": ["Journal Article"]},
                "abstractText": "Synthetic static-inspection integration test, not a real publication."}

    def open(self, request, timeout=25):
        url = request.full_url
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        self.events.append({"host": parsed.hostname, "path": parsed.path, "method": request.get_method()})
        if parsed.hostname == "www.ebi.ac.uk":
            query = params["query"][0]
            match = re.search(r'ppl-fixture-(\d+)', query)
            numbers = [int(match.group(1))] if query.startswith("DOI:") and match else range(1, 7)
            raw = _json({"resultList": {"result": [self.publication(n) for n in numbers]}})
        elif parsed.hostname == "pmc-oa-opendata.s3.amazonaws.com":
            prefix = params.get("prefix", [parsed.path])[0]
            number = int(re.search(r"PMC9900(\d)", prefix).group(1))
            if parsed.path == "/":
                raw = f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><CommonPrefixes><Prefix>PMC9900{number}.1/</Prefix></CommonPrefixes></ListBucketResult>'.encode()
            elif parsed.path.endswith(".json"):
                raw = _json({"doi": f"10.9999/ppl-fixture-{number}", "version": 1, "is_manuscript": False,
                    "is_retracted": False, "license_code": "CC BY", "is_pmc_openaccess": True,
                    "xml_url": self.media_url(number, f"PMC9900{number}.1.xml", _xml(number)),
                    "media_urls": [self.media_url(number, "case-g001.png", self.image),
                                   self.media_url(number, "case-s001.csv", CSV),
                                   self.media_url(number, "case-s002.zip", self.supplement_archive)]})
            elif parsed.path.endswith(".xml"):
                raw = _xml(number)
            elif parsed.path.endswith(".png"):
                raw = self.image
            elif parsed.path.endswith(".csv"):
                raw = CSV
            elif parsed.path.endswith(".zip"):
                raw = self.supplement_archive
            else:
                raise AssertionError("Unexpected PMC fixture path: " + parsed.path)
        elif parsed.hostname == "api.github.com":
            self.assert_token(request)
            if "/commits/" in parsed.path:
                raw = _json({"sha": COMMIT})
            elif "/git/trees/" in parsed.path:
                raw = _json({"truncated": False, "tree": [
                    {"path": "plots/Figure_1.py", "type": "blob", "size": len(SCRIPT)},
                    {"path": "inputs/plot.csv", "type": "blob", "size": len(CSV)}]})
            else:
                raw = _json({"default_branch": "main", "license": {"spdx_id": "MIT"}})
        elif parsed.hostname == "raw.githubusercontent.com":
            if request.get_header("Authorization"):
                raise AssertionError("GitHub token leaked to raw-file host")
            raw = CSV if parsed.path.endswith(".csv") else SCRIPT
        elif parsed.hostname == "www.easyscholar.cc":
            if params.get("secretKey") != [SECRETS[1]]:
                raise AssertionError("Metric credential not passed correctly")
            raw = _json({"code": 200, "msg": "SUCCESS", "data": {"officialRank": {"all": {
                "sciif": "12.5", "sci": "Q1", "sciUp": "1区"}}}})
        elif parsed.hostname == "api.deepseek.com":
            if request.get_header("Authorization") != "Bearer " + SECRETS[0]:
                raise AssertionError("LLM authorization not passed correctly")
            payload = json.loads(request.data)
            if payload.get("response_format") != {"type": "json_object"} or payload.get("thinking") != {"type": "disabled"}:
                raise AssertionError("LLM JSON contract missing")
            facts = json.loads(payload["messages"][1]["content"])
            summary = {"title_zh": "合成测试案例：输入与图形注释", "summary_zh": "根据静态输入读取语句整理绘图步骤；尚未运行代码。",
                       "learning_points": ["先检查输入列名，再核对图号对应。"],
                       "limitations": ["图号对应未经人工核验。"], "evidence_ids": ["publication", "script-0"]}
            if facts["doi"].startswith("10.9999/ppl-fixture-") is not True:
                raise AssertionError("Unexpected paper in fixture")
            raw = _json({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(summary, ensure_ascii=False)}}]})
        else:
            raise AssertionError("Unexpected real source request in offline fixture: " + str(parsed.hostname))
        return FixtureResponse(url, raw)

    @staticmethod
    def assert_token(request):
        if request.get_header("Authorization") != "Bearer " + SECRETS[2]:
            raise AssertionError("GitHub API credential not passed correctly")


class PipelineEndToEndTests(unittest.TestCase):
    def prepare(self, root):
        (root / "web").mkdir()
        for filename in ("index.html", "app.js", "styles.css"):
            shutil.copyfile(PROJECT / "web" / filename, root / "web" / filename)
        write_json(root / "data" / "catalog.json", {"schemaVersion": 4, "papers": [], "inputSpecs": {},
                   "dimensions": {"forms": [], "functions": []}, "synthetic": True, "authorCodeExecuted": False})
        write_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}})
        write_json(root / "data" / "updates.json", [])

    def test_full_transport_pipeline_daily_cap_idempotency_and_public_boundary(self):
        with tempfile.TemporaryDirectory(prefix="ppl-synthetic-e2e-") as directory:
            root = Path(directory)
            self.prepare(root)
            fixture = OfficialApiFixture()
            settings = Settings(llm_api_key=SECRETS[0], llm_model="fixture-json-model",
                easyscholar_key=SECRETS[1], github_token=SECRETS[2], ncbi_api_key=SECRETS[3],
                discovery_limit=6, candidate_checks=6, max_new_per_day=5, max_ai_calls=5,
                max_requests=200, repair_limit=5, max_images=1, max_supplements=2,
                citation_enrichment=False)
            client = HttpClient(root / ".ppl" / "cache", max_requests=200, opener=fixture,
                                sleep=lambda delay: None, retries=1, max_post_attempts=5)
            first = run_pipeline(root, settings, client)
            self.assertEqual(first["newCount"], 5, first)
            self.assertEqual(first["dailyNewCount"], 5)
            self.assertEqual(first["aiAttempts"], 5)
            self.assertEqual(first["discoveryStatus"], "success")
            catalog = read_json(root / "data" / "catalog.json")
            self.assertEqual(len(catalog["papers"]), 5)
            for paper in catalog["papers"]:
                self.assertEqual(paper["repository"]["commit"], COMMIT)
                self.assertEqual(paper["repository"]["license"], "MIT")
                self.assertEqual(paper["repository"]["inputs"][0]["path"], "inputs/plot.csv")
                self.assertEqual([field["name"] for field in paper["repository"]["inputs"][0]["fields"]], ["sample", "age", "clock"])
                self.assertEqual(paper["article"]["figures"][0]["downloadStatus"], "downloaded")
                self.assertEqual(paper["article"]["figures"][0]["width"], 160)
                self.assertEqual(paper["article"]["figures"][0]["reuseStatus"], "allowed")
                self.assertEqual([item["status"] for item in paper["article"]["supplements"]], ["downloaded_private", "downloaded_private"])
                self.assertEqual(paper["ai"]["status"], "generated")
                self.assertEqual(paper["metrics"]["if"], "12.5")
                self.assertEqual(paper["metrics"]["jcr"], "Q1")
                self.assertEqual(paper["metrics"]["metric_year"], "unknown")
                self.assertEqual(paper["automation"]["status"], "partial")
                self.assertFalse(paper["runPerformed"])
            self.assertEqual(len(list((root / "site" / "assets").glob("*.png"))), 5)
            self.assertGreaterEqual(len(list((root / ".ppl" / "downloads").glob("**/*.zip"))), 5)
            public = read_json(root / "site" / "data" / "catalog.json")
            self.assertTrue(public["synthetic"])
            self.assertFalse(public["authorCodeExecuted"])
            self.assertEqual(len(public["papers"]), 5)
            public_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "site").rglob("*") if path.is_file() and path.suffix in {".json", ".js", ".css", ".html"})
            for secret in SECRETS:
                self.assertNotIn(secret, public_text)
            self.assertNotIn("fixture-private-row", public_text)
            self.assertNotIn("import pandas", public_text)
            self.assertFalse(list((root / "site").glob("**/*.zip")))
            self.assertFalse(list((root / "site").glob("**/*.csv")))
            self.assertFalse((root / "AUTHOR_CODE_EXECUTED").exists())
            second = run_pipeline(root, settings, client)
            self.assertEqual(second["newCount"], 0)
            self.assertEqual(second["dailyNewCount"], 5)
            self.assertEqual(second["repairCount"], 5)
            self.assertEqual(client.post_attempts, 5, "Unchanged evidence must not repeat billed AI calls.")
            self.assertEqual(len(read_json(root / "data" / "catalog.json")["papers"]), 5)
            for paper in read_json(root / "data" / "catalog.json")["papers"]:
                self.assertTrue(paper["ai"].get("persistent_cache_hit"))


if __name__ == "__main__":
    unittest.main()
