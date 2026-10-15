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
from .bootstrap import (bootstrap_assessment, bootstrap_plan, bootstrap_report, note_bootstrap_attempt,
                        _safe_record_state, safe_removed_dois, effective_collection_mode)
from .enrichment import enrich_metrics, summarize
from .figures import inspect_article
from .http import BudgetExceeded, HttpClient
from .repository import inspect_repository
from .retention import archive_sources, article_safety_failure, retain_enrichment, retain_source, sanitized_archive
from .site import build_site, has_publishable_body_figure, _safe_reason, _safe_http_status
from .sources import discover, lookup_paper, lookup_scope_metadata, normalize_doi, paper_id
from .storage import project_lock, read_json, write_json
from .scope import (POLICIES, assess_scope, eligible_daily_dois, has_verified_scope,
                    matches_scope, scope_priority, select_scope_candidates)
from .candidate_resume import CandidateResume, validate_checkpoint


def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_FAILED_STAGES = {
    "error", "unavailable", "metadata_unavailable", "invalid_response", "budget_exhausted",
    "authentication_error", "payment_required", "permission_denied", "invalid_request",
    "endpoint_not_found", "rate_limited", "service_unavailable", "other_http_error",
    "network_error", "invalid_endpoint",
}
_AI_RUN_FATAL = {"missing_key", "missing_model", "missing_endpoint", "invalid_endpoint",
                 "authentication_error", "payment_required", "permission_denied",
                 "invalid_request", "endpoint_not_found"}


def _collection_budget_exhausted(client):
    """Stop source scans before they consume the slots held for paid requests."""
    if not isinstance(client, HttpClient):
        return False  # Adapter-only offline clients do not implement transport budgets.
    reserved = min(client.reserved_post_requests, max(0, client.max_post_attempts - client.post_attempts))
    return client.requests >= client.max_requests - reserved


def _can_summarize(record):
    return (record.get("automation", {}).get("stages", {}).get("metadata") == "matched"
            and record.get("automation", {}).get("stages", {}).get("article") == "inspected"
            and record.get("automation", {}).get("stages", {}).get("repository") == "inspected"
            and record.get("automation", {}).get("status") != "excluded"
            and record.get("article", {}).get("status") == "inspected"
            and record.get("repository", {}).get("status") == "inspected"
            and not record.get("article", {}).get("retained_from_previous")
            and not record.get("repository", {}).get("retained_from_previous")
            and bool(record.get("repository", {}).get("scripts")))


def _summary_backlog(record):
    """Identify checked AI work; it must share repair slots with untouched seeds."""
    return _can_summarize(record) and record.get("ai", {}).get("status") != "generated"


