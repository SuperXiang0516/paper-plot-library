"""Bounded literature metadata discovery; full-text retrieval lives in figures.py."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import re
from urllib.parse import quote
import xml.etree.ElementTree as ET

from .scope import POLICIES, assess_scope, matches_scope, query_constraint, select_scope_candidates

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
DEFAULT_QUERY = ('(genomics OR "single cell" OR "multi-omics" OR bioinformatics) '
                 'AND ("source code" OR GitHub OR "code availability") '
                 'AND HAS_DOI:Y AND SRC:MED NOT PUB_TYPE:"Review"')


def normalize_doi(value):
    value = str(value or "").strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I).lower()
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s<>]+", value) else ""


def paper_id(doi):
    return "doi-" + hashlib.sha256(normalize_doi(doi).encode()).hexdigest()[:16]


def _text(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _paper(row):
    doi = normalize_doi(row.get("doi"))
    journal = row.get("journalInfo", {}).get("journal", {})
    abstract = re.sub(r"<[^>]+>", " ", row.get("abstractText") or "")
    publication_types = row.get("pubTypeList", {}).get("pubType", [])
    title = re.sub(r"<[^>]+>", "", row.get("title") or "")
    pmcid = str(row.get("pmcid") or "")
    result = {
        "id": paper_id(doi), "paper": title, "doi": doi,
        "journal": journal.get("title") or row.get("journalTitle") or "",
        "date": row.get("firstPublicationDate") or str(row.get("pubYear") or ""),
        "year": str(row.get("pubYear") or ""), "paperURL": "https://doi.org/" + doi,
        "pmcid": pmcid, "pmid": str(row.get("id") or ""),
        "abstract": " ".join(abstract.split()), "publicationTypes": publication_types,
        "issn": journal.get("issn") or "", "eissn": journal.get("essn") or "",
        "metadataSource": "Europe PMC", "metadataURL": EPMC,
        "citedByCount": int(row.get("citedByCount") or 0),
        "citationSource": "Europe PMC", "citationRetrievedAt": date.today().isoformat(),
        "codeURLs": sorted(set(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+", abstract))),
    }
    result["isPreprint"] = row.get("source") == "PPR" or any("preprint" in str(t).lower() for t in publication_types)
    result["isRetracted"] = row.get("isRetracted") in (True, "Y", "yes") or any(
        "retract" in str(t).lower() for t in publication_types)
    return result


def _usable(record):
    types = record.get("publicationTypes") or []
    if isinstance(types, str):
        types = [types]
    excluded_type = any(any(term in str(value).lower() for term in (
        "review", "editorial", "comment", "retract", "preprint", "conference abstract")) for value in types)
    return bool(record.get("doi") and record.get("paper") and not record.get("isPreprint")
                and not record.get("isRetracted") and not excluded_type)


def _utc_today():
    return datetime.now(timezone.utc).date()


def _published_not_future(record, today=None):
    """Year-only dates are uncertain, not invented; known future dates excluded."""
    today = today or _utc_today()
    value = str(record.get("date") or record.get("year") or "")
    match = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:$|T)", value)
    if not match:
        return False
    year, month, day = match.groups()
    try:
        # Earliest possible day handles partial dates without pretending exact
        # publication dates were supplied. Future year/month remains excluded.
        earliest = date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return False
    return earliest <= today


def _research_policy(settings):
    policy = getattr(settings, "research_scope", "unrestricted")
    if policy not in POLICIES:
        raise ValueError("未知研究领域策略")
    return policy


def lookup_scope_metadata(client, papers):
    """Resolve at most 50 existing DOIs in one bounded metadata-only request.

    Missing or conflicting matches are not guessed, and no per-paper fallback
    can turn a scope migration into article downloads or an unbounded scan.
    """
    requested = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        doi = normalize_doi(paper.get("doi"))
        # DOI normalization is not query escaping. Reject quote/backslash
        # syntax rather than letting a suffix alter this fixed DOI-only query.
        if (not doi or len(doi) > 512 or any(char in doi for char in ('"', "\\"))
                or any(ord(char) < 32 or ord(char) == 127 for char in doi)):
            continue
        if doi not in requested:
            requested.append(doi)
        if len(requested) == 50:
            break
    if not requested:
        return {}
    response = client.get_json(EPMC, params={"query": " OR ".join('DOI:"' + doi + '"' for doi in requested),
        "format": "json", "resultType": "core", "pageSize": 50}, max_bytes=4000000)
    if not isinstance(response, dict):
        return {}
    result_list = response.get("resultList")
    rows = result_list.get("result") if isinstance(result_list, dict) else None
    if not isinstance(rows, list):
        return {}
    today, resolved, conflicts = _utc_today(), {}, set()
    for row in rows[:50]:
        if not isinstance(row, dict):
            continue
        doi = normalize_doi(row.get("doi"))
        if doi not in requested or doi in conflicts:
            continue
        try:
            record = _paper(row)
            if not _usable(record) or not _published_not_future(record, today):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        previous = resolved.get(doi)
        if previous is not None:
            identity = lambda value: (" ".join(value["paper"].split()).casefold(), value["abstract"])
            if identity(previous) != identity(record):
                resolved.pop(doi)
                conflicts.add(doi)
                continue
            if previous.get("pmcid") or not record.get("pmcid"):
                continue
        record["status"] = "matched"
        resolved[doi] = record
    return resolved


def lookup_paper(client, doi):
    """Exact DOI match, Europe PMC first and formal Crossref metadata fallback."""
    doi = normalize_doi(doi)
    if not doi:
        return {"status": "invalid_doi", "doi": ""}
    epmc_error = None
    try:
        response = client.get_json(EPMC, params={"query": 'DOI:"' + doi + '"',
                                  "format": "json", "resultType": "core", "pageSize": 10})
        rows = response.get("resultList", {}).get("result", [])
        matches = [_paper(row) for row in rows if normalize_doi(row.get("doi")) == doi]
        matches.sort(key=lambda r: (r["isPreprint"], not bool(r["pmcid"])))
        if matches:
            result = matches[0]
            result["status"] = "matched" if _usable(result) else "excluded"
            return result
    except Exception as exc:
        epmc_error = type(exc).__name__
    try:
        row = client.get_json("https://api.crossref.org/works/" + quote(doi, safe=""))["message"]
        if normalize_doi(row.get("DOI")) != doi:
            return {"status": "doi_mismatch", "doi": doi}
        if row.get("type") != "journal-article":
            return {"status": "excluded", "doi": doi, "publicationTypes": [row.get("type", "unknown")]}
        parts = (row.get("published") or row.get("published-print") or row.get("published-online") or {}).get("date-parts", [[]])[0]
        pubdate = "-".join(str(n).zfill(4 if i == 0 else 2) for i, n in enumerate(parts))
        result = {"id": paper_id(doi), "doi": doi, "paper": (row.get("title") or [""])[0],
                  "journal": (row.get("container-title") or [""])[0], "date": pubdate,
                  "year": str(parts[0]) if parts else "", "paperURL": "https://doi.org/" + doi,
                  "pmcid": "", "abstract": "", "publicationTypes": ["journal-article"],
                  "metadataSource": "Crossref", "status": "matched", "isPreprint": False,
                  "isRetracted": False, "codeURLs": [], "limitations": ["PMC full text not resolved"]}
        if epmc_error:
            result["limitations"].append("Europe PMC lookup unavailable: " + epmc_error)
        return result
    except Exception:
        return {"status": "metadata_unavailable", "doi": doi}


def _ncbi_discover(client, settings, limit, exclude_dois=frozenset()):
    """NCBI fallback; bounded PubMed metadata, not article/PDF scraping."""
    policy = _research_policy(settings)
    default_term = ('(genomics OR "single cell" OR "multi omics" OR bioinformatics) '
                    'AND (github OR "source code") NOT review[Publication Type]')
    if policy != "unrestricted":
        # Scope and method terms below determine topical eligibility. Code
        # availability is enrichment and must not block discovery.
        default_term = '(aging OR ageing OR senescence OR longevity OR diabetes OR metabolic) NOT review[Publication Type]'
    term = getattr(settings, "ncbi_query", None) or default_term
    if policy != "unrestricted":
        # A custom/fallback query may narrow the field, never remove its gate.
        term = "(" + term + ") AND (" + query_constraint("ncbi") + ")"
    today = _utc_today()
    term = "(" + term + ') AND ("1900/01/01"[Date - Publication] : "' + today.strftime("%Y/%m/%d") + '"[Date - Publication])'
    params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": limit, "sort": "pub date", "tool": "paper_plot_library"}
    if getattr(settings, "ncbi_api_key", ""):
        params["api_key"] = settings.ncbi_api_key
    if getattr(settings, "ncbi_email", ""):
        params["email"] = settings.ncbi_email
    response = client.get_json(NCBI + "esearch.fcgi", params=params)
    ids = response.get("esearchresult", {}).get("idlist", [])[:limit]
    if not ids:
        return []
    params = {key: value for key, value in params.items() if key in {"db", "tool", "api_key", "email"}}
    params.update({"id": ",".join(ids), "retmode": "xml"})
    xml = client.get_text(NCBI + "efetch.fcgi", params=params, max_bytes=4000000)
    tree = ET.fromstring(xml)
    result = []
    for node in tree.findall(".//PubmedArticle"):
        article = node.find(".//Article")
        doi = normalize_doi(next((_text(n) for n in node.findall(".//ArticleId") if n.get("IdType") == "doi"), ""))
        if not doi or article is None:
            continue
        types = [_text(n) for n in article.findall(".//PublicationType")]
        year = _text(article.find(".//PubDate/Year"))
        record = {"id": paper_id(doi), "doi": doi, "paper": _text(article.find("ArticleTitle")),
                  "journal": _text(article.find("Journal/Title")), "date": year, "year": year,
                  "paperURL": "https://doi.org/" + doi,
                  "pmid": _text(node.find(".//PMID")),
                  "pmcid": next((_text(n) for n in node.findall(".//ArticleId") if n.get("IdType") == "pmc"), ""),
                  "abstract": " ".join(_text(n) for n in article.findall(".//AbstractText")),
                  "publicationTypes": types, "metadataSource": "NCBI PubMed",
                  "isPreprint": any("preprint" in t.lower() for t in types),
                  "isRetracted": any("retract" in t.lower() for t in types)}
        record["codeURLs"] = sorted(set(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+", record["abstract"])))
        if (_usable(record) and _published_not_future(record, today)
                and matches_scope(record, policy)):
            result.append(record)
    result = [record for record in result if record["doi"] not in exclude_dois]
    return select_scope_candidates(result, limit, policy)


def discover(client, settings, exclude_dois=None):
    """Return bounded unseen candidates, paging past known/cooling papers.

    Europe PMC documents cursorMark/nextCursorMark pagination. Never follow a
    provider-supplied URL: only the opaque cursor is sent to the fixed endpoint.
    At most five metadata pages are inspected per batch, within HTTP budgets.
    """
    policy = _research_policy(settings)
    limit = min(100, max(1, int(getattr(settings, "discovery_limit", 30))))
    excluded = frozenset(normalize_doi(value) for value in (exclude_dois or ()))
    query = getattr(settings, "discovery_query", None) or (
        DEFAULT_QUERY if policy == "unrestricted" else '("source code" OR GitHub OR "code availability")')
    today = _utc_today()
    # Filter BEFORE newest-first pagination. Filtering future rows only after
    # retrieval would allow forthcoming Nov/Dec issues to starve older papers.
    # Eligibility is enforced at the server too: otherwise DOI-less PMC-only
    # records or reviews can occupy the newest-first page before local filtering.
    query = ("(" + query + ') AND HAS_DOI:Y AND SRC:MED AND NOT PUB_TYPE:"Review"'
             " AND FIRST_PDATE:[1900-01-01 TO " + today.isoformat() + "]")
    unique = {}
    # Scope is a mandatory AND constraint even when the query is customized.
    # Each lane is queried even if a primary page fills the metadata limit.
    # Two aging, one metabolic and two tool pages share the old five-page cap.
    # Known/cooling and off-topic rows never take successful candidate slots.
    lanes = [(None, 5)] if policy == "unrestricted" else [("primary", 2), ("expansion", 1), ("tools", 2)]
    try:
        for tier, page_limit in lanes:
            lane_query = query if tier is None else "(" + query + ") AND (" + query_constraint("epmc", tier) + ")"
            cursors, cursor = set(), "*"
            lane_count = 0
            for _ in range(page_limit):
                cursors.add(cursor)
                response = client.get_json(EPMC, params={"query": lane_query, "format": "json", "resultType": "core",
                    "pageSize": limit, "sort": "FIRST_PDATE_D desc", "cursorMark": cursor}, ttl=3600)
                rows = response.get("resultList", {}).get("result", [])
                if not isinstance(rows, list):
                    raise ValueError("Invalid metadata page")
                for row in rows[:limit]:
                    if not isinstance(row, dict):
                        continue
                    record = _paper(row)
                    assessment = assess_scope(record, policy)
                    if (record["doi"] not in excluded and _usable(record)
                            and _published_not_future(record, today) and assessment["status"] == "included"
                            and (tier is None or assessment["tier"] == tier)):
                        lane_count += int(record["doi"] not in unique)
                        unique.setdefault(record["doi"], record)
                if lane_count >= limit:
                    break
                following = response.get("nextCursorMark")
                if (not rows or not isinstance(following, str) or not 1 <= len(following) <= 8192
                        or any(ord(char) < 32 or ord(char) > 126 for char in following) or following in cursors):
                    break
                cursor = following
        return select_scope_candidates(unique.values(), limit, policy)
    except Exception:
        if unique:
            return select_scope_candidates(unique.values(), limit, policy)
        return _ncbi_discover(client, settings, limit, excluded)
