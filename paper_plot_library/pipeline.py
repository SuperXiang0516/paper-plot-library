"""Resumable daily orchestration; five new papers maximum, no author execution."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit

from .config import load_settings
from .enrichment import enrich_metrics, summarize
from .figures import inspect_article
from .http import HttpClient
from .repository import inspect_repository
from .site import build_site
from .sources import discover, lookup_paper, normalize_doi, paper_id
from .storage import project_lock, read_json, write_json


def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or catalog.get("authorCodeExecuted") is not False:
        raise ValueError("案例库必须明确 authorCodeExecuted=false")
    papers = catalog.get("papers")
    if not isinstance(papers, list):
        raise ValueError("papers 必须为列表")
    ids, dois = set(), set()
    for record in papers:
        doi = normalize_doi(record.get("doi"))
        identifier = record.get("id", "")
        if not re.fullmatch(r"[\w-]{1,120}", identifier) or not doi or doi in dois or identifier in ids:
            raise ValueError("存在无效/重复 DOI 或案例 ID；未修改案例库")
        if record.get("runPerformed", False) is not False:
            raise ValueError("本流程不允许宣称作者代码已运行")
        ids.add(identifier)
        dois.add(doi)
    return catalog


def validate_ledger(ledger, today):
    """Fail closed on corrupt quotas, then bound history without losing today's cap."""
    error = "更新配额记录格式无效；未修改案例库，请检查 data/ledger.json"
    if not isinstance(ledger, dict) or not isinstance(ledger.get("days"), dict) or not isinstance(ledger.get("candidates"), dict):
        raise ValueError(error)
    for day_key, value in ledger["days"].items():
        try:
            valid_date = isinstance(day_key, str) and date.fromisoformat(day_key).isoformat() == day_key
        except ValueError:
            valid_date = False
        if not valid_date or not isinstance(value, dict) or not isinstance(value.get("newDOIs"), list):
            raise ValueError(error)
        if any(not isinstance(doi, str) or not normalize_doi(doi) or doi != normalize_doi(doi) for doi in value["newDOIs"]):
            raise ValueError(error)
    for doi, value in ledger["candidates"].items():
        if (not isinstance(doi, str) or not normalize_doi(doi) or doi != normalize_doi(doi)
                or not isinstance(value, dict) or not isinstance(value.get("status"), str)
                or not _valid_timestamp(value.get("checkedAt"))):
            raise ValueError(error)
    earliest = (date.fromisoformat(today) - timedelta(days=89)).isoformat()
    bounded = deepcopy(ledger)
    bounded["days"] = {day_key: value for day_key, value in ledger["days"].items() if earliest <= day_key <= today}
    recent = ((doi, value) for doi, value in ledger["candidates"].items() if earliest <= value["checkedAt"][:10] <= today)
    bounded["candidates"] = dict(sorted(recent, key=lambda entry: entry[1]["checkedAt"], reverse=True)[:3000])
    return bounded


def _valid_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|\+00:00))?", value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def persistent_catalog(catalog):
    """Public repository state is not the private HTTP/full-text/source cache."""
    value = deepcopy(catalog)
    for paper in value["papers"]:
        paper.pop("abstract", None)
        for script in paper.get("repository", {}).get("scripts", []):
            for evidence in script.get("evidence", []):
                evidence.pop("text", None)  # Publish line links, never cached author source excerpts.
        for evidence in paper.get("article", {}).get("codeURLEvidence", []):
            evidence.pop("text", None)
        for figure in paper.get("article", {}).get("figures", []):
            if figure.get("reuseStatus") != "allowed":
                figure.pop("caption", None)
    return value


