"""Deterministic research eligibility, separate from figure/editorial ranking.

Only publication titles/abstracts are evidence. AI prose, journal names, curated
tags and code READMEs cannot turn an unrelated paper into an eligible result.
"""
from __future__ import annotations

import hashlib
import html
import re


POLICY = "aging-sequencing-metabolic"
POLICIES = frozenset({"unrestricted", POLICY})
SCOPE_LABEL = "衰老测序与多组学优先；兼顾代谢相关组学和可迁移组学方法工具"
SCOPE_VERSION = 3

_METHODS = {
    "long_read": r"\b(?:long[\s-]?reads?|third[\s-]generation\s+sequencing|nanopore|pacbio|smrt|hifi)\b|三代测序|长读长|纳米孔",
    "short_read": r"\b(?:next[\s-]generation\s+sequencing|second[\s-]generation\s+sequencing|short[\s-]?reads?|ngs|illumina)\b|二代测序|短读长",
    "sequencing": r"\b(?:sequencing|(?:sc|sn|bulk)?(?:rna|dna|atac|chip|bisulfite)[\s-]?seq|wgs|wes|wgbs)\b|测序",
    "multiomics": r"\b(?:multi[\s-]?omics?|integrative\s+omics|integrated\s+omics)\b|多组学|组学整合",
    "omics": r"\b(?:genom(?:e|es|ics?)|transcriptom(?:e|es|ics?)|epigenom(?:e|es|ics?)|methylom(?:e|es|ics?)|proteom(?:e|es|ics?)|metabolom(?:e|es|ics?))\b|基因组|转录组|表观组|甲基化组|蛋白组|代谢组",
    "epigenetic_profile": r"\b(?:(?:dna|cpg)\s+(?:methylation|hydroxymethylation)|(?:genome[\s-]wide|whole[\s-]genome)\s+methylation|chromatin\s+(?:accessibility|states?))\b|DNA甲基化|DNA羟甲基化",
    "single_cell_profile": r"(?s)(?=.*\bsingle[\s-](?:cells?|nucle(?:us|i))\b)(?=.*\b(?:genes?|rna|transcriptom\w*)\b)(?=.*\b(?:atlas|profiled|profiling)\b).+",
    "regulatory_network": r"\b(?:gene[\s-]regulatory\s+networks?|regulon\s+(?:inference|analysis))\b|基因调控网络",
}
_TOOL_MARKER = "method_tool"
# A generic mention of methods, analysis, AI or visualization is not a methods
# contribution. Require a named kind of reusable tool in the publication title,
# or an explicit contribution statement in the formal abstract.
_TOOL_TITLE = re.compile(
    r"\b(?:toolkits?|software(?:\s+packages?)?|frameworks?|algorithms?|computational\s+methods?|"
    r"(?:deep\s+)?generative\s+models?|(?:statistical|probabilistic)\s+models?|"
    r"(?:scalable|integrative|integrated|automated|accurate|efficient)\s+(?:analysis|inference|integration|reconstruction)|"
    r"methods?\s+(?:for|to)\b)|"
    r"工具包|软件|算法|计算方法|整合框架", re.I)
_TOOL_ABSTRACT = re.compile(
    r"\b(?:we\s+(?:here\s+)?(?:introduce|present|develop|propose|provide|implement)|"
    r"here[,\s]+we\s+(?:introduce|present|develop|propose|provide)|"
    r"we\s+(?:have\s+)?developed)\b.{0,180}\b(?:tools?|toolkits?|software|packages?|"
    r"frameworks?|methods?|algorithms?|models?|approaches?|pipelines?|workflows?|platforms?)\b|"
    r"我们(?:提出|开发|构建).{0,80}(?:工具|软件|算法|框架|方法)", re.I)
_NAMED_CONTRIBUTION = re.compile(
    r"\bwe\s+(?:here\s+)?(?:introduce|present|develop|propose|provide|implement)\s+"
    r"([A-Za-z][A-Za-z0-9+_.-]{2,40})(?=\s|[,(.:;]|$)", re.I)
_COMPUTATIONAL_EVIDENCE = re.compile(
    r"\b(?:computational\s+(?:trajectory\s+)?(?:inference|method|approach|framework)|"
    r"non[\s-]parametric|graph[\s-]based\s+test|algorithms?|software|source\s+code|"
    r"implementation|generative\s+models?|probabilistic\s+models?|statistical\s+testing)\b", re.I)


