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
SCOPE_LABEL = "衰老测序与多组学优先；扩展糖尿病等代谢相关组学"

_METHODS = {
    "long_read": r"\b(?:long[\s-]?reads?|third[\s-]generation\s+sequencing|nanopore|pacbio|smrt|hifi)\b|三代测序|长读长|纳米孔",
    "short_read": r"\b(?:next[\s-]generation\s+sequencing|second[\s-]generation\s+sequencing|short[\s-]?reads?|ngs|illumina)\b|二代测序|短读长",
    "sequencing": r"\b(?:sequencing|(?:sc|sn|bulk)?(?:rna|dna|atac|chip|bisulfite)[\s-]?seq|wgs|wes|wgbs)\b|测序",
    "multiomics": r"\b(?:multi[\s-]?omics?|integrative\s+omics|integrated\s+omics)\b|多组学|组学整合",
    "omics": r"\b(?:genom(?:e|es|ics?)|transcriptom(?:e|es|ics?)|epigenom(?:e|es|ics?)|methylom(?:e|es|ics?)|proteom(?:e|es|ics?)|metabolom(?:e|es|ics?))\b|基因组|转录组|表观组|甲基化组|蛋白组|代谢组",
    "epigenetic_profile": r"\b(?:(?:dna|cpg)\s+(?:methylation|hydroxymethylation)|(?:genome[\s-]wide|whole[\s-]genome)\s+methylation|chromatin\s+(?:accessibility|states?))\b|DNA甲基化|DNA羟甲基化",
    "single_cell_profile": r"(?s)(?=.*\bsingle[\s-](?:cells?|nucle(?:us|i))\b)(?=.*\b(?:genes?|rna|transcriptom\w*)\b)(?=.*\b(?:atlas|profiled|profiling)\b).+",
}
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
    if (set(raw) != keys or type(raw.get("version")) is not int or raw["version"] != 1
            or raw.get("policy") != policy or raw.get("source") != "verified-metadata"
            or raw.get("titleDOIHash") != _binding(record)):
        return None
    methods, topics = raw.get("methodMatches"), raw.get("topicMatches")
    if (not isinstance(methods, list) or not isinstance(topics, list)
            or any(not isinstance(v, str) or v not in _METHODS for v in methods)
            or any(not isinstance(v, str) or v not in _TOPICS for v in topics)
            or len(set(methods)) != len(methods) or len(set(topics)) != len(topics)):
        return None
    expected_tier = "primary" if "aging" in topics else "expansion"
    if raw.get("status") == "included":
        if raw.get("reason") != "eligible" or not methods or not topics or raw.get("tier") != expected_tier:
            return None
    elif raw.get("status") == "excluded":
        if raw.get("tier") != "none" or raw.get("reason") not in {
                "non_biomedical", "topic_not_supported", "omics_not_supported"}:
            return None
        if (raw["reason"] == "topic_not_supported" and topics
                or raw["reason"] == "omics_not_supported" and (methods or not topics)):
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
    reason = "eligible"
    if _NON_BIOMEDICAL.search(title) and not _BIOMEDICAL.search(text):
        reason = "non_biomedical"
    elif not topics:
        reason = "topic_not_supported"
    elif not methods:
        reason = "omics_not_supported"
    return {"version": 1, "policy": policy, "status": "included" if reason == "eligible" else "excluded",
            "tier": ("primary" if "aging" in topics else "expansion") if reason == "eligible" else "none",
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
    """All eligible aging work precedes disease expansion, regardless of IF."""
    assessment = assess_scope(record, policy)
    return {"primary": 2, "expansion": 1}.get(assessment["tier"], 0)


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
    topic = primary if tier == "primary" else expansion if tier == "expansion" else primary + " OR " + expansion
    method = ('sequencing OR nanopore OR PacBio OR "long read" OR "short read" OR NGS '
              'OR "multi omics" OR multiomics OR genome OR genomics OR transcriptome OR transcriptomics '
              'OR epigenome OR epigenomics OR methylome OR proteome OR proteomics OR metabolome '
              'OR metabolomics OR methylation OR "single cell" OR "single nucleus" '
              'OR "DNA methylation" OR "RNA seq" OR "ATAC seq" OR "scRNA-seq" OR "snRNA-seq"')
    if provider == "ncbi":
        def tagged(expression):
            return "(" + " OR ".join(term + "[Title/Abstract]" for term in expression.split(" OR ")) + ")"
        return tagged(topic) + " AND " + tagged(method)
    return "TITLE_ABS:(" + topic + ") AND TITLE_ABS:(" + method + ")"