def import_seed(root, source):
    """One-time migration retaining the curated fifty cases and uncertainty."""
    root = Path(root)
    target = root / "data" / "catalog.json"
    if target.exists():
        raise ValueError("案例库已存在；不覆盖已有更新")
    catalog = validate_catalog(read_json(source))
    catalog["schemaVersion"] = 4
    catalog["siteId"] = "paper-plot-library"
    catalog["siteName"] = "论文绘图案例库"
    catalog["generatedAt"] = stamp()
    for record in catalog["papers"]:
        record["doi"] = normalize_doi(record["doi"])
        record["automation"] = {"status": "seed", "stages": {}, "lastCheckedAt": "",
                                "errors": [], "source": "人工核查种子；不等于自动验证通过"}
        # Resolve existing independently verified PMCID only; never guess it from DOI.
        for source_record in record.get("sources", []):
            match = re.search(r"/articles/(PMC\d+)/?", source_record.get("url", ""))
            if match:
                record["pmcid"] = match.group(1)
                break
        record["runPerformed"] = False
    root.joinpath("data").mkdir(parents=True, exist_ok=True)
    write_json(target, catalog)
    write_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}})
    write_json(root / "data" / "updates.json", [])
    build_site(root, catalog, [])
    return catalog


def editorial_score(record, counts=None):
    """Preference ranking, never a claim of scientific validity or paper quality."""
    counts = counts or Counter()
    text = " ".join(str(record.get(k, "")) for k in ("paper", "abstract", "topic", "description")).lower()
    topic = 12 * bool(re.search(r"aging|ageing|senescen|longevity|衰老", text))
    methods = 15 * bool(re.search(r"multi.?omic|single.?cell|spatial|computational|bioinformatic|software|tool|多组学", text))
    panels = max((len(set(re.findall(r"(?:^|[\s(,;])([a-h])(?:[),.;]|\s)", f.get("caption", "").lower())))
                  for f in record.get("article", {}).get("figures", [])), default=0)
    composite = min(20, panels * 4)
    repository = record.get("repository", {})
    usability = 25 * bool(repository.get("scripts")) + 15 * any(i.get("fields") for i in repository.get("inputs", []))
    images = 10 * any(f.get("asset") and f.get("reuseStatus") == "allowed" for f in record.get("article", {}).get("figures", []))
    journal = record.get("journal", "").casefold()
    # Explicit editorial preference, distinct from methodology quality.
    journal_preference = 5 * any(j in journal for j in ("nature", "cell", "science", "genome biology", "genome research"))
    citation = record.get("citations", {})
    percentile = citation.get("citation_normalized_percentile")
    citation_preference = min(5, max(0, percentile * 5)) if isinstance(percentile, (int, float)) else 0
    forms = record.get("graphForms", [])
    diversity = 5 if forms and any(counts.get(tag, 0) < 3 for tag in forms) else 0
    components = {"topic": topic, "method": methods, "composite": composite, "usability": usability,
                  "image": images, "journalPreference": journal_preference,
                  "normalizedCitationPreference": citation_preference, "categoryGap": diversity}
    return {"total": sum(components.values()), "components": components,
            "meaning": "采集优先级；期刊/引用仅为编辑偏好，不是研究质量评判"}


def enrich_citations(client, record, settings):
    old = record.get("citations", {})
    if not settings.citation_enrichment:
        return old or {"status": "disabled"}
    if old.get("retrievedAt") and stamp()[:10] == old["retrievedAt"][:10]:
        return old
    params = {"api_key": settings.openalex_key} if settings.openalex_key else None
    try:
        response = client.get_json("https://api.openalex.org/works/https://doi.org/" + quote(record["doi"], safe=""),
                                   params=params, ttl=604800, max_bytes=1000000)
        if normalize_doi(response.get("doi")) != record["doi"]:
            return dict(old, last_attempt_status="doi_mismatch") if old.get("status") == "matched" else {"status": "doi_mismatch"}
        result = {"status": "matched", "source": "OpenAlex", "retrievedAt": stamp(), "url": response.get("id", "")}
        for key in ("cited_by_count", "fwci"):
            value = response.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                result[key] = value
        raw = response.get("citation_normalized_percentile")
        value = raw.get("value") if isinstance(raw, dict) else raw
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
            result["citation_normalized_percentile"] = value
        result["normalization"] = "FWCI/其百分位按类型、年份、领域归一；缺失值不按0解释"
        return result
    except Exception:
        return dict(old, last_attempt_status="error") if old.get("status") == "matched" else {"status": "unavailable"}