def _safe_repair_schedule(record):
    """Only bounded scheduling facts persist; never provider messages or credentials."""
    raw = record.get("automation", {}).get("repairSchedule", {})
    if not isinstance(raw, dict):
        return {}
    result = {}
    failures = raw.get("consecutiveFailures")
    if type(failures) is int and 0 <= failures <= 30:
        result["consecutiveFailures"] = failures
    last_attempt = raw.get("lastAttemptAt")
    if _valid_timestamp(last_attempt) and "T" in last_attempt:
        result["lastAttemptAt"] = last_attempt
    next_attempt = raw.get("nextAttemptAt")
    if result.get("lastAttemptAt") and _valid_timestamp(next_attempt) and "T" in next_attempt:
        start = datetime.fromisoformat(result["lastAttemptAt"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(next_attempt.replace("Z", "+00:00"))
        if timedelta(0) <= end - start <= timedelta(hours=24):
            result["nextAttemptAt"] = next_attempt
    outcome = raw.get("lastOutcome")
    if isinstance(outcome, str) and outcome in {"automatic_complete", "failure", "incomplete", "deferred", "excluded"}:
        result["lastOutcome"] = outcome
    return result


def _untouched(record):
    automation = record.get("automation", {})
    return not _valid_timestamp(automation.get("lastCheckedAt")) and not automation.get("stages")


def _automatic_outcome(record, settings):
    """Pipeline completion is separate from human approval of exact figure binding."""
    stages = record.get("automation", {}).get("stages", {})
    if record.get("automation", {}).get("status") == "excluded":
        return "excluded"
    if any(value in _FAILED_STAGES - {"budget_exhausted"} for value in stages.values()):
        return "failure"
    if "budget_exhausted" in stages.values():
        return "deferred"
    source_complete = (stages.get("metadata") == "matched" and stages.get("article") == "inspected"
                       and stages.get("repository") == "inspected"
                       and not record.get("article", {}).get("retained_from_previous")
                       and not record.get("repository", {}).get("retained_from_previous")
                       and bool(record.get("repository", {}).get("scripts")))
    if not source_complete:
        return "incomplete"
    if settings.llm_api_key and settings.llm_model and stages.get("ai") != "generated":
        return "incomplete"
    if settings.easyscholar_key and stages.get("metrics") not in {"matched", "not_found"}:
        return "incomplete"
    return "automatic_complete"


def _has_content_gaps(record):
    """Missing reusable assets/real headers need another scan, not a false ready flag."""
    return (not any(f.get("asset") and f.get("reuseStatus") == "allowed"
                    for f in record.get("article", {}).get("figures", []))
            or not any(item.get("fields") for item in record.get("repository", {}).get("inputs", [])))


def _update_repair_schedule(record, settings, previous, attempted_at, exception=False):
    outcome = "failure" if exception else _automatic_outcome(record, settings)
    previous_failures = previous.get("consecutiveFailures", 0)
    failures = min(30, previous_failures + 1) if outcome == "failure" else 0
    # One immediate retry permits recovery after a corrected key/transient outage;
    # repeated failures then back off. Budget/configuration skips are not failures.
    delay = min(24, 2 ** min(5, failures - 2)) if failures >= 2 else 0
    if outcome == "incomplete" or (outcome == "automatic_complete" and _has_content_gaps(record)):
        delay = 24
    schedule = {"consecutiveFailures": failures, "lastAttemptAt": attempted_at, "lastOutcome": outcome}
    if delay:
        next_attempt = datetime.fromisoformat(attempted_at.replace("Z", "+00:00")) + timedelta(hours=delay)
        schedule["nextAttemptAt"] = next_attempt.isoformat().replace("+00:00", "Z")
    record["automation"]["repairSchedule"] = schedule
    return outcome


def _repair_plan(papers, limit, now, ledger, backlog_ids, only_dois=None):
    """Bounded fair lanes: seeds advance while known gaps get repair opportunities."""
    explicit = bool(only_dois)
    wanted = {normalize_doi(doi) for doi in only_dois} if explicit else None
    eligible, skipped = [], 0
    for paper in papers:
        if wanted is not None and paper["doi"] not in wanted:
            continue
        schedule = _safe_repair_schedule(paper)
        if not explicit and paper.get("automation", {}).get("status") == "excluded":
            continue
        # A forged future lastAttemptAt cannot freeze a case, even if its relative
        # cooldown interval otherwise looks valid. Parse UTC values, not spellings.
        last_attempt = schedule.get("lastAttemptAt", "")
        next_attempt = schedule.get("nextAttemptAt", "")
        active_cooldown = bool(last_attempt and next_attempt and
            datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
            <= datetime.fromisoformat(now.replace("Z", "+00:00"))
            < datetime.fromisoformat(next_attempt.replace("Z", "+00:00")))
        if not explicit and active_cooldown:
            skipped += 1
            continue
        eligible.append(paper)
    seeds = sorted((p for p in eligible if _untouched(p)), key=lambda p: p["id"])
    retries = sorted((p for p in eligible if not _untouched(p)), key=lambda p: (
        _safe_repair_schedule(p).get("lastOutcome") == "automatic_complete" and not _has_content_gaps(p),
        p.get("automation", {}).get("lastCheckedAt", ""), p["id"] not in backlog_ids, p["id"]))
    if limit <= 0:
        return [], skipped
    if limit == 1 and seeds and retries:
        state = ledger.get("scheduler", {})
        group = state.get("nextGroup", "retry") if isinstance(state, dict) else "retry"
        chosen = [seeds[0]] if group == "seed" else [retries[0]]
        ledger["scheduler"] = {"nextGroup": "retry" if group == "seed" else "seed"}
        return chosen, skipped
    seed_slots = (limit + 1) // 2 if retries else limit
    retry_slots = limit - seed_slots if seeds else limit
    chosen = seeds[:seed_slots] + retries[:retry_slots]
    chosen_ids = {p["id"] for p in chosen}
    remainder = [p for p in seeds + retries if p["id"] not in chosen_ids]
    chosen.extend(remainder[:max(0, limit - len(chosen))])
    return chosen, skipped


def _retain_unchecked_summary(record):
    """Keep old prose visible as old, never as a successful current source check."""
    previous = record.get("ai", {})
    if previous.get("summary_zh") and previous.get("status") in {"generated", "stale"}:
        retained = dict(previous, status="stale", last_attempt_status="missing_evidence",
                        error_reason="missing_evidence",
                        stale_reason="本轮文献、原图或作者代码未通过来源复查；保留旧注释，不生成新说明。")
        retained.pop("http_status", None)
        record["ai"] = retained
        return True
    return False


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
        if "checkpoint" in value:
            validate_checkpoint(value["checkpoint"], doi)
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
        if "retentionArchive" in paper:
            archive = sanitized_archive(paper)
            if archive is None:
                paper.pop("retentionArchive", None)
            else:
                paper["retentionArchive"] = archive
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
    scope = assess_scope(record)
    topic = 12 * ("aging" in scope["topicMatches"])
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
    except BudgetExceeded:
        return (dict(old, last_attempt_status="budget_exhausted") if old.get("status") == "matched"
                else {"status": "budget_exhausted"})
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


def _summary_result(client, record, settings, adapters):
    """Roll back unsuccessful provider side effects; keep post-success gates."""
    before = deepcopy(record)
    try:
        value = adapters.get("summary", summarize)(client, record, evidence_for(record), settings)
    except Exception:
        record.clear()
        record.update(before)
        raise
    if value.get("status") != "generated":
        record.clear()
        record.update(before)
    return retain_enrichment(before.get("ai", {}), value, "ai")


def complete_summary(client, record, settings, adapters):
    if _can_summarize(record):
        record["ai"] = _summary_result(client, record, settings, adapters)
    else:
        # Do not pay to describe retained captions/scripts as fresh source evidence.
        if not _retain_unchecked_summary(record):
            record["ai"] = {"status": "missing_evidence", "error_reason": "missing_evidence"}
    current_status = record["ai"].get("last_attempt_status") or record["ai"].get("status", "unknown")
    record["automation"]["stages"]["ai"] = current_status
    # A successful retry must not keep yesterday's AI error in today's report.
    errors = record["automation"].setdefault("errors", [])
    errors[:] = [error for error in errors if not isinstance(error, str) or not error.startswith("ai:")]
    if current_status in _FAILED_STAGES:
        errors.append("ai: " + current_status)
    refresh_status(record)
    return record


def _stage_failure(record, error):
    """Attach classified diagnostics, never raw third-party response text."""
    stage = error.split(":", 1)[0] if isinstance(error, str) else "unknown"
    result = {"doi": record["doi"], "stage": stage, "error": error}
    current = record.get("automation", {}).get("stages", {}).get(stage)
    if isinstance(current, str) and current in _FAILED_STAGES:
        result["status"] = current
        evidence = record.get(stage, {})
        if isinstance(evidence, dict):
            if _safe_reason(evidence.get("error_reason")):
                result["error_reason"] = evidence["error_reason"]
            if _safe_http_status(evidence.get("http_status")):
                result["http_status"] = evidence["http_status"]
    return result


def inspect_record(client, record, root, settings, adapters=None, generate_ai=True):
    adapters = adapters or {}
    repair_schedule = _safe_repair_schedule(record)
    bootstrap_state = (_safe_record_state(record)
                       if isinstance(record.get("automation", {}).get("bootstrap"), dict) else None)
    # Inspectors may append nested evidence before raising. Never let a failed
    # transaction mutate the caller's last-good source record in place.
    record = deepcopy(record)
    stages, errors = {}, []
    metadata = adapters.get("lookup", lookup_paper)(client, record["doi"])
    stages["metadata"] = metadata.get("status", "unknown")
    if (metadata.get("doi") and normalize_doi(metadata["doi"]) != normalize_doi(record["doi"])):
        stages["metadata"] = "doi_mismatch"
    if stages["metadata"] in {"excluded", "doi_mismatch", "invalid_doi"} or metadata.get("isRetracted"):
        archive_sources(record, "retracted" if metadata.get("isRetracted") else stages["metadata"], stamp())
        record["automation"] = {"status": "excluded", "stages": stages, "errors": ["正式版本/撤稿检查未通过"], "lastCheckedAt": stamp()}
        record["automation"]["repairSchedule"] = repair_schedule
        if bootstrap_state is not None:
            record["automation"]["bootstrap"] = bootstrap_state
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
        record["automation"]["repairSchedule"] = repair_schedule
        if bootstrap_state is not None:
            record["automation"]["bootstrap"] = bootstrap_state
        _retain_unchecked_summary(record)
        return record
    if settings.research_scope != "unrestricted":
        # The discovery payload, curated tags and previous AI prose are not
        # sufficient evidence. Re-evaluate the exact DOI's current metadata.
        if not metadata.get("abstract") and record.get("article", {}).get("figures"):
            # Missing new scope evidence is not affirmative evidence that an
            # existing paper became unrelated. Keep its previous DOI/title
            # decision pending, but do not bill AI against retained sources.
            stages["researchScope"] = "metadata_unavailable"
            record["automation"] = {"status": "pending", "stages": stages,
                "errors": ["researchScope: metadata_unavailable"], "lastCheckedAt": stamp(),
                "repairSchedule": repair_schedule}
            if bootstrap_state is not None:
                record["automation"]["bootstrap"] = bootstrap_state
            _retain_unchecked_summary(record)
            return record
        if metadata.get("paper"):
            record["paper"] = metadata["paper"]
        record["abstract"] = metadata.get("abstract") or ""
        record["researchScope"] = assess_scope(record, settings.research_scope,
            allow_saved=False, source="verified-metadata")
        if not matches_scope(record, settings.research_scope):
            archive_sources(record, "scope_excluded", stamp())
            stages["researchScope"] = "excluded"
            record["automation"] = {"status": "excluded", "stages": stages,
                "errors": ["当前正式文献元数据不符合测序/组学与衰老/代谢主题要求"], "lastCheckedAt": stamp(),
                "repairSchedule": repair_schedule}
            if bootstrap_state is not None:
                record["automation"]["bootstrap"] = bootstrap_state
            record["article"] = {"status": "excluded", "figures": [], "supplements": []}
            return record
    record["maxFigureDownloads"] = settings.max_images
    record["maxSupplementDownloads"] = settings.max_supplements
    record["maxFileBytes"] = settings.max_file_bytes
    for stage, adapter in (
        ("article", adapters.get("article", inspect_article)),
        ("repository", adapters.get("repository", inspect_repository)),
    ):
        args = ((client, deepcopy(record), Path(root) / "data" / "assets", settings.include_supplements)
                if stage == "article" else (client, deepcopy(record)))
        value = adapter(*args)
        stages[stage] = value.get("status", "unknown")
        safety = article_safety_failure(value, record.get(stage, {})) if stage == "article" else ""
        if safety:
            archive_sources(record, safety, stamp())
            record["article"] = dict(value, status="excluded", figures=[], supplements=[])
            stages["article"] = safety if safety in {"retracted", "doi_mismatch", "excluded"} else "excluded"
            record["automation"] = {"status": "excluded", "stages": stages,
                "errors": ["article: " + safety], "lastCheckedAt": stamp(),
                "repairSchedule": repair_schedule}
            if bootstrap_state is not None:
                record["automation"]["bootstrap"] = bootstrap_state
            _retain_unchecked_summary(record)
            return record
        current_usable = None
        if stage == "article" and record.get(stage, {}).get("figures") and value.get("status") == "inspected":
            current_usable = has_publishable_body_figure(Path(root), dict(record, article=value,
                automation={"status": "partial"}))
        record[stage] = retain_source(record.get(stage), value, stage, current_usable)
    old_metrics = deepcopy(record.get("metrics", {}))
    record["metrics"] = retain_enrichment(old_metrics,
        adapters.get("metrics", enrich_metrics)(client, record, settings), "metrics")
    stages["metrics"] = record["metrics"].get("last_attempt_status") or record["metrics"].get("status", "unknown")
    record["citations"] = adapters.get("citations", enrich_citations)(client, record, settings)
    current_sources = (stages.get("article") == "inspected" and stages.get("repository") == "inspected"
                       and not record.get("article", {}).get("retained_from_previous")
                       and not record.get("repository", {}).get("retained_from_previous")
                       and bool(record.get("repository", {}).get("scripts")))
    if not current_sources:
        _retain_unchecked_summary(record)
    if generate_ai and current_sources:
        record["ai"] = _summary_result(client, record, settings, adapters)
        stages["ai"] = record["ai"].get("last_attempt_status") or record["ai"].get("status", "unknown")
    else:
        stages["ai"] = "missing_evidence" if not current_sources else "deferred_until_selection"
    classify(record)
    record["runPerformed"] = False
    for stage, value in stages.items():
        if value in _FAILED_STAGES:
            errors.append(f"{stage}: {value}")
    record["automation"] = {"status": "pending", "stages": stages, "errors": errors, "lastCheckedAt": stamp()}
    record["automation"]["repairSchedule"] = repair_schedule
    if bootstrap_state is not None:
        record["automation"]["bootstrap"] = bootstrap_state
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


def refresh_legacy_scope(client, catalog, settings, adapters=None):
    """One bounded metadata-only migration, never full source/paid processing.

    Old catalogues omit abstracts. Re-query at most fifty exact DOIs together
    rather than dropping valid aging atlases/clocks based on short titles alone.
    This transport uses the ordinary HTTP counter and leaves sources untouched.
    """
    if settings.research_scope == "unrestricted":
        return 0
    pending = [paper for paper in catalog["papers"]
               if not has_verified_scope(paper, settings.research_scope)]
    if not pending or _collection_budget_exhausted(client):
        return 0
    if len(pending) > 50:
        # Today's apparent successes need canonical verification first. Rotate
        # the rest so unresolved DOI rows at the head cannot starve later ones.
        today = stamp()[:10]
        priority = [p for p in pending if isinstance(p.get("admittedAt"), str)
                    and p["admittedAt"].startswith(today)][:50]
        priority_ids = {p["id"] for p in priority}
        remaining = [p for p in pending if p["id"] not in priority_ids]
        cursor = catalog.get("scopeMetadataCursor", {})
        offset = cursor.get("offset", 0) if isinstance(cursor, dict) and cursor.get("policy") == settings.research_scope else 0
        offset = offset if type(offset) is int and 0 <= offset <= 10000 else 0
        offset %= max(1, len(remaining))
        slots = 50 - len(priority)
        pending = priority + (remaining[offset:] + remaining[:offset])[:slots]
        catalog["scopeMetadataCursor"] = {"policy": settings.research_scope,
                                         "offset": (offset + slots) % max(1, len(remaining))}
    try:
        resolved = (adapters or {}).get("scope_metadata", lookup_scope_metadata)(client, pending)
    except Exception:
        return 0  # Missing/failed metadata is not a positive domain decision.
    checked = 0
    for paper in pending:
        metadata = resolved.get(paper["doi"]) if isinstance(resolved, dict) else None
        if (not isinstance(metadata, dict) or metadata.get("status") != "matched"
                or normalize_doi(metadata.get("doi")) != paper["doi"] or not metadata.get("paper")):
            continue
        paper["paper"] = metadata["paper"]
        evidence = dict(paper, abstract=metadata.get("abstract") or "")
        paper["researchScope"] = assess_scope(evidence, settings.research_scope,
            allow_saved=False, source="verified-metadata")
        checked += 1
    return checked


def run_pipeline(root, settings=None, client=None, adapters=None, only_dois=None, discover_new=True, mode="auto", session_dois=None):
    root = Path(root)
    settings = settings or load_settings(root)
    if not isinstance(settings.research_scope, str) or settings.research_scope not in POLICIES:
        raise ValueError("未知研究领域策略；未开始采集")
    adapters = adapters or {}
    effective_mode = effective_collection_mode(mode)
    if session_dois is not None:
        if (mode != "backfill" or discover_new or not isinstance(session_dois, list)
                or not 1 <= len(session_dois) <= min(5, settings.max_papers_per_run, settings.bootstrap_batch_limit)
                or any(not isinstance(doi, str) or normalize_doi(doi) != doi for doi in session_dois)
                or len(set(session_dois)) != len(session_dois)):
            raise ValueError("连续补库批次必须是最多5篇既有规范 DOI；不得发现新文献")
    reserved_ai = min(settings.max_ai_calls, settings.max_requests) if settings.llm_api_key and settings.llm_model else 0
    client = client or HttpClient(root / ".ppl" / "cache", settings.max_requests,
                                  max_post_attempts=settings.max_ai_calls, reserved_post_requests=reserved_ai)
    if isinstance(client, HttpClient):
        # Injected test/local clients obey the same reservation policy as cloud clients.
        client.reserved_post_requests = min(reserved_ai, client.max_requests, client.max_post_attempts)
    client.github_token = settings.github_token
    client.ai_stopped_status = ""
    report = {"startedAt": stamp(), "date": stamp()[:10], "newCount": 0, "repairCount": 0, "checkedCount": 0,
              "failures": [], "discoveryStatus": "disabled", "authorCodeExecuted": False,
              "aiRepairCount": 0, "collectionBudgetExhausted": False,
              "maxRequests": getattr(client, "max_requests", settings.max_requests),
              "maxAiCalls": settings.max_ai_calls,
              "paperBatchLimit": settings.max_papers_per_run,
              "maxAiAttemptsPerPaper": settings.max_ai_attempts_per_paper,
              "aiCasesAttempted": 0, "aiRetriedCaseCount": 0, "aiRetrySuccessCount": 0,
              "reservedAiRequests": getattr(client, "reserved_post_requests", reserved_ai),
              "untouchedRepairCount": 0, "retryRepairCount": 0, "cooldownSkippedCount": 0,
              "untouchedRemainingCount": 0, "automaticCompleteCount": 0, "incompleteCount": 0}
    with project_lock(root):
        catalog = validate_catalog(read_json(root / "data" / "catalog.json"))
        if settings.research_scope != "unrestricted":
            catalog["researchScopePolicy"] = settings.research_scope
            for paper in catalog["papers"]:
                paper["researchScope"] = assess_scope(paper, settings.research_scope)
        else:
            catalog.pop("researchScopePolicy", None)
        ledger = validate_ledger(read_json(root / "data" / "ledger.json", {"days": {}, "candidates": {}}), report["date"])
        reports = read_json(root / "data" / "updates.json", [])
        if not isinstance(reports, list) or any(not isinstance(entry, dict) for entry in reports):
            raise ValueError("更新历史格式无效；未修改案例库，请检查 data/updates.json")
        if session_dois is not None and not set(session_dois) <= {p["doi"] for p in catalog["papers"]}:
            raise ValueError("连续补库批次包含非既有文章；未开始请求")
        report["scopeMetadataCheckedCount"] = refresh_legacy_scope(client, catalog, settings, adapters)
        initialization = (settings.initialize_existing_first or mode == "backfill"
                          or "bootstrap" in ledger or "bootstrap" in catalog)
        bootstrap_created = initialization and "bootstrap" not in ledger
        before = bootstrap_report(catalog, ledger, root=root, now=report["startedAt"]) if initialization else None
        if bootstrap_created:
            # Save the fixed cohort before any catalog checkpoint can publish its
            # progress. Interruption must not leave a summary without its cohort.
            write_json(root / "data" / "ledger.json", validate_ledger(ledger, report["date"]))
        is_backfill = effective_mode == "backfill"
        report["mode"] = effective_mode
        if before:
            catalog["bootstrap"] = before
            report["bootstrap"] = before
        if is_backfill:
            discover_new = False
            report["discoveryStatus"] = "bootstrap_needs_attention" if before["status"] == "needs_attention" else "bootstrap_in_progress"
        day = ledger.setdefault("days", {}).setdefault(report["date"], {"newDOIs": []})
        seen = {p["doi"] for p in catalog["papers"]} | safe_removed_dois(ledger)
        repaired_ids = []
        checked_ids = set()
        checked_doi_order = []
        original_schedules = {}
        failed_ids = set()
        summarized_ids = set()
        backlog_ids = {p["id"] for p in catalog["papers"] if _summary_backlog(p)} if reserved_ai else set()
        summary_attempts = Counter()

        def summarize_case(item, stage_adapters=None):
            before_attempts = getattr(client, "post_attempts", 0)
            try:
                return complete_summary(client, item, settings, adapters if stage_adapters is None else stage_adapters)
            finally:
                after_attempts = getattr(client, "post_attempts", 0)
                if type(before_attempts) is int and type(after_attempts) is int:
                    summary_attempts[item["id"]] += max(0, after_attempts - before_attempts)

        def checkpoint():
            # Catalog is authoritative for daily admissions if a crash happened between these atomic files.
            write_json(root / "data" / "catalog.json", persistent_catalog(validate_catalog(catalog)))
            write_json(root / "data" / "ledger.json", validate_ledger(ledger, report["date"]))

        if session_dois is not None:
            by_doi = {paper["doi"]: paper for paper in catalog["papers"]}
            repairs = [by_doi[doi] for doi in session_dois]
        elif is_backfill:
            batch_limit = min(settings.bootstrap_batch_limit, settings.max_papers_per_run)
            repairs = bootstrap_plan(catalog, ledger, batch_limit, report["startedAt"],
                                     root=root, retry_attention=mode == "backfill" or bool(only_dois))
            if only_dois:
                wanted = {normalize_doi(doi) for doi in only_dois}
                # Explicit selection cannot be lost behind the batch limit.
                selected = dict(catalog, papers=[p for p in catalog["papers"] if p["doi"] in wanted])
                repairs = bootstrap_plan(selected, ledger, batch_limit, report["startedAt"],
                                         root=root, retry_attention=True)
        else:
            repair_slots = min(settings.repair_limit, settings.max_papers_per_run)
            next_workload = None
            # Maintenance and discovery share a paper cap. Leave room for new
            # candidates instead of spending the whole batch refreshing old ones.
            today_has_room = len(eligible_daily_dois(catalog, day["newDOIs"], report["date"],
                                                   settings.research_scope)) < settings.max_new_per_day
            if discover_new and not only_dois and today_has_room and settings.candidate_checks:
                if settings.max_papers_per_run == 1:
                    scheduler = ledger.get("scheduler", {})
                    group = scheduler.get("nextWorkload", "repair") if isinstance(scheduler, dict) else "repair"
                    repair_slots = 0 if group == "discovery" else repair_slots
                    next_workload = "repair" if group == "discovery" else "discovery"
                else:
                    candidate_slots = min(3, settings.max_papers_per_run - 1)
                    repair_slots = min(repair_slots, settings.max_papers_per_run - candidate_slots)
            repairs, report["cooldownSkippedCount"] = _repair_plan(
                catalog["papers"], repair_slots, report["startedAt"], ledger, backlog_ids, only_dois)
            if next_workload:
                raw = ledger.get("scheduler", {})
                state = {"nextWorkload": next_workload}
                group = raw.get("nextGroup") if isinstance(raw, dict) else None
                if isinstance(group, str) and group in {"seed", "retry"}:
                    state["nextGroup"] = group
                ledger["scheduler"] = state
        for old in repairs:
            if _collection_budget_exhausted(client):
                report["collectionBudgetExhausted"] = True
                break
            record_index = next(index for index, item in enumerate(catalog["papers"]) if item["id"] == old["id"])
            try:
                original_schedules[old["id"]] = _safe_repair_schedule(old)
                revised = inspect_record(client, old, root, settings, adapters, generate_ai=False)
                catalog["papers"][record_index] = revised
                catalog.setdefault("inputSpecs", {})[old["id"]] = input_spec(revised, catalog.get("inputSpecs", {}).get(old["id"]))
                report["repairCount"] += 1
                report["untouchedRepairCount" if _untouched(old) else "retryRepairCount"] += 1
                repaired_ids.append(old["id"])
                checked_ids.add(old["id"])
                report["collectionBudgetExhausted"] |= _collection_budget_exhausted(client)
                _update_repair_schedule(revised, settings, original_schedules[old["id"]], revised["automation"]["lastCheckedAt"])
                checkpoint()
                # Previously admitted cases must not starve behind fresh discoveries.
                # Recheck their sources first; do not summarize stale/retracted evidence.
                if (initialization or old["id"] in backlog_ids) and _can_summarize(revised):
                    summarized_ids.add(old["id"])
                    try:
                        summarize_case(revised)
                        if revised.get("ai", {}).get("status") == "generated":
                            report["aiRepairCount"] += 1
                    except Exception as exc:
                        failed_ids.add(old["id"])
                        report["failures"].append({"doi": old["doi"], "stage": "summary", "error": type(exc).__name__})
                    _update_repair_schedule(revised, settings, original_schedules[old["id"]], revised["automation"]["lastCheckedAt"], old["id"] in failed_ids)
                    checkpoint()
                if is_backfill:
                    if not _can_summarize(revised):
                        revised["automation"]["stages"]["ai"] = "missing_evidence"
                    note_bootstrap_attempt(revised, catalog.get("inputSpecs", {}).get(revised["id"]),
                                           stamp(), root=root,
                                           attempt_status="error" if old["id"] in failed_ids else None)
                    checkpoint()
            except Exception as exc:
                # Persist only a classified current failure and a bounded retry time.
                # The old sources remain retained but cannot become fresh paid input.
                revised = deepcopy(old)
                reason = "budget_exhausted" if isinstance(exc, BudgetExceeded) else "error"
                revised["automation"] = {"status": "pending", "stages": {"metadata": reason},
                                         "errors": ["repair: " + reason], "lastCheckedAt": stamp()}
                if isinstance(old.get("automation", {}).get("bootstrap"), dict):
                    revised["automation"]["bootstrap"] = _safe_record_state(old)
                _retain_unchecked_summary(revised)
                _update_repair_schedule(revised, settings, original_schedules.get(old["id"], {}), revised["automation"]["lastCheckedAt"])
                if is_backfill:
                    note_bootstrap_attempt(revised, catalog.get("inputSpecs", {}).get(revised["id"]),
                                           stamp(), root=root, attempt_status=reason)
                catalog["papers"][record_index] = revised
                checked_ids.add(old["id"])
                report["failures"].append({"doi": old["doi"], "stage": "repair", "error": type(exc).__name__})
                checkpoint()
            report["checkedCount"] += 1
            checked_doi_order.append(old["doi"])

        # Maintenance may reveal old material gaps. Report them honestly without
        # switching today's independent discovery back into initialization.
        if initialization and not is_backfill:
            refreshed = bootstrap_report(catalog, ledger, root=root, now=stamp())
            catalog["bootstrap"] = refreshed
            report["bootstrap"] = refreshed
            checkpoint()

        # Admission time also carried in records: stale/missing ledger cannot create >5/day.
        daily = eligible_daily_dois(catalog, day["newDOIs"], report["date"], settings.research_scope)
        allowance = max(0, settings.max_new_per_day - len(daily))
        if discover_new and not allowance:
            report["discoveryStatus"] = "daily_quota_reached"
        elif discover_new and allowance and _collection_budget_exhausted(client):
            report["discoveryStatus"] = "budget_exhausted"
            report["collectionBudgetExhausted"] = True
        elif discover_new and allowance and report["checkedCount"] >= settings.max_papers_per_run:
            report["discoveryStatus"] = "paper_batch_limit_reached"
        elif discover_new and allowance:
            try:
                # Known material gaps cool for a week, not a fresh scan of the
                # same first sixty failures every UTC morning. Transient/AI/
                # configuration failures remain eligible on the next UTC day.
                earliest_retry = (date.fromisoformat(report["date"]) - timedelta(days=7)).isoformat()
                cooling = {doi for doi, state in ledger["candidates"].items()
                           if state["checkedAt"].startswith(report["date"])
                           or settings.require_complete_new_cases and state.get("retryClass") == "materials"
                               and state["checkedAt"][:10] > earliest_retry}
                # Unadmitted drafts are not a public catalog or paid cache. They
                # retain only a controlled seed and immutable stage products,
                # and still pass fresh DOI/retraction/source checks below.
                resumable = [deepcopy(state["checkpoint"]["seed"])
                             for doi, state in ledger["candidates"].items()
                             if "checkpoint" in state and doi not in seen | cooling]
                try:
                    if adapters.get("discover"):
                        candidates = adapters["discover"](client, settings)
                    else:
                        candidates = discover(client, settings, exclude_dois=seen | cooling)
                except Exception as exc:
                    if not resumable:
                        raise
                    candidates = []
                    report["failures"].append({"stage": "discovery", "error": type(exc).__name__})
                    # A search outage must not discard a previously checkpointed
                    # DOI. Canonical metadata/source probes still run normally.
                candidates = resumable + candidates
                report["discoveryStatus"] = "success"
                # Reserve lanes only after real eligibility/deduplication. An
                # already seen or cooling DOI cannot occupy the tools slot.
                available = {}
                for candidate in candidates:
                    doi = normalize_doi(candidate.get("doi"))
                    if (not doi or doi in seen | cooling or candidate.get("isPreprint")
                            or candidate.get("isRetracted") or candidate.get("date", "")[:10] > report["date"]):
                        continue
                    state = ledger["candidates"].get(doi, {})
                    if not matches_scope(candidate, settings.research_scope) and "checkpoint" not in state:
                        report["scopeRejectedCount"] = report.get("scopeRejectedCount", 0) + 1
                        continue
                    # The saved seed omits abstracts. Prefer richer fresh
                    # metadata for the same DOI, never a duplicate paper slot.
                    if doi not in available or candidate.get("abstract") and not available[doi].get("abstract"):
                        available[doi] = dict(candidate, doi=doi)
                ordered = sorted(available.values(), key=lambda p: editorial_score(p)["total"], reverse=True)
                slots = min(settings.candidate_checks, max(0, settings.max_papers_per_run - report["checkedCount"]))
                # An abstract-only verified candidate may no longer match its
                # title after redaction. Give pending provenance rechecks one
                # slot; do not invent scope evidence merely to resume it.
                pending_scope = [p for p in ordered if not matches_scope(p, settings.research_scope)]
                reserve_pending = min(1, slots, len(pending_scope))
                candidates = select_scope_candidates(
                    [p for p in ordered if matches_scope(p, settings.research_scope)],
                    slots - reserve_pending, settings.research_scope)
                candidates = pending_scope[:reserve_pending] + candidates
                if len(candidates) < slots:
                    candidates.extend(pending_scope[reserve_pending:reserve_pending + slots - len(candidates)])
                checked = []
                checked_dois = set()
                candidate_adapters = {}
                for candidate in candidates:
                    candidate_doi = normalize_doi(candidate.get("doi"))
                    candidate_state = ledger["candidates"].get(candidate_doi, {})
                    if not matches_scope(candidate, settings.research_scope) and "checkpoint" not in candidate_state:
                        report["scopeRejectedCount"] = report.get("scopeRejectedCount", 0) + 1
                        continue  # No download, paid attempt or five-paper slot.
                    if report["checkedCount"] >= settings.max_papers_per_run:
                        report["discoveryStatus"] = "paper_batch_limit_reached"
                        break
                    if _collection_budget_exhausted(client):
                        report["collectionBudgetExhausted"] = True
                        report["discoveryStatus"] = "budget_exhausted"
                        break
                    doi = normalize_doi(candidate.get("doi"))
                    if not doi or doi in seen or doi in checked_dois or candidate.get("isPreprint") or candidate.get("isRetracted"):
                        continue
                    candidate["doi"] = doi
                    if candidate.get("date", "")[:10] > report["date"]:
                        continue
                    state = ledger.setdefault("candidates", {}).get(doi, {})
                    # A checked but unadmitted candidate must not consume every
                    # following batch. Per-paper AI retries happen inside the
                    # bounded summary attempt, not by restarting source scans.
                    if doi in cooling:
                        continue
                    if len(checked) >= settings.candidate_checks:
                        break
                    checked_dois.add(doi)
                    def save_candidate(value, current_doi=doi):
                        entry = ledger["candidates"].setdefault(current_doi, {"checkedAt": stamp(), "status": "pending",
                            "retryClass": "transient", "reason": "候选阶段已保存；尚未准入"})
                        entry["checkpoint"] = value
                        checkpoint()
                    resume = CandidateResume(root, doi, state.get("checkpoint"), save_candidate, settings, adapters, stamp)
                    active_adapters = resume.wrapped(adapters.get("lookup", lookup_paper))
                    candidate_adapters[candidate["id"]] = active_adapters
                    try:
                        revised = inspect_record(client, new_case(candidate), root, settings, active_adapters, generate_ai=False)
                    except Exception as exc:
                        report["failures"].append({"doi": doi, "stage": "candidate", "error": type(exc).__name__})
                        ledger["candidates"][doi] = dict(ledger["candidates"].get(doi, {}), checkedAt=stamp(), status="pending",
                                                      retryClass="transient", reason="本篇临时失败；未中断其它候选")
                        report["checkedCount"] += 1
                        checked.append({"doi": doi, "skip": True})
                        checkpoint()
                        continue
                    code_evidence = revised.get("article", {}).get("codeURLEvidence", [])
                    declared = {e.get("url", "").rstrip("/") for e in code_evidence}
                    actual_repo = revised.get("repository", {}).get("url", "").rstrip("/")
                    acceptable = (revised["automation"]["stages"].get("metadata") == "matched"
                                  and revised["automation"]["status"] != "excluded"
                                  and matches_scope(revised, settings.research_scope)
                                  and actual_repo in declared and bool(revised.get("repository", {}).get("scripts")))
                    retained_checkpoint = ledger["candidates"].get(doi, {}).get("checkpoint")
                    ledger["candidates"][doi] = {"checkedAt": stamp(), "status": "source_confirmed" if acceptable else "pending",
                                                  "retryClass": "transient" if acceptable or any(
                                                      value in _FAILED_STAGES or value in {"not_checked", "deferred"}
                                                      for value in revised["automation"]["stages"].values()) else "materials",
                                                  "reason": "作者代码声明和可读脚本已定位" if acceptable else "正式文献/作者代码/脚本依据尚不齐全"}
                    if retained_checkpoint:
                        ledger["candidates"][doi]["checkpoint"] = retained_checkpoint
                    checked.append(revised)
                    report["checkedCount"] += 1
                    report["collectionBudgetExhausted"] |= _collection_budget_exhausted(client)
                    checkpoint()
                if (len(available) > len(candidates) and report["checkedCount"] >= settings.max_papers_per_run
                        and report["discoveryStatus"] == "success"):
                    report["discoveryStatus"] = "paper_batch_limit_reached"
                counts = Counter(tag for p in catalog["papers"] for tag in p.get("graphForms", []))
                usable = [p for p in checked if not p.get("skip") and ledger["candidates"][p["doi"]]["status"] == "source_confirmed"]
                usable.sort(key=lambda p: (scope_priority(p, settings.research_scope),
                                           editorial_score(p, counts)["total"]), reverse=True)
                for revised in usable:
                    if len(daily) >= settings.max_new_per_day:
                        break
                    if not matches_scope(revised, settings.research_scope):
                        continue
                    if settings.require_complete_new_cases:
                        # Incomplete candidates are not public admissions and do
                        # not consume today's five-success quota. Paid attempts
                        # are still accounted even when a candidate is rejected.
                        spec = input_spec(revised)
                        source_assessment = bootstrap_assessment(revised, spec, root=root)
                        # Do not spend on annotations for candidates already
                        # lacking required non-AI materials.
                        non_ai_gaps = set(source_assessment["reasonCodes"]) - {
                            "ai_missing", "ai_stale", "ai_configuration_missing",
                            "ai_authentication_failed", "ai_payment_required",
                            "ai_transient_failure", "ai_response_invalid"}
                        if not has_publishable_body_figure(root, revised):
                            non_ai_gaps.add("body_figure_unavailable")
                        # A materials-complete supplement alone is not a visible
                        # new website entry. Use the exact publication gate and
                        # do not pay for AI when no main-text image can appear.
                        if not non_ai_gaps and has_publishable_body_figure(root, revised):
                            try:
                                summarize_case(revised, candidate_adapters[revised["id"]])
                            except Exception:
                                revised.setdefault("automation", {}).setdefault("errors", []).append("summary: error")
                        assessment = bootstrap_assessment(revised, spec, root=root)
                        if assessment["status"] != "complete" or not has_publishable_body_figure(root, revised):
                            temporary_gaps = {"source_transient_failure", "source_refresh_failed", "ai_missing", "ai_stale",
                                "ai_configuration_missing", "ai_authentication_failed", "ai_payment_required",
                                "ai_transient_failure", "ai_response_invalid", "metrics_configuration_missing", "metrics_query_failed",
                                "metrics_unchecked", "request_budget_deferred"}
                            material_gaps = non_ai_gaps - temporary_gaps
                            source_deferred = bool(set(source_assessment["reasonCodes"]) & {
                                "source_transient_failure", "source_refresh_failed", "request_budget_deferred"})
                            # A resolved permitted image that failed in transit
                            # is not evidence that the paper has no body image.
                            failed_download = any(isinstance(figure, dict) and figure.get("downloadStatus") == "failed"
                                and figure.get("reuseStatus") == "allowed"
                                and isinstance(figure.get("sourceURL"), str) and figure["sourceURL"].startswith("https://")
                                for figure in revised.get("article", {}).get("figures", []))
                            retained_checkpoint = ledger["candidates"][revised["doi"]].get("checkpoint")
                            ledger["candidates"][revised["doi"]] = {
                                "checkedAt": stamp(), "status": "pending",
                                "retryClass": "materials" if material_gaps and not source_deferred and not failed_download else "transient",
                                "reason": "可发布正文图、代码、输入或注释不完整；未收录、不占成功名额"}
                            if retained_checkpoint:
                                ledger["candidates"][revised["doi"]]["checkpoint"] = retained_checkpoint
                            report.setdefault("rejectedCandidateCount", 0)
                            report["rejectedCandidateCount"] += 1
                            checkpoint()
                            continue
                    if not matches_scope(revised, settings.research_scope):
                        continue  # A summary adapter cannot change the scope gate.
                    revised["admittedAt"] = stamp()
                    revised["selectionScore"] = editorial_score(revised, counts)
                    catalog["papers"].append(revised)
                    ledger["candidates"][revised["doi"]].pop("checkpoint", None)
                    catalog.setdefault("inputSpecs", {})[revised["id"]] = input_spec(revised)
                    seen.add(revised["doi"])
                    daily.add(revised["doi"])
                    # Preserve admission history (including pre-policy errors),
                    # while only qualified current records occupy target slots.
                    day["newDOIs"] = sorted(set(day["newDOIs"]) | {revised["doi"]})
                    report["newCount"] += 1
                    checked_ids.add(revised["id"])
                    original_schedules[revised["id"]] = {}
                    # Durable successful admission before advancing to another
                    # candidate. Legacy library mode can still repair paid work.
                    checkpoint()
                    if not settings.require_complete_new_cases:
                        try:
                            summarize_case(revised, candidate_adapters[revised["id"]])
                        except Exception as exc:
                            failed_ids.add(revised["id"])
                            revised["automation"]["errors"].append("summary: " + type(exc).__name__)
                    _update_repair_schedule(revised, settings, {}, revised["automation"]["lastCheckedAt"], revised["id"] in failed_ids)
                    ledger["candidates"][revised["doi"]].pop("checkpoint", None)
                    checkpoint()
            except Exception as exc:
                report["discoveryStatus"] = "partial_failure"
                report["failures"].append({"stage": "discovery", "error": type(exc).__name__})
        # Checked AI backlog was already serviced; new cases precede fresh-seed AI.
        for record in catalog["papers"]:
            if record["id"] not in repaired_ids or record["id"] in summarized_ids or not _can_summarize(record):
                continue
            try:
                summarize_case(record)
                checkpoint()
            except Exception as exc:
                failed_ids.add(record["id"])
                report["failures"].append({"doi": record["doi"], "stage": "summary", "error": type(exc).__name__})
            _update_repair_schedule(record, settings, original_schedules.get(record["id"], {}), record["automation"]["lastCheckedAt"], record["id"] in failed_ids)
        catalog["generatedAt"] = stamp()
        catalog["checkedAt"] = report["date"]
        if settings.auto_remove_uncollectable:
            from .pruning import prune_uncollectable
            report["removedPapers"] = prune_uncollectable(catalog, ledger, root, stamp())
            report["removedCount"] = report["removedPapers"]["removedCount"]
        if initialization:
            after = bootstrap_report(catalog, ledger, root=root, now=stamp())
            catalog["bootstrap"] = after
            report["bootstrap"] = after
            if is_backfill:
                report["discoveryStatus"] = ("bootstrap_complete_next_run" if after["status"] == "complete"
                                             else "bootstrap_needs_attention" if after["status"] == "needs_attention"
                                             else "bootstrap_in_progress")
        checked_records = [p for p in catalog["papers"] if p["id"] in checked_ids]
        outcomes = Counter(_automatic_outcome(p, settings) for p in checked_records)
        report["automaticCompleteCount"] = outcomes["automatic_complete"]
        report["incompleteCount"] = outcomes["incomplete"]
        report["untouchedRemainingCount"] = sum(_untouched(p) for p in catalog["papers"])
        report["checkedDOIs"] = checked_doi_order
        for record in checked_records:
            for error in record.get("automation", {}).get("errors", []):
                report["failures"].append(_stage_failure(record, error))
        report["stageCounts"] = {stage: dict(Counter(
            p.get("automation", {}).get("stages", {}).get(stage, "not_checked") for p in checked_records))
                                 for stage in ("metadata", "article", "repository", "metrics", "ai")}
        report["missingKeys"] = [key for key, value in (("LLM_API_KEY", settings.llm_api_key), ("LLM_MODEL", settings.llm_model), ("EASYSCHOLAR_SECRET_KEY", settings.easyscholar_key)) if not value]
        report["aiCasesAttempted"] = sum(count > 0 for count in summary_attempts.values())
        report["aiRetriedCaseCount"] = sum(count > 1 for count in summary_attempts.values())
        report["aiRetrySuccessCount"] = sum(
            summary_attempts[item["id"]] > 1 and item.get("ai", {}).get("status") == "generated"
            and item.get("automation", {}).get("stages", {}).get("ai") == "generated"
            for item in catalog["papers"] if item["id"] in summary_attempts)
        stopped = getattr(client, "ai_stopped_status", "")
        if isinstance(stopped, str) and stopped in _AI_RUN_FATAL:
            report["aiStoppedStatus"] = stopped
        report.update(finishedAt=stamp(), requests=getattr(client, "requests", 0), aiAttempts=getattr(client, "post_attempts", 0),
                      collectionRequests=max(0, getattr(client, "requests", 0) - getattr(client, "post_attempts", 0)),
                      totalCount=len(catalog["papers"]), dailyNewCount=len(daily),
                      status="partial" if report["failures"] or report["collectionBudgetExhausted"] or outcomes["deferred"] or outcomes["incomplete"] or (is_backfill and report["bootstrap"]["status"] != "complete") else "completed")
        reports = [report] + reports[:89]
        checkpoint()
        write_json(root / "data" / "updates.json", reports)
        build_site(root, catalog, reports)
    return report
