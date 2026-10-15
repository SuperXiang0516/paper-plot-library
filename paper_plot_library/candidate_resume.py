"""Public-repository-safe candidate checkpoints, separate from private HTTP cache.

Only immutable source references, validated raster hashes, observed column names
and validated AI notes survive a disposable runner. Abstracts, source excerpts,
prompts, provider responses and credentials never enter these checkpoints.
Every resumed candidate still resolves its DOI and checks current source status.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, urlsplit, unquote

from .bootstrap import _fixed_scripts, _verified_asset
from .enrichment import PROMPT_VERSION, _SUMMARY_FIELDS, _validate_summary
from .figures import inspect_article
from .repository import inspect_repository, _repository_url
from .sources import normalize_doi


_HASH = re.compile(r"[a-f0-9]{64}")
_COMMIT = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_SEED = {"id": 120, "doi": 200, "paper": 1000, "journal": 300, "date": 32,
         "year": 4, "pmcid": 40, "pmid": 40, "paperURL": 1000}
_ARTICLE = {"status", "version", "xmlURL", "license", "figures"}
_FIGURE = {"id", "label", "sourceURL", "captionHash", "license", "asset", "sha256",
           "assetSHA256", "width", "height"}
_REPOSITORY = {"status", "url", "commit", "license", "scripts", "inputs"}
_SCRIPT = {"path", "url", "sha256", "language", "lineScope"}
_INPUT = {"name", "path", "reader", "format", "status", "orientation", "scale", "scriptURL",
          "line", "url", "headerSHA256", "fields"}
_TOP = {"version", "doi", "seed", "metadataHash", "updatedAt", "article", "repository", "ai", "aiBinding"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value, limit=1000):
    return (isinstance(value, str) and len(value) <= limit
            and not any(ord(c) < 32 for c in value))


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _url(value):
    if not _text(value, 1500) or not value:
        return False
    try:
        parsed = urlsplit(value)
        integrity_query = (parsed.hostname == "pmc-oa-opendata.s3.amazonaws.com"
                           and re.fullmatch(r"md5=[a-fA-F0-9]{32}", parsed.query))
        return (parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
                and not parsed.password and not parsed.port and (not parsed.query or integrity_query) and not parsed.fragment)
    except ValueError:
        return False


def _path(value, asset=False):
    if not _text(value, 400) or not value or "\\" in value or "//" in value:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and not any(part in {".", ".."} for part in value.split("/"))
            and (not asset or value.startswith("assets/") and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}))


def _project(source, keys):
    return {key: deepcopy(source[key]) for key in keys if key in source}


def article_projection(value, root):
    if not isinstance(value, dict) or value.get("status") != "inspected":
        return None
    if not _url(value.get("xmlURL")) or not _text(value.get("version"), 20):
        return None
    figures = []
    for figure in value.get("figures", []):
        if (not isinstance(figure, dict) or not _verified_asset(figure, root)
                or not _url(figure.get("sourceURL"))):
            continue
        item = _project(figure, _FIGURE - {"captionHash"})
        item["captionHash"] = _hash(figure.get("caption", ""))
        item["assetSHA256"] = figure.get("assetSHA256") or figure.get("sha256")
        figures.append(item)
    if not figures:
        return None
    return {"status": "inspected", "version": value["version"], "xmlURL": value["xmlURL"],
            "license": value.get("license", "unknown"), "figures": figures[:5]}


def repository_projection(value):
    if not isinstance(value, dict) or value.get("status") != "inspected":
        return None
    scripts = _fixed_scripts(value)
    if not scripts:
        return None
    # A static input path with no actual header remains incomplete and must be
    # rescanned. Never freeze a failed header fetch into a permanent cache hit.
    inputs = []
    script_urls = {script["url"] for script in scripts}
    for item in value.get("inputs", [])[:30]:
        if (not isinstance(item, dict) or item.get("scriptURL") not in script_urls or not _path(item.get("path"))
                or not _url(item.get("url"))
                or item.get("status") != "header_observed" or not _HASH.fullmatch(str(item.get("headerSHA256", "")))):
            continue
        fields = [{"name": field["name"], "kind": "actual_file_header"}
                  for field in item.get("fields", [])[:80]
                  if isinstance(field, dict) and field.get("kind") == "actual_file_header"
                  and _text(field.get("name"), 120) and field["name"].strip()]
        if fields:
            projected = _project(item, _INPUT)
            projected["fields"] = fields
            inputs.append(projected)
    if not inputs:
        return None
    return {"status": "inspected", "url": value["url"], "commit": value["commit"],
            "license": value.get("license", "unknown"),
            "scripts": [_project(script, _SCRIPT) for script in scripts[:12]], "inputs": inputs}


def validate_checkpoint(value, doi):
    """Strict schema validation, including every nested public-persistence field."""
    def invalid():
        raise ValueError("候选阶段检查点无效；不能忽略或重置已保存进度")
    if (not isinstance(value, dict) or set(value) - _TOP or value.get("version") != 1
            or type(value.get("version")) is not int or value.get("doi") != doi
            or not normalize_doi(doi) or doi != normalize_doi(doi)
            or not _time(value.get("updatedAt")) or not _HASH.fullmatch(str(value.get("metadataHash", "")))):
        invalid()
    seed = value.get("seed")
    if (not isinstance(seed, dict) or set(seed) - set(_SEED) or seed.get("doi") != doi
            or not seed.get("paper") or not re.fullmatch(r"[\w-]{1,120}", str(seed.get("id", "")))
            or any(not _text(item, _SEED[key]) for key, item in seed.items())
            or "paperURL" in seed and not _url(seed["paperURL"])):
        invalid()
    article = value.get("article")
    if article is not None:
        if (not isinstance(article, dict) or set(article) != _ARTICLE or article.get("status") != "inspected"
                or not _url(article.get("xmlURL")) or not _text(article.get("license"), 200)
                or not _text(article.get("version"), 20) or not isinstance(article.get("figures"), list)
                or not 1 <= len(article["figures"]) <= 5):
            invalid()
        for figure in article["figures"]:
            if (not isinstance(figure, dict) or set(figure) != _FIGURE or not _path(figure.get("asset"), True)
                    or not all(_text(figure.get(key), limit) for key, limit in (("id", 120), ("label", 500), ("license", 200)))
                    or not _url(figure.get("sourceURL"))
                    or any(not _HASH.fullmatch(str(figure.get(key, ""))) for key in ("sha256", "assetSHA256", "captionHash"))
                    or any(type(figure.get(key)) is not int or not 1 <= figure[key] <= 100000 for key in ("width", "height"))):
                invalid()
    repo = value.get("repository")
    if repo is not None:
        if (not isinstance(repo, dict) or set(repo) != _REPOSITORY or repo.get("status") != "inspected"
                or not _url(repo.get("url")) or not _COMMIT.fullmatch(str(repo.get("commit", "")))
                or not _text(repo.get("license"), 200) or not isinstance(repo.get("scripts"), list)
                or not 1 <= len(repo["scripts"]) <= 12 or not isinstance(repo.get("inputs"), list)
                or not 1 <= len(repo["inputs"]) <= 30 or len(_fixed_scripts(repo)) != len(repo["scripts"])):
            invalid()
        for script in repo["scripts"]:
            if (not isinstance(script, dict) or set(script) - _SCRIPT or not _path(script.get("path"))
                    or any(not _text(script[key], 100) for key in ("language", "lineScope") if key in script)):
                invalid()
        for item in repo["inputs"]:
            if (not isinstance(item, dict) or set(item) - _INPUT or item.get("status") != "header_observed"
                    or not _path(item.get("path")) or not _url(item.get("url"))
                    or item.get("scriptURL") not in {script["url"] for script in repo["scripts"]}
                    or unquote(urlsplit(item["url"]).path) != urlsplit(repo["url"]).path.rstrip("/") + "/blob/" + repo["commit"] + "/" + item["path"]
                    or not _HASH.fullmatch(str(item.get("headerSHA256", "")))
                    or any(not _text(item[key], 400) for key in _INPUT - {"fields", "line"} if key in item)
                    or "line" in item and (type(item["line"]) is not int or not 1 <= item["line"] <= 10000000)
                    or not isinstance(item.get("fields"), list) or not 1 <= len(item["fields"]) <= 80):
                invalid()
            for field in item["fields"]:
                if (not isinstance(field, dict) or set(field) != {"name", "kind"}
                        or field["kind"] != "actual_file_header" or not _text(field["name"], 120) or not field["name"].strip()):
                    invalid()
    if "ai" in value or "aiBinding" in value:
        ai = value.get("ai")
        allowed = {"publication", "inspection-boundary"} | {f"figure-{i}" for i in range(4)} | {f"script-{i}" for i in range(4)}
        validated = _validate_summary(ai, allowed)
        if (not article or not repo or not _HASH.fullmatch(str(value.get("aiBinding", "")))
                or not validated or set(ai) != _SUMMARY_FIELDS):
            invalid()
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 100000:
        invalid()
    return deepcopy(value)


def _metadata_hash(metadata):
    return _hash({key: metadata.get(key) for key in ("doi", "paper", "abstract", "pmcid", "publicationTypes")})


class CandidateResume:
    """Adapter decorator: fresh source checks, cached immutable stage products."""

    def __init__(self, root, doi, previous, save, settings, adapters, now):
        self.root, self.doi, self.save, self.settings, self.adapters, self.now = Path(root), doi, save, settings, adapters, now
        self.value = validate_checkpoint(previous, doi) if previous else None
        self.active = False

    def _save(self, record):
        if not self.active:
            return
        seed = {key: record[key] for key, limit in _SEED.items() if key in record and _text(record[key], limit)}
        if not seed.get("paperURL"):
            seed.pop("paperURL", None)
        self.value.update(seed=seed, updatedAt=self.now())
        try:
            validated = validate_checkpoint(self.value, self.doi)
        except ValueError:
            return  # Unprojectable adapter products never become persistent data.
        self.save(validated)

    def wrapped(self, lookup):
        result = dict(self.adapters)
        def checked_lookup(client, doi):
            metadata = lookup(client, doi)
            self.active = (isinstance(metadata, dict) and metadata.get("status") == "matched"
                           and normalize_doi(metadata.get("doi", doi)) == doi and not metadata.get("isRetracted"))
            if self.active:
                digest = _metadata_hash(metadata)
                if not self.value or self.value["metadataHash"] != digest:
                    self.value = {"version": 1, "doi": doi, "metadataHash": digest}
            return metadata
        result.update(lookup=checked_lookup, article=self.article, repository=self.repository, summary=self.summary)
        return result

    def article(self, client, record, asset_dir, include_supplements):
        cached = self.value.get("article") if self.active and self.value else None
        inspector = self.adapters.get("article", inspect_article)
        # The ordinary parser rechecks current license/DOI/version and declarations.
        # Only the immutable, already-downloaded licensed raster is reused.
        probe_record = dict(record, maxFigureDownloads=0) if cached and inspector is inspect_article else record
        value = inspector(client, probe_record, asset_dir, include_supplements)
        if cached and inspector is inspect_article and isinstance(value, dict) and value.get("status") == "inspected":
            same_source = all(value.get(key) == cached[key] for key in ("version", "xmlURL", "license"))
            reused = 0
            if same_source:
                for figure in value.get("figures", []):
                    previous = next((item for item in cached["figures"] if item["id"] == figure.get("id")
                        and item["sourceURL"] == figure.get("sourceURL") and item["license"] == figure.get("license")
                        and item["captionHash"] == _hash(figure.get("caption", ""))), None)
                    immutable_media = re.fullmatch(r"md5=[a-fA-F0-9]{32}", urlsplit(figure.get("sourceURL", "")).query)
                    if (previous and immutable_media and figure.get("reuseStatus") == "allowed"
                            and not figure.get("sha256") and _verified_asset(dict(previous, reuseStatus="allowed"), self.root)):
                        figure.update(_project(previous, _FIGURE - {"captionHash"}))
                        figure.update(downloadStatus="downloaded", checkpointAssetReused=True)
                        reused += 1
            if inspector is inspect_article and not reused:
                value = inspector(client, record, asset_dir, include_supplements)
        projected = article_projection(value, self.root)
        if self.active and projected:
            if self.value.get("article") != projected:
                self.value.pop("ai", None)
                self.value.pop("aiBinding", None)
            self.value["article"] = projected
            self._save(record)
        return value

    def _repository_current(self, client, record, cached):
        found = _repository_url(record)
        if not found or cached["url"] != "https://github.com/" + found[0] + "/" + found[1]:
            return False
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if getattr(client, "github_token", ""):
            headers["Authorization"] = "Bearer " + client.github_token
        base = "https://api.github.com/repos/" + quote(found[0]) + "/" + quote(found[1])
        metadata = client.get_json(base, ttl=0, headers=headers)
        if metadata.get("private") is not False or metadata.get("visibility", "public") != "public":
            return False
        commit = client.get_json(base + "/commits/" + quote(metadata.get("default_branch") or "main", safe=""), ttl=0, headers=headers)
        current_license = (metadata.get("license") or {}).get("spdx_id")
        current_license = current_license if current_license and current_license not in {"NOASSERTION", "OTHER"} else "unknown"
        return commit.get("sha") == cached["commit"] and current_license == cached["license"]

    def repository(self, client, record):
        cached = self.value.get("repository") if self.active and self.value else None
        inspector = self.adapters.get("repository", inspect_repository)
        # Custom inspectors must explicitly supply an immutable-snapshot probe.
        # They otherwise run normally: a test adapter is not fresh source proof.
        probe = self.adapters.get("checkpoint_repository_current")
        if cached and (probe or inspector is inspect_repository):
            try:
                current = probe(client, record, cached) if probe else self._repository_current(client, record, cached)
            except Exception:
                current = False
            if current is True:
                value = dict(deepcopy(cached), authorCodeExecuted=False, checkpointReused=True,
                             sourceSnapshotCheckedAt=self.now(), limitations=[
                                 "Pinned source snapshot reused after public repository/commit check; author code was not executed.",
                                 "Only previously verified script references and actual input headers were retained; source excerpts and unverified inputs were not copied."])
                self._save(record)
                return value
        value = inspector(client, record)
        projected = repository_projection(value)
        if self.active and projected:
            if self.value.get("repository") != projected:
                self.value.pop("ai", None)
                self.value.pop("aiBinding", None)
            self.value["repository"] = projected
            self._save(record)
        return value

    def _ai_binding(self, record):
        return _hash({"prompt": PROMPT_VERSION, "model": self.settings.llm_model,
                      "endpoint": _hash(self.settings.llm_api_url), "metadata": self.value["metadataHash"],
                      "article": article_projection(record.get("article"), self.root),
                      "repository": repository_projection(record.get("repository"))})

    def summary(self, client, record, evidence, settings):
        from .enrichment import summarize
        allowed = {item["id"] for item in evidence if isinstance(item, dict) and isinstance(item.get("id"), str)}
        binding = self._ai_binding(record) if self.active else None
        if binding and self.value.get("aiBinding") == binding:
            valid = _validate_summary(self.value.get("ai"), allowed)
            if valid:
                return dict(valid, checkpoint_cache_hit=True, ai_attempts=0)
        value = self.adapters.get("summary", summarize)(client, record, evidence, settings)
        if (self.active and isinstance(value, dict) and value.get("status") == "generated"
                and not value.get("last_attempt_status") and value.get("author_code_executed") is not True):
            projected = {key: value.get(key) for key in _SUMMARY_FIELDS}
            valid = _validate_summary(projected, allowed)
            if valid and self.value.get("article") and self.value.get("repository"):
                self.value["ai"] = {key: valid[key] for key in _SUMMARY_FIELDS}
                self.value["aiBinding"] = binding
                self._save(record)
        return value