FORM_RULES = {"scatter": r"\b(?:scatter|lmplot|regplot|geom_point)\b", "heatmap": r"\b(?:heatmap|clustermap|pheatmap|Heatmap)\b",
              "violin": r"\b(?:violinplot|geom_violin)\b", "box": r"\b(?:boxplot|geom_boxplot)\b",
              "line": r"\b(?:lineplot|geom_line)\b", "bar": r"\b(?:barplot|geom_bar|geom_col)\b"}
FUNCTION_RULES = {"association": r"correlation|regression|association|相关|回归", "differential": r"differential|差异",
                  "integration": r"multi.?omic|integration|多组学|整合", "prediction": r"predict|classifier|diagnos|预测|诊断"}


def classify(record):
    """Automation proposes; curated/approved categories alone enter live selectors."""
    text = " ".join(e.get("text", "") for s in record.get("repository", {}).get("scripts", []) for e in s.get("evidence", []))
    caption = " ".join(f.get("caption", "") for f in record.get("article", {}).get("figures", []) if f.get("asset"))
    def merge_suggestions(previous, rules, observed):
        # Curated seed proposals can be objects with uncertainty notes, not just IDs.
        proposals = {}
        for item in previous if isinstance(previous, list) else []:
            identifier = item.get("id") if isinstance(item, dict) else item
            if isinstance(identifier, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier):
                proposals[identifier] = item
        for tag, regex in rules.items():
            if re.search(regex, observed, re.I):
                proposals.setdefault(tag, tag)
        return [proposals[tag] for tag in sorted(proposals)]
    record["suggestedGraphForms"] = merge_suggestions(record.get("suggestedGraphForms"), FORM_RULES, text)
    record["suggestedAnalysisFunctions"] = merge_suggestions(record.get("suggestedAnalysisFunctions"), FUNCTION_RULES, caption)
    record.setdefault("graphForms", [])
    record.setdefault("analysisFunctions", [])
    record.setdefault("formClassificationStatus", "pending")
    record.setdefault("functionClassificationStatus", "pending")
    record["dimensionLinkScope"] = "selected-case"
    record.setdefault("dimensionLinkEvidence", "标签共现不证明逐分面图形—功能—代码对应；自动建议待核")


def input_spec(record, previous=None):
    inputs = record.get("repository", {}).get("inputs", [])
    if not inputs:
        return previous or {"tree": "输入待定位", "shape": "未知", "status": "未核", "files": []}
    files = []
    for item in inputs[:30]:
        fields = item.get("fields", [])
        references = item.get("observedFieldReferences", [])
        note = "真实文件表头已读取；含义/单位仍待核" if fields else "代码读取路径已观察；未读真实表头"
        rows = [[f["name"], "类型未核", "真实文件表头", "列含义/尺度未核"] for f in fields]
        files.append({"name": item.get("name", "未知"), "role": "脚本输入候选；不等于选图直接输入已核",
                      "format": item.get("format", "unknown"), "orientation": "未知", "encoding": "仅表头 UTF-8 检查；全文件编码未核",
                      "scale": "未知；不推断归一化", "note": note, "fields": rows,
                      "preview": "仅真实列名，不含虚构示例值" if fields else "真实表头未知",
                      "csv": "\n".join(f["name"] for f in fields) if fields else "# 未核实表头",
                      "mapping": f"{item.get('scriptURL', '')}；读取行 {item.get('line', '')}；图号对应仍待核",
                      "checks": ["核对直接绘图对象与作者脚本版本", "核对连接键、矩阵方向、单位、缺失值与变换"],
                      "url": item.get("url") or item.get("scriptURL", ""),
                      "observedFieldReferences": references})
    # Seed selected-case notes remain accessible; don't replace verified chosen input with unrelated all-repo inputs.
    if previous and previous.get("files"):
        return dict(previous, automaticInputs={"tree": "\n".join(f["name"] for f in files), "files": files,
                                                "status": "全仓库静态输入候选；与原选图输入分开"})
    return {"tree": "\n".join(f["name"] for f in files), "shape": "方向/单位待核", "status": "静态输入候选；未执行", "files": files}