def _tool_contribution(title, abstract):
    if _TOOL_TITLE.search(title) or _TOOL_ABSTRACT.search(abstract):
        return True
    # Experimental sequencing methods are transferable methodology too, not
    # necessarily software. Require sequencing as the developed object, not
    # merely the data used to develop a disease-specific score or hypothesis.
    for match in re.finditer(
            r"\bwe\s+(?:have\s+)?(?:develop(?:ed)?|introduce[ds]?)\s+([^.!?]{1,140}?)\bsequencing\b",
            abstract, re.I):
        if not re.search(r"\b(?:using|with|via|by|based\s+on)\b", match.group(1), re.I):
            return True
    # Named tools often put the methodological description in the following
    # sentence. A repeated mixed-case name plus computational evidence covers
    # that grammatical form without a tool-brand or DOI allowlist.
    for match in _NAMED_CONTRIBUTION.finditer(abstract):
        name = match.group(1).rstrip(".")
        if (any(char.isupper() for char in name[1:]) and any(char.islower() for char in name)
                and len(re.findall(r"(?<!\w)" + re.escape(name) + r"(?!\w)", abstract)) >= 2
                and _COMPUTATIONAL_EVIDENCE.search(abstract)):
            return True
    # A formal comparison of analysis methods is also reusable methodology;
    # an ordinary disease-discovery title merely saying "integration" is not.
    return bool(re.search(r"\bbenchmark(?:ing|s)?\b", title, re.I)
                and re.search(r"\bmethods?\b", abstract, re.I)
                and re.search(r"\b(?:benchmarked|evaluated|compared|benchmarking)\b", abstract, re.I))
_TOPICS = {
    "aging": r"\b(?:aging|ageing|age[\s-](?:related|associated)|senescen(?:ce|t)|longevity|life[\s-]?span|geroscience|geroprotect\w*|biological\s+age|epigenetic\s+clocks?|aged\s+(?:mice|mouse|rats?|animals?|tissues?|cells?))\b|衰老|老龄|寿命|生物学年龄|表观遗传时钟",
    "diabetes": r"\b(?:diabet(?:es|ic)|prediabet(?:es|ic)|t1d|t2d|insulin[\s-]resistan(?:ce|t))\b|糖尿病|胰岛素抵抗",
    "metabolic_disease": r"\b(?:obesity|obese|metabolic\s+(?:diseases?|disorders?|syndrome|dysfunction)|nafld|masld|non[\s-]alcoholic\s+fatty\s+liver)\b|肥胖|代谢性疾病|代谢病|代谢综合征|代谢紊乱|脂肪肝",
}
_NON_BIOMEDICAL = re.compile(
    r"\b(?:marine|coral|reef|biodiversity|phytoplankton|zooplankton|soil|crops?|plants?|arabidopsis|rice|wheat|maize|aquaculture|environmental\s+dna)\b|海洋|珊瑚|农作物|植物", re.I)
_BIOMEDICAL = re.compile(
    r"\b(?:humans?|patients?|mammal\w*|mice|mouse|murine|rats?|clinical|pancrea\w*|islets?|adipose|caenorhabditis|elegans|drosophila|yeast|saccharomyces)\b|人类|患者|小鼠|大鼠|线虫|果蝇|酵母", re.I)


def _text(value):
    value = value if isinstance(value, str) else ""
    value = value.translate(str.maketrans({c: "-" for c in "‐‑‒–—−"}))
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value[:100000])).split())


def _binding(record):
    # A retained decision cannot be reused after the DOI/title is changed.
    value = str(record.get("doi") or "").lower() + "\n" + _text(record.get("paper"))
    return hashlib.sha256(value.encode()).hexdigest()


