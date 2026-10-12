import json
import unittest

from paper_plot_library.repository import inspect_repository, inspect_source

COMMIT = "a" * 40
SCRIPT = b'import pandas as pd\ndf = pd.read_csv("data.csv")\nx = df["age"]\nplt.savefig("Figure_1.pdf")\n'


class FakeClient:
    def __init__(self, license="MIT", raw=SCRIPT):
        self.license, self.raw, self.calls = license, raw, []

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        if "/commits/" in url:
            return {"sha": COMMIT}
        if "/git/trees/" in url:
            return {"truncated": False, "tree": [{"path": "Figure_1.py", "type": "blob", "size": 100},
                    {"path": "data.csv", "type": "blob", "size": 30},
                    {"path": "../escape.py", "type": "blob", "size": 10},
                    {"path": "execute.py", "type": "blob", "size": 9000000}]}
        return {"default_branch": "main", "license": {"spdx_id": self.license}}

    def get_bytes(self, url, **kwargs):
        self.calls.append(url)
        return b"sample,age\npatient1,50\n" if url.endswith("data.csv") else self.raw


class RepositoryTests(unittest.TestCase):
    def test_static_inputs_commit_and_mapping(self):
        client = FakeClient()
        record = inspect_repository(client, {"codeURL": "https://github.com/a/b"})
        self.assertEqual(record["status"], "inspected")
        self.assertEqual(record["commit"], COMMIT)
        self.assertEqual(record["inputs"][0]["fields"][1]["name"], "age")
        self.assertEqual(record["figureMappings"][0]["status"], "candidate")
        self.assertFalse(record["authorCodeExecuted"])
        self.assertFalse(record["sourceScanLimited"])
        self.assertNotIn("patient1", json.dumps(record))
        self.assertFalse(any("escape" in url or "execute.py" in url for url in client.calls))

    def test_bounded_source_scan_persists_limitation_for_safe_pruning(self):
        class ManyScripts(FakeClient):
            def get_json(self, url, **kwargs):
                if "/git/trees/" in url:
                    self.calls.append(url)
                    return {"truncated": False, "tree": [{"path": "plot" + str(i) + ".py", "type": "blob", "size": 100} for i in range(13)]}
                return super().get_json(url, **kwargs)
        client = ManyScripts(raw=b"x=1\n")
        result = inspect_repository(client, {"codeURL": "https://github.com/a/b"}, max_files=12)
        self.assertTrue(result["sourceScanLimited"])
        self.assertEqual(len(result["scripts"]), 12)

    def test_truncated_tree_is_not_exhaustive_source_scan(self):
        class Truncated(FakeClient):
            def get_json(self, url, **kwargs):
                result = super().get_json(url, **kwargs)
                if "/git/trees/" in url:
                    result["truncated"] = True
                return result
        result = inspect_repository(Truncated(), {"codeURL": "https://github.com/a/b"})
        self.assertTrue(result["sourceScanLimited"])

    def test_no_unknown_license_author_code_redistribution(self):
        record = inspect_repository(FakeClient(license="NOASSERTION"), {"codeURL": "https://github.com/a/b"})
        self.assertEqual(record["license"], "unknown")
        self.assertTrue(any("link-only" in note for note in record["limitations"]))

    def test_bad_url_never_fetched(self):
        client = FakeClient()
        result = inspect_repository(client, {"codeURL": "https://github.com@127.0.0.1/a/b"})
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(client.calls, [])

    def test_notebook_outputs_ignored(self):
        from paper_plot_library.repository import _source_text
        raw = json.dumps({"cells": [{"cell_type": "code", "source": ["x=1"], "outputs": [{"text": "sensitive"}]}]}).encode()
        text, scope = _source_text(raw, "analysis.ipynb")
        self.assertNotIn("sensitive", text)
        self.assertIn("concatenation", scope)

    def test_r_observed_fields_not_asserted_input_headers(self):
        result = inspect_source('d <- read.csv("x.csv")\nx <- d$age\nggsave("Figure2.pdf")', "x.R")
        self.assertEqual(result["inputs"][0]["path"], "x.csv")
        self.assertEqual(result["fields"][0]["kind"], "code_field_reference")
        self.assertEqual(result["outputs"][0]["path"], "Figure2.pdf")

    def test_dynamic_path_explicit_unknown(self):
        result = inspect_source('df = pd.read_csv(config["data_path"])', "x.py")
        self.assertEqual(result["inputs"][0]["status"], "dynamic_path")

    def test_header_none_not_claimed_as_fields(self):
        client = FakeClient(raw=b'df = pd.read_csv("data.csv", header=None)\n')
        record = inspect_repository(client, {"codeURL": "https://github.com/a/b"})
        self.assertEqual(record["inputs"][0]["fields"], [])
        self.assertEqual(record["inputs"][0]["status"], "header_unresolved")

    def test_tab_separator_and_skiprows_respected(self):
        from paper_plot_library.repository import _actual_header
        raw = b"comment\nsample\tage\na\t25\n"
        self.assertEqual(_actual_header(raw, ".txt", {"sep": "\t", "skiprows": 1}), ["sample", "age"])
        self.assertEqual(_actual_header(raw, ".txt", {"namesProvided": True}), [])

    def test_explicit_script_url_preferred_for_same_repo(self):
        from paper_plot_library.repository import _repository_url
        value = _repository_url({"codeURL": "https://github.com/a/b", "scriptURL": "https://github.com/a/b/blob/main/plots/Fig2.R"})
        self.assertEqual(value, ("a", "b", "plots/Fig2.R"))

    def test_article_author_declaration_beats_incidental_abstract_url(self):
        from paper_plot_library.repository import _repository_url
        value = _repository_url({"codeURLs": ["https://github.com/cited/tool"],
                                 "article": {"codeURLs": ["https://github.com/author/paper"]}})
        self.assertEqual(value, ("author", "paper", ""))

    def test_relative_input_parent_stays_inside_repo(self):
        from paper_plot_library.repository import _input_path
        self.assertEqual(_input_path("scripts/Fig1.py", "../data.csv", {"data.csv": {}}), "data.csv")
        self.assertEqual(_input_path("scripts/Fig1.py", "../../secret.csv", {"secret.csv": {}}), "")

    def test_token_only_sent_github_api(self):
        class HeaderClient(FakeClient):
            github_token = "not-a-real-key"
            def get_json(self, url, **kwargs):
                self.api_header = kwargs.get("headers", {})
                return super().get_json(url, **kwargs)
            def get_bytes(self, url, **kwargs):
                self.assert_no_auth = not kwargs.get("headers", {}).get("Authorization")
                return super().get_bytes(url, **kwargs)
        client = HeaderClient()
        inspect_repository(client, {"codeURL": "https://github.com/a/b"})
        self.assertEqual(client.api_header["Authorization"], "Bearer not-a-real-key")
        self.assertTrue(client.assert_no_auth)

    def test_redirect_block_sanitized_not_worked_around(self):
        from paper_plot_library.http import FetchError
        class RedirectBlockedClient(FakeClient):
            def get_json(self, url, **kwargs):
                raise FetchError("远程来源不在许可 HTTPS 接口名单中", status=301)
        client = RedirectBlockedClient()
        result = inspect_repository(client, {"codeURL": "https://github.com/a/b"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["errorStatus"], 301)
        self.assertIn("许可 HTTPS", result["lastError"])
        self.assertNotIn("http", result["lastError"])

    def test_private_or_internal_repository_stops_before_tree_or_code(self):
        for marker in [{"private": True}, {"visibility": "private"}, {"visibility": "internal"}]:
            class PrivateClient(FakeClient):
                def get_json(self, url, **kwargs):
                    self.calls.append(url)
                    return {"default_branch": "main", "license": {"spdx_id": "MIT"}, **marker}
                def get_bytes(self, url, **kwargs):
                    raise AssertionError("Private author file must not be fetched")
            client = PrivateClient()
            client.github_token = "fixture-private-token"
            result = inspect_repository(client, {"codeURL": "https://github.com/a/b"})
            self.assertEqual(result["status"], "link_only_private")
            self.assertEqual(client.calls, ["https://api.github.com/repos/a/b"])
            self.assertEqual(result["commit"], "")
            self.assertEqual(result["scripts"], [])
            self.assertEqual(result["inputs"], [])

    def test_commit_length_must_be_exactly_40_or_64(self):
        for length in [39, 41, 48, 63, 65]:
            class InvalidCommitClient(FakeClient):
                def get_json(self, url, **kwargs):
                    if "/commits/" in url:
                        self.calls.append(url)
                        return {"sha": "a" * length}
                    return super().get_json(url, **kwargs)
            client = InvalidCommitClient()
            result = inspect_repository(client, {"codeURL": "https://github.com/a/b"})
            self.assertEqual(result["status"], "unavailable")
            self.assertFalse(any("/git/trees/" in url or "raw.githubusercontent" in url for url in client.calls))


if __name__ == "__main__":
    unittest.main()