def evidence_for(record):
    evidence = [{"id": "publication", "text": f"{record.get('paper')} DOI:{record['doi']}；期刊:{record.get('journal')}；时间:{record.get('date')}"},
                {"id": "inspection-boundary", "text": "作者代码未执行，输入尺度/图号对应未明示时保持未知。AI仅为待核学习注释。"}]
    for index, figure in enumerate(record.get("article", {}).get("figures", [])[:4]):
        evidence.append({"id": f"figure-{index}", "text": f"{figure['label']} 图注:{figure.get('caption', '')[:1500]}；来源哈希:{figure.get('sha256', '')}"})
    for index, script in enumerate(record.get("repository", {}).get("scripts", [])[:4]):
        observed = "；".join(e.get("text", "") for e in script.get("evidence", [])[:8])
        evidence.append({"id": f"script-{index}", "text": f"固定提交:{record['repository'].get('commit')}；路径:{script['path']}；SHA256:{script.get('sha256')}；静态观察:{observed}"})
    if record.get("evidence"):
        evidence.append({"id": "curated-note", "text": record["evidence"][:2000]})
    return evidence


def mapping_is_current(record):
    review = record.get("mappingReview", {})
    repo = record.get("repository", {})
    if (not isinstance(review, dict) or review.get("approved") is not True
            or not isinstance(review.get("reviewer"), str) or not 1 <= len(review["reviewer"].strip()) <= 200
            or not _valid_timestamp(review.get("reviewedAt")) or review.get("inputSchemaVerified") is not True):
        return False
    if (not isinstance(review.get("commit"), str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", review["commit"])
            or review["commit"] != repo.get("commit")):
        return False
    if any(not isinstance(review.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", review[key])
           for key in ("scriptSHA256", "figureSHA256", "inputSHA256")):
        return False
    script = next((s for s in repo.get("scripts", []) if s.get("path") == review.get("scriptPath") and s.get("sha256") == review.get("scriptSHA256")), None)
    if not script or not isinstance(script.get("url"), str):
        return False
    try:
        parsed = urlsplit(script["url"])
        path = unquote(parsed.path).split("/", 5)
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
                or len(path) != 6 or path[3] != "blob" or path[4] != review["commit"] or path[5] != script["path"]):
            return False
    except ValueError:
        return False
    figure = next((f for f in record.get("article", {}).get("figures", []) if f.get("id") == review.get("figureId") and f.get("sha256") == review.get("figureSHA256") and f.get("asset") and f.get("reuseStatus") == "allowed"), None)
    inputs = repo.get("inputs", [])
    exact_input = any(i.get("path") == review.get("inputPath") and i.get("headerSHA256") == review.get("inputSHA256")
                      and i.get("scriptURL") == script["url"] and i.get("fields") for i in inputs)
    return bool(script and figure and exact_input)


def refresh_status(record):
    stages = record["automation"]["stages"]
    ready = (stages.get("metadata") == "matched" and stages.get("article") == "inspected"
             and stages.get("repository") == "inspected" and mapping_is_current(record)
             and stages.get("ai") == "generated")
    status = "ready" if ready else "partial" if record.get("repository", {}).get("scripts") else "pending"
    if stages.get("metadata") == "excluded" or stages.get("article") in {"retracted", "excluded", "doi_mismatch"}:
        status = "excluded"
    record["automation"]["status"] = status


def complete_summary(client, record, settings, adapters):
    record["ai"] = adapters.get("summary", summarize)(client, record, evidence_for(record), settings)
    record["automation"]["stages"]["ai"] = record["ai"].get("status", "unknown")
    if record["ai"].get("status") in {"error", "invalid_response"}:
        record["automation"]["errors"].append("ai: " + record["ai"]["status"])
    refresh_status(record)
    return record


def inspect_record(client, record, root, settings, adapters=None, generate_ai=True):
    adapters = adapters or {}
    record = dict(record)
    stages, errors = {}, []
    metadata = adapters.get("lookup", lookup_paper)(client, record["doi"])
    stages["metadata"] = metadata.get("status", "unknown")
    if stages["metadata"] in {"excluded", "doi_mismatch", "invalid_doi"} or metadata.get("isRetracted"):
        record["automation"] = {"status": "excluded", "stages": stages, "errors": ["正式版本/撤稿检查未通过"], "lastCheckedAt": stamp()}
        # A revoked article must not keep previously public images.
        record["article"] = {"status": "excluded", "figures": [], "supplements": []}
        return record
    if stages["metadata"] == "matched":
        for key in ("pmcid", "pmid", "abstract", "publicationTypes", "issn", "eissn", "metadataSource"):
            if metadata.get(key):
                record[key] = metadata[key]
        # Keep curated id/selected figure/script; never relabel unknown title as a new case.
    else:
        record["automation"] = {"status": "pending", "stages": stages, "errors": ["metadata: " + stages["metadata"]], "lastCheckedAt": stamp()}
        return record
    record["maxFigureDownloads"] = settings.max_images
    record["maxSupplementDownloads"] = settings.max_supplements
    record["maxFileBytes"] = settings.max_file_bytes
    for stage, adapter, args in (
        ("article", adapters.get("article", inspect_article), (client, record, Path(root) / "data" / "assets", settings.include_supplements)),
        ("repository", adapters.get("repository", inspect_repository), (client, record)),
    ):
        value = adapter(*args)
        stages[stage] = value.get("status", "unknown")
        if stages[stage] in {"error", "unavailable"} and record.get(stage, {}).get("status") == "inspected":
            record[stage] = dict(record[stage], retained_from_previous=True, last_attempt_status=stages[stage])
        else:
            record[stage] = value
    record["metrics"] = adapters.get("metrics", enrich_metrics)(client, record, settings)
    stages["metrics"] = record["metrics"].get("status", "unknown")
    record["citations"] = adapters.get("citations", enrich_citations)(client, record, settings)
    if generate_ai:
        record["ai"] = adapters.get("summary", summarize)(client, record, evidence_for(record), settings)
    stages["ai"] = record.get("ai", {}).get("status", "deferred_until_selection")
    classify(record)
    record["runPerformed"] = False
    for stage, value in stages.items():
        if value in {"error", "unavailable", "metadata_unavailable", "invalid_response"}:
            errors.append(f"{stage}: {value}")
    record["automation"] = {"status": "pending", "stages": stages, "errors": errors, "lastCheckedAt": stamp()}
    refresh_status(record)
    record["selectionScore"] = editorial_score(record)
    return record


def new_case(record):
    result = dict(record)
    result.update(name="自动发现 · 选图待核", figure="", codeURL="", scriptURL="", lang="待核", langs=[],
                  topic="自动发现", description="选图、脚本对应及输入结构正在检查", graphForms=[], analysisFunctions=[],
                  formClassificationStatus="pending", functionClassificationStatus="pending", runPerformed=False,
                  collectionBatch="automatic", limitations=["静态检查不等于复现；自动建议标签需核验"])
    return result


def run_pipeline(root, settings=None, client=None, adapters=None, only_dois=None, discover_new=True):
    root = Path(root)
    settings = settings or load_settings(root)
    adapters = adapters or {}
    client = client or HttpClient(root / ".ppl" / "cache", settings.max_requests, max_post_attempts=settings.max_ai_calls)
    client.github_token = settings.github_token
    report = {"startedAt": stamp(), "date": stamp()[:10], "newCount": 0, "repairCount": 0, "checkedCount": 0,
              "failures": [], "discoveryStatus": "disabled", "authorCodeExecuted": False}
    with project_lock(root):
        catalog = validate_catalog(read_json(root / "data" / "catalog.json"))
        ledger = validate_ledger(read_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}}), report["date"])
        reports = read_json(root / "data" / "updates.json", [])
        if not isinstance(reports, list) or any(not isinstance(entry, dict) for entry in reports):
            raise ValueError("更新历史格式无效；未修改案例库，请检查 data/updates.json")
        day = ledger.setdefault("days", {}).setdefault(report["date"], {"newDOIs": []})
        seen = {p["doi"] for p in catalog["papers"]}
        repaired_ids = []

        def checkpoint():
            # Catalog is authoritative for daily admissions if a crash happened between these atomic files.
            write_json(root / "data" / "catalog.json", persistent_catalog(validate_catalog(catalog)))
            write_json(root / "data" / "ledger.json", validate_ledger(ledger, report["date"]))

        repairs = sorted(catalog["papers"], key=lambda p: p.get("automation", {}).get("lastCheckedAt", ""))
        if only_dois:
            wanted = {normalize_doi(d) for d in only_dois}
            repairs = [p for p in repairs if p["doi"] in wanted]
        for old in repairs[:settings.repair_limit]:
            try:
                revised = inspect_record(client, old, root, settings, adapters, generate_ai=False)
                catalog["papers"][catalog["papers"].index(old)] = revised
                catalog.setdefault("inputSpecs", {})[old["id"]] = input_spec(revised, catalog.get("inputSpecs", {}).get(old["id"]))
                report["repairCount"] += 1
                repaired_ids.append(old["id"])
                checkpoint()
            except Exception as exc:
                report["failures"].append({"doi": old["doi"], "stage": "repair", "error": type(exc).__name__})
            report["checkedCount"] += 1

        # Admission time also carried in records: stale/missing ledger cannot create >5/day.
        admitted_today = {p["doi"] for p in catalog["papers"] if p.get("admittedAt", "").startswith(report["date"])}
        daily = set(day["newDOIs"]) | admitted_today
        allowance = max(0, settings.max_new_per_day - len(daily))
        if discover_new and allowance:
            try:
                candidates = adapters.get("discover", discover)(client, settings)
                report["discoveryStatus"] = "success"
                candidates = sorted(candidates, key=lambda p: editorial_score(p)["total"], reverse=True)
                checked = []
                checked_dois = set()
                for candidate in candidates:
                    doi = normalize_doi(candidate.get("doi"))
                    if not doi or doi in seen or doi in checked_dois or candidate.get("isPreprint") or candidate.get("isRetracted"):
                        continue
                    candidate["doi"] = doi
                    if candidate.get("date", "")[:10] > report["date"]:
                        continue
                    state = ledger.setdefault("candidates", {}).get(doi, {})
                    if state.get("checkedAt", "").startswith(report["date"]) and state.get("status") == "pending":
                        continue
                    if len(checked) >= settings.candidate_checks:
                        break
                    checked_dois.add(doi)
                    try:
                        revised = inspect_record(client, new_case(candidate), root, settings, adapters, generate_ai=False)
                    except Exception as exc:
                        report["failures"].append({"doi": doi, "stage": "candidate", "error": type(exc).__name__})
                        ledger["candidates"][doi] = {"checkedAt": stamp(), "status": "pending", "reason": "本篇临时失败；未中断其它候选"}
                        report["checkedCount"] += 1
                        checked.append({"doi": doi, "skip": True})
                        checkpoint()
                        continue
                    code_evidence = revised.get("article", {}).get("codeURLEvidence", [])
                    declared = {e.get("url", "").rstrip("/") for e in code_evidence}
                    actual_repo = revised.get("repository", {}).get("url", "").rstrip("/")
                    acceptable = (revised["automation"]["stages"].get("metadata") == "matched"
                                  and revised["automation"]["status"] != "excluded"
                                  and actual_repo in declared and bool(revised.get("repository", {}).get("scripts")))
                    ledger["candidates"][doi] = {"checkedAt": stamp(), "status": "source_confirmed" if acceptable else "pending",
                                                  "reason": "作者代码声明和可读脚本已定位" if acceptable else "正式文献/作者代码/脚本依据尚不齐全"}
                    checked.append(revised)
                    report["checkedCount"] += 1
                    checkpoint()
                counts = Counter(tag for p in catalog["papers"] for tag in p.get("graphForms", []))
                usable = [p for p in checked if not p.get("skip") and ledger["candidates"][p["doi"]]["status"] == "source_confirmed"]
                usable.sort(key=lambda p: editorial_score(p, counts)["total"], reverse=True)
                for revised in usable[:allowance]:
                    revised["admittedAt"] = stamp()
                    revised["selectionScore"] = editorial_score(revised, counts)
                    catalog["papers"].append(revised)
                    catalog.setdefault("inputSpecs", {})[revised["id"]] = input_spec(revised)
                    seen.add(revised["doi"])
                    daily.add(revised["doi"])
                    day["newDOIs"] = sorted(daily)
                    report["newCount"] += 1
                    # Commit admission BEFORE a paid call; interrupted runs can safely repair it.
                    checkpoint()
                    try:
                        complete_summary(client, revised, settings, adapters)
                    except Exception as exc:
                        revised["automation"]["errors"].append("summary: " + type(exc).__name__)
                    checkpoint()
            except Exception as exc:
                report["discoveryStatus"] = "partial_failure"
                report["failures"].append({"stage": "discovery", "error": type(exc).__name__})
        # New selected cases get the first AI budget; then old-case repairs.
        for record in catalog["papers"]:
            if record["id"] not in repaired_ids or record["automation"]["stages"].get("metadata") != "matched" or record["automation"]["status"] == "excluded":
                continue
            try:
                complete_summary(client, record, settings, adapters)
                checkpoint()
            except Exception as exc:
                report["failures"].append({"doi": record["doi"], "stage": "summary", "error": type(exc).__name__})
        catalog["generatedAt"] = stamp()
        catalog["checkedAt"] = report["date"]
        checked_records = [p for p in catalog["papers"] if p.get("automation", {}).get("lastCheckedAt", "") >= report["startedAt"]]
        for record in checked_records:
            for error in record.get("automation", {}).get("errors", []):
                report["failures"].append({"doi": record["doi"], "stage": error.split(":")[0], "error": error})
        report["stageCounts"] = {stage: dict(Counter(p.get("automation", {}).get("stages", {}).get(stage, "not_checked") for p in checked_records))
                                 for stage in ("metadata", "article", "repository", "metrics", "ai")}
        report["missingKeys"] = [key for key, value in (("LLM_API_KEY", settings.llm_api_key), ("LLM_MODEL", settings.llm_model), ("EASYSCHOLAR_SECRET_KEY", settings.easyscholar_key)) if not value]
        report.update(finishedAt=stamp(), requests=getattr(client, "requests", 0), aiAttempts=getattr(client, "post_attempts", 0),
                      totalCount=len(catalog["papers"]), dailyNewCount=len(daily),
                      status="partial" if report["failures"] or any(p.get("automation", {}).get("status") != "ready" for p in checked_records) else "completed")
        reports = [report] + reports[:89]
        checkpoint()
        write_json(root / "data" / "updates.json", reports)
        build_site(root, catalog, reports)
    return report