def _saved(record, policy):
    raw = record.get("researchScope")
    if not isinstance(raw, dict):
        return None
    keys = {"version", "policy", "status", "tier", "reason", "methodMatches", "topicMatches", "source", "titleDOIHash"}
    if (set(raw) != keys or type(raw.get("version")) is not int or raw["version"] not in {1, 2, SCOPE_VERSION}
            or raw.get("policy") != policy or raw.get("source") != "verified-metadata"
            or raw.get("titleDOIHash") != _binding(record)):
        return None
    # Earlier exclusions did not recognize transferable omics tools. They must
    # be looked up again, rather than permanently freezing the old decision.
    if raw["version"] < SCOPE_VERSION and raw.get("status") != "included":
        return None
    if raw["version"] < SCOPE_VERSION and (raw.get("tier") == "tools" or (
            _NON_BIOMEDICAL.search(_text(record.get("paper")))
            and not _BIOMEDICAL.search(_text(record.get("paper"))))):
        return None  # New contribution and biomedical checks need fresh evidence.
    methods, topics = raw.get("methodMatches"), raw.get("topicMatches")
    if (not isinstance(methods, list) or not isinstance(topics, list)
            or any(not isinstance(v, str) or v not in {*_METHODS, _TOOL_MARKER} for v in methods)
            or any(not isinstance(v, str) or v not in _TOPICS for v in topics)
            or len(set(methods)) != len(methods) or len(set(topics)) != len(topics)):
        return None
    omics_methods = [method for method in methods if method != _TOOL_MARKER]
    expected_tier = "primary" if "aging" in topics else "expansion" if topics else "tools"
    if raw.get("status") == "included":
        if (raw.get("reason") != "eligible" or not omics_methods or raw.get("tier") != expected_tier
                or (not topics and (raw["version"] < 2 or _TOOL_MARKER not in methods))):
            return None
    elif raw.get("status") == "excluded":
        if raw.get("tier") != "none" or raw.get("reason") not in {
                "non_biomedical", "topic_not_supported", "omics_not_supported"}:
            return None
        if (raw["reason"] == "topic_not_supported" and topics
                or raw["reason"] == "omics_not_supported" and (omics_methods or not topics)):
            return None
    else:
        return None
    return {**raw, "methodMatches": list(methods), "topicMatches": list(topics)}


def assess_scope(record, policy=POLICY, *, allow_saved=True, source="metadata"):
    if policy not in POLICIES:
        raise ValueError("未知研究领域策略")
    if policy == "unrestricted":
        return {"status": "included", "tier": "unrestricted", "methodMatches": [], "topicMatches": []}
    # Persistent catalogues deliberately omit full abstracts. Retain only a
    # narrowly validated decision made after an exact-DOI metadata lookup.
    if allow_saved and "abstract" not in record:
        saved = _saved(record, policy)
        if saved is not None:
            return saved
    title = _text(record.get("paper"))
    text = title + "\n" + _text(record.get("abstract"))
    methods = [key for key, pattern in _METHODS.items() if re.search(pattern, text, re.I)]
    topics = [key for key, pattern in _TOPICS.items() if re.search(pattern, text, re.I)]
    tool = bool(methods and _tool_contribution(title, _text(record.get("abstract"))))
    if tool:
        methods.append(_TOOL_MARKER)
    reason = "eligible"
    if _NON_BIOMEDICAL.search(title) and not _BIOMEDICAL.search(title):
        reason = "non_biomedical"
    elif not topics and not tool:
        reason = "topic_not_supported"
    elif not methods:
        reason = "omics_not_supported"
    return {"version": SCOPE_VERSION, "policy": policy, "status": "included" if reason == "eligible" else "excluded",
            "tier": ("primary" if "aging" in topics else "expansion" if topics else "tools") if reason == "eligible" else "none",
            "reason": reason, "methodMatches": methods, "topicMatches": topics,
            "source": source, "titleDOIHash": _binding(record)}


def matches_scope(record, policy=POLICY):
    return assess_scope(record, policy)["status"] == "included"


def has_verified_scope(record, policy=POLICY):
    if policy == "unrestricted":
        return True
    saved = _saved(record, policy)
    if saved is None:
        return False
    if "abstract" in record:
        current = assess_scope(record, policy, allow_saved=False)
        if any(current[key] != saved[key] for key in ("status", "tier", "reason", "methodMatches", "topicMatches")):
            return False
    return True


def scope_priority(record, policy=POLICY):
    """Domain priority does not replace fair per-batch access for tools."""
    assessment = assess_scope(record, policy)
    return {"primary": 3, "expansion": 2, "tools": 1}.get(assessment["tier"], 0)


