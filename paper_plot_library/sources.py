"""Bounded literature metadata discovery; full-text retrieval lives in figures.py."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import re
from urllib.parse import quote
import xml.etree.ElementTree as ET

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


def _ncbi_discover(client, settings, limit):
    """NCBI fallback; bounded PubMed metadata, not article/PDF scraping."""
    term = getattr(settings, "ncbi_query", None) or (
        '(genomics OR "single cell" OR "multi omics" OR bioinformatics) '
        'AND (github OR "source code") NOT review[Publication Type]')
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
        if _usable(record) and _published_not_future(record, today):
            result.append(record)
    return result


def discover(client, settings):
    """Return at most discovery_limit unique formal candidates; no filler papers."""
    limit = min(100, max(1, int(getattr(settings, "discovery_limit", 30))))
    query = getattr(settings, "discovery_query", None) or DEFAULT_QUERY
    today = _utc_today()
    # Filter BEFORE newest-first pagination. Filtering future rows only after
    # retrieval would allow forthcoming Nov/Dec issues to starve older papers.
    # Eligibility is enforced at the server too: otherwise DOI-less PMC-only
    # records or reviews can occupy the newest-first page before local filtering.
    query = ("(" + query + ') AND HAS_DOI:Y AND SRC:MED AND NOT PUB_TYPE:"Review"'
             " AND FIRST_PDATE:[1900-01-01 TO " + today.isoformat() + "]")
    try:
        response = client.get_json(EPMC, params={"query": query, "format": "json", "resultType": "core",
                                               "pageSize": limit, "sort": "FIRST_PDATE_D desc"}, ttl=3600)
        records = [_paper(row) for row in response.get("resultList", {}).get("result", [])]
        unique = {}
        for record in records:
            if _usable(record) and _published_not_future(record, today):
                unique.setdefault(record["doi"], record)
        return list(unique.values())[:limit]
    except Exception:
        return _ncbi_discover(client, settings, limit)