def select_scope_candidates(records, limit, policy=POLICY):
    """Stable bounded selection; reserve access to tools and disease expansion.

    The primary direction stays first, but a full primary page cannot prevent
    the other eligible lanes from ever entering an actual processing batch.
    Missing lanes donate their slots; no candidate is duplicated or invented.
    """
    limit = max(0, int(limit))
    ordered = sorted(records, key=lambda record: scope_priority(record, policy), reverse=True)
    if not limit or policy == "unrestricted":
        return ordered[:limit]
    buckets = {tier: [] for tier in ("primary", "expansion", "tools")}
    seen = set()
    for record in ordered:
        doi = str(record.get("doi") or "").lower()
        assessment = assess_scope(record, policy)
        if not doi or doi in seen or assessment["status"] != "included":
            continue
        seen.add(doi)
        buckets[assessment["tier"]].append(record)
    selected = []
    # At a five-paper batch this is 3 primary + 1 expansion + 1 tools when
    # all are available. Two-paper batches prefer primary + tools.
    reserve_tools = int(limit >= 2 and bool(buckets["tools"]))
    reserve_expansion = int(limit >= 3 and bool(buckets["expansion"]))
    for tier, count in (("primary", limit - reserve_tools - reserve_expansion),
                        ("expansion", reserve_expansion), ("tools", reserve_tools)):
        selected.extend(buckets[tier][:count])
        del buckets[tier][:count]
    for tier in ("primary", "expansion", "tools"):
        selected.extend(buckets[tier][:limit - len(selected)])
    return sorted(selected, key=lambda record: scope_priority(record, policy), reverse=True)[:limit]


def eligible_daily_dois(catalog, ledger_dois, day, policy="unrestricted"):
    if policy not in POLICIES:
        raise ValueError("未知研究领域策略")
    counted = set(ledger_dois)
    counted.update(p["doi"] for p in catalog["papers"]
                   if isinstance(p.get("admittedAt"), str) and p["admittedAt"].startswith(day))
    if policy == "unrestricted":
        return counted
    qualified = set()
    for paper in catalog["papers"]:
        if paper["doi"] not in counted:
            continue
        assessment = assess_scope(paper, policy)
        if assessment["status"] == "included" and has_verified_scope(paper, policy):
            qualified.add(paper["doi"])
    return qualified


def query_constraint(provider="epmc", tier=None):
    """Use title/abstract fields, not mentions buried in references/full text."""
    primary = ('aging OR ageing OR senescence OR senescent OR longevity OR lifespan OR "life span" '
               'OR "age related" OR "age associated" OR geroscience OR "biological age" OR "epigenetic clock"')
    expansion = ('diabetes OR diabetic OR prediabetes OR prediabetic OR T1D OR T2D '
                 'OR "insulin resistance" OR "insulin resistant" OR obesity OR obese OR "metabolic disease" '
                 'OR "metabolic disorder" OR "metabolic syndrome" OR "metabolic dysfunction" '
                 'OR NAFLD OR MASLD OR "non alcoholic fatty liver"')
    tools = ('toolkit OR software OR framework OR algorithm OR "computational method" OR "generative model" '
             'OR "sequencing method" OR "sequencing assay" OR "sequencing protocol" OR developed '
             'OR integration OR inference OR reconstruction OR mapping OR benchmarking')
    topic = primary if tier == "primary" else expansion if tier == "expansion" else tools if tier == "tools" else primary + " OR " + expansion + " OR " + tools
    method = ('sequencing OR nanopore OR PacBio OR "long read" OR "short read" OR NGS '
              'OR "multi omics" OR multiomics OR genome OR genomics OR transcriptome OR transcriptomics '
              'OR epigenome OR epigenomics OR methylome OR proteome OR proteomics OR metabolome '
              'OR metabolomics OR methylation OR "single cell" OR "single nucleus" '
              'OR "DNA methylation" OR "RNA seq" OR "ATAC seq" OR "scRNA-seq" OR "snRNA-seq" '
              'OR "gene regulatory network" OR "spatial transcriptomics"')
    if provider == "ncbi":
        def tagged(expression):
            return "(" + " OR ".join(term + "[Title/Abstract]" for term in expression.split(" OR ")) + ")"
        return tagged(topic) + " AND " + tagged(method)
    return "TITLE_ABS:(" + topic + ") AND TITLE_ABS:(" + method + ")"
