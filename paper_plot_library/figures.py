"""Current PMC OA cloud JATS and original figure adapter, with a reuse gate.

Only selected article assets are retrieved. No whole issue/archive scraping, no
guessed panel extraction, and no publisher-page images used as original figures.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from .sources import normalize_doi

CLOUD = "https://pmc-oa-opendata.s3.amazonaws.com"
XLINK = "{http://www.w3.org/1999/xlink}href"
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp"}
MAX_IMAGE_BYTES = 8000000
MAX_SUPPLEMENT_BYTES = 6000000


def cloud_url(value):
    """Only URLs in the current PMC article bucket; preserve integrity query."""
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme == "s3" and parsed.netloc == "pmc-oa-opendata":
        return CLOUD + parsed.path + ("?" + parsed.query if parsed.query else "")
    if parsed.scheme == "https" and parsed.hostname == "pmc-oa-opendata.s3.amazonaws.com" and not parsed.username and not parsed.password:
        return value
    return ""


def _bool(value):
    return value is True or str(value).lower() in {"true", "yes", "y", "1"}


def normalize_license(value):
    value = str(value or "").strip().upper().replace("_", " ").replace("-", " ")
    if "CREATIVECOMMONS.ORG/PUBLICDOMAIN/ZERO" in value or value in {"CC0", "CC 0"}:
        return "CC0"
    if "CREATIVECOMMONS.ORG/LICENSES/" in value:
        tail = value.split("CREATIVECOMMONS.ORG/LICENSES/")[1].split("/")[0]
        return "CC " + tail
    return " ".join(value.split()) or "UNKNOWN"


def _license_value(node):
    if node is None:
        return ""
    for value in [node.get(XLINK, "")] + [n.get(XLINK, "") for n in node.iter()]:
        if "creativecommons.org/" in value.lower():
            return value
    return _text(node)


def _license_url(value, fallback=""):
    """Return a source-declared CC license URL, never assume its version."""
    for candidate in (value, fallback):
        if isinstance(candidate, str):
            parsed = urlsplit(candidate)
            if parsed.scheme in {"http", "https"} and parsed.hostname == "creativecommons.org" and not parsed.username and not parsed.password and (parsed.path.startswith("/licenses/") or parsed.path.startswith("/publicdomain/zero/")):
                return "https://creativecommons.org" + parsed.path
    return ""


def figure_reference(value):
    """Main, Extended Data and Supplementary numbering are separate namespaces."""
    value = str(value or "")
    if not value.strip():
        return None
    if re.search(r"extended\s*(?:data|fig)|扩展", value, re.I):
        scope = "extended"
    elif re.search(r"supplement|supporting|补充|附图", value, re.I):
        scope = "supplementary"
    else:
        scope = "main"
    match = re.search(r"(?:\bfig(?:ure)?s?|图)\.?\s*(s?)(\d+)(?:\s*([a-z])(?=$|[^a-z]))?", value, re.I)
    if not match and scope == "extended":
        match = re.search(r"extended\s*data\s*(s?)(\d+)(?:\s*([a-z])(?=$|[^a-z]))?", value, re.I)
    if not match:
        return None
    if match.group(1).lower() == "s":
        scope = "supplementary"
    return {"scope": scope, "number": match.group(2), "panel": (match.group(3) or "").lower()}


def _declared_repositories(node, heading=""):
    """A code section may also name third-party tools; evidence is per statement."""
    def render(element):
        chunks = [element.text or ""]
        for child in element:
            chunks.append(render(child))
            href = child.get(XLINK, "")
            if child.tag.rsplit("}", 1)[-1] == "ext-link" and href.startswith("https://github.com/"):
                chunks.append(" " + href + " ")
            chunks.append(child.tail or "")
        return "".join(chunks)
    declaration = " ".join(render(node).split())
    urls = []
    def replace_url(match):
        raw = match.group(0)
        clean = raw.rstrip(".,;:!?")
        punctuation = raw[len(clean):]
        url = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)(?:[/#?].*)?", clean)
        if not url:
            return "UNRESOLVED_URL" + punctuation
        repo = "https://github.com/" + url.group(1).rstrip(".") + "/" + url.group(2).rstrip(".").removesuffix(".git")
        index = len(urls)
        urls.append(repo)
        return f"REPOSITORY_{index}" + punctuation
    tokenized = re.sub(r"https://github\.com/[^\s<>()\[\]]+", replace_url, declaration)
    statements = re.split(r"(?<=[.!?;])\s+|\s+(?:and|while|whereas)\s+(?=(?:our|custom)\s)", tokenized, flags=re.I)
    result = []
    for statement in statements:
        ids = [int(value) for value in re.findall(r"REPOSITORY_(\d+)", statement)]
        repos = list(dict.fromkeys(urls[index] for index in ids))
        if not repos:
            continue
        has_code = bool(re.search(r"\b(?:code|scripts?|software)\b", statement, re.I))
        availability = bool(re.search(r"\b(?:available|accessible|accessed|deposited|provided|released|found|retrieved)\b", statement, re.I))
        author_cue = bool(re.search(r"\bour\b|\bcustom\b|\bdeveloped\b|\bgenerated\b|\banalysis\s+(?:code|scripts?)\b", statement, re.I))
        third_party = bool(re.search(r"third[ -]party|\bexternal\b|\bexisting\b|\b(?:package|library|tool)s?\s+(?:used|utilized)|\bused\s+(?:the\s+)?(?:package|library|tool)\b", statement, re.I))
        # Generic 'code is available' under a code declaration is sufficient,
        # but unrelated tool mentions and multi-repository ambiguous sentences
        # are kept out of source_confirmed evidence.
        if not has_code or not availability or (third_party and not author_cue) or (len(repos) > 1 and re.search(r"\b(?:package|library|tool)s?\b", statement, re.I)):
            continue
        context = statement
        for index in ids:
            context = context.replace(f"REPOSITORY_{index}", urls[index])
        result.extend((repo, context[:500]) for repo in repos)
    return result


def reuse_allowed(license_code, figure_text="", figure_license=""):
    license_code = normalize_license(figure_license or license_code)
    # Strict v1: NC, ND, SA, TDM and unresolved terms are links only.
    allowed = license_code in {"CC BY", "CC BY 4.0", "CC BY 3.0", "CC BY 2.5", "CC BY 2.0", "CC0"}
    third_party = bool(re.search(r"reproduced\s+(?:from|with)|adapted\s+from|with\s+permission|copyright|©|third[ -]party|courtesy\s+of", figure_text, re.I))
    return allowed and not third_party


def _text(node):
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _author_credit(article):
    """Bounded source-declared JATS names; never invent omitted author names."""
    names = []
    for contributor in article.findall(".//article-meta/contrib-group/contrib"):
        if contributor.get("contrib-type") != "author":
            continue
        collaboration = contributor.find(".//collab")
        if collaboration is not None:
            name = _text(collaboration)
        else:
            node = contributor.find(".//name")
            name = " ".join(part for part in (_text(node.find("given-names")), _text(node.find("surname"))) if part) if node is not None else ""
        name = name[:180].strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= 7:
            break
    if not names:
        return "Article authors (names unavailable in JATS metadata)"
    return "; ".join(names[:6]) + ("; et al." if len(names) > 6 else "")


def _parse_xml(text):
    # Standard JATS DOCTYPE declarations are permitted, but custom entities are not.
    if "<!ENTITY" in text.upper():
        raise ValueError("XML entity declarations rejected")
    return ET.fromstring(text)


def _verify_digest(raw, url):
    match = re.search(r"(?:[?&])md5=([a-fA-F0-9]{32})(?:&|$)", url)
    if match and hashlib.md5(raw).hexdigest() != match.group(1).lower():
        raise ValueError("PMC object MD5 mismatch")


def _atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_bytes(raw)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _media_match(href, media_urls, image_only=False):
    """Resolve only a declared graphic basename to a declared cloud object."""
    if not href:
        return ""
    clean = unquote(urlsplit(href).path)
    basename = PurePosixPath(clean).name
    if not basename or basename in {".", ".."}:
        return ""
    stem = PurePosixPath(basename).stem
    matches = []
    for value in media_urls:
        url = cloud_url(value)
        if not url:
            continue
        filename = PurePosixPath(unquote(urlsplit(url).path)).name
        extension = PurePosixPath(filename).suffix.lower()
        if image_only and extension not in IMAGE_TYPES:
            continue
        if filename == basename or (not PurePosixPath(basename).suffix and PurePosixPath(filename).stem == stem):
            matches.append(url)
    # No guessing similarly named images (g001 is not g001a / g001-large).
    preference = {".tif": 0, ".tiff": 0, ".png": 1, ".jpg": 2, ".jpeg": 2, ".gif": 3, ".webp": 3}
    matches.sort(key=lambda u: preference.get(PurePosixPath(urlsplit(u).path).suffix.lower(), 10))
    return matches[0] if matches else ""


def _image(raw):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        if width < 64 or height < 64 or width * height > 50000000:
            raise ValueError("Image dimensions outside safe bounds")
        if image.format not in {"JPEG", "PNG", "TIFF", "GIF", "WEBP"}:
            raise ValueError("Unsupported figure media format")
        image.verify()
        image_format = image.format
    # Browsers cannot display TIFF reliably: a deterministic PNG derivative.
    if image_format == "TIFF":
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            output = io.BytesIO()
            image.convert("RGB").save(output, "PNG")
            return output.getvalue(), "png", width, height, "TIFF converted to PNG; uncropped original dimensions retained"
    extension = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}[image_format]
    return raw, extension, width, height, "Original source bytes; no crop"


def _supplement_format(raw, url):
    extension = PurePosixPath(urlsplit(url).path).suffix.lower()
    if extension == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValueError("Not a PDF response")
        return "pdf"
    if extension in {".zip", ".docx", ".xlsx", ".pptx"}:
        if not raw.startswith(b"PK\x03\x04"):
            raise ValueError("Not a ZIP-container response")
        # Central-directory inspection only; never decompress/extract/run members.
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if not 1 <= len(members) <= 1000 or sum(member.file_size for member in members) > 100000000:
                raise ValueError("Archive member count or uncompressed budget exceeded")
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename or member.flag_bits & 1:
                    raise ValueError("Unsafe or encrypted archive member")
                if member.file_size > 30000000 or member.file_size / max(1, member.compress_size) > 200:
                    raise ValueError("Archive decompression ratio or member budget exceeded")
            if extension in {".docx", ".xlsx", ".pptx"}:
                names = {member.filename for member in members}
                expected_prefix = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}[extension]
                if "[Content_Types].xml" not in names or not any(name.startswith(expected_prefix) for name in names):
                    raise ValueError("ZIP container is not declared Office format")
        return extension.lstrip(".")
    if extension in {".csv", ".tsv", ".txt"}:
        if b"\x00" in raw or raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise ValueError("Unexpected supplement text format")
        raw.decode("utf-8-sig")
        return extension.lstrip(".")
    return "unsupported"


def inspect_article(client, record, asset_dir, include_supplements=True):
    """Resolve version/DOI, inspect JATS, fetch bounded licensed original figures."""
    result = {"status": "not_found", "license": "unknown", "version": "", "xmlURL": "",
              "figures": [], "supplements": [], "codeURLs": [], "limitations": [],
              "retrievedAt": datetime.now(timezone.utc).isoformat(), "source": "PMC OA Cloud"}
    result["codeURLEvidence"] = []
    pmcid = str(record.get("pmcid") or "").upper()
    doi = normalize_doi(record.get("doi"))
    max_file_bytes = min(10000000, max(1024, int(record.get("maxFileBytes", MAX_SUPPLEMENT_BYTES))))
    max_supplements = min(5, max(0, int(record.get("maxSupplementDownloads", 3))))
    if not re.fullmatch(r"PMC\d+", pmcid) or not doi:
        result["limitations"] = ["No exact PMCID and DOI pair resolved; original images are link-only."]
        return result
    try:
        listing = client.get_text(CLOUD + "/", params={"list-type": "2", "prefix": pmcid + ".", "delimiter": "/", "max-keys": 30}, ttl=86400, max_bytes=100000)
        tree = _parse_xml(listing)
        prefixes = [node.text or "" for node in tree.iter() if node.tag.rsplit("}", 1)[-1] == "Prefix"]
        versions = sorted({int(match.group(1)) for value in prefixes if (match := re.fullmatch(re.escape(pmcid) + r"\.(\d+)/", value))}, reverse=True)[:8]
        candidates = []
        for version in versions:
            meta = client.get_json(CLOUD + "/metadata/" + pmcid + "." + str(version) + ".json", ttl=86400, max_bytes=200000)
            if normalize_doi(meta.get("doi")) != doi:
                continue
            if _bool(meta.get("is_retracted")):
                result["status"] = "retracted"
                result["limitations"] = ["PMC marks a matching DOI version retracted; excluded from publication."]
                return result
            if meta.get("is_manuscript") not in (False, "no", "false", "N", 0):
                continue
            if cloud_url(meta.get("xml_url")):
                candidates.append(meta)
        if not candidates:
            result["status"] = "no_final_version"
            result["limitations"] = ["No DOI-matched non-manuscript publisher version available in current PMC cloud."]
            return result
        metadata = candidates[0]
        result["version"] = str(metadata.get("version"))
        result["license"] = normalize_license(metadata.get("license_code"))
        result["xmlURL"] = cloud_url(metadata.get("xml_url"))
        raw_xml = client.get_bytes(result["xmlURL"], max_bytes=6000000)
        _verify_digest(raw_xml, result["xmlURL"])
        article = _parse_xml(raw_xml.decode("utf-8"))
        xml_doi = next((_text(n) for n in article.findall(".//article-meta/article-id") if n.get("pub-id-type") == "doi"), "")
        if normalize_doi(xml_doi) != doi:
            result["status"] = "doi_mismatch"
            result["limitations"] = ["JATS DOI does not match the selected paper; no assets downloaded."]
            return result
        article_type = article.get("article-type", "")
        if article_type in {"retraction", "retraction-of-publication", "preprint"}:
            result["status"] = "excluded"
            result["limitations"] = ["JATS article type is excluded: " + article_type]
            return result
        all_media = metadata.get("media_urls") or []
        # Enforce article-specific prefix to prevent cross-paper object association.
        expected_prefix = "/" + pmcid + "." + result["version"] + "/"
        media = [u for u in all_media if cloud_url(u) and urlsplit(cloud_url(u)).path.startswith(expected_prefix)]
        xml_license = article.find(".//article-meta/permissions/license")
        license_href = _license_value(xml_license)
        result["licenseURL"] = _license_url(license_href)
        if license_href:
            normalized = normalize_license(license_href)
            if normalized != result["license"] and not reuse_allowed(normalized):
                result["license"] = normalized
        # Whole article text / references can cite somebody else's tool repo.
        # Only explicit author code-availability declarations are evidence.
        declarations = []
        for section in article.findall(".//sec"):
            title = _text(section.find("title")).lower()
            sec_type = (section.get("sec-type") or "").lower()
            if "code" in title or "code" in sec_type or "availability" in title or "availability" in sec_type:
                if re.search(r"\b(code|scripts?|software)\b", _text(section), re.I):
                    declarations.extend((paragraph, title) for paragraph in section.findall(".//p"))
        for node in article.findall(".//custom-meta"):
            if re.search(r"code|software", _text(node.find("meta-name")), re.I):
                declarations.append((node, _text(node.find("meta-name"))))
        for paragraph in article.findall(".//p"):
            if re.search(r"(?:our\s+(?:code|scripts?)|(?:source\s+)?code\s+(?:is\s+)?(?:available|availability)|scripts?\s+(?:are\s+)?available)", _text(paragraph), re.I):
                declarations.append((paragraph, ""))
        for node, heading in declarations:
            for repo_url, declaration in _declared_repositories(node, heading):
                result["codeURLs"].append(repo_url)
                evidence_item = {"url": repo_url, "scope": "author_code_availability",
                    "status": "source_declared", "sectionID": node.get("id", ""),
                    "sourceURL": result["xmlURL"], "text": declaration[:500]}
                if not any(item["url"] == repo_url for item in result["codeURLEvidence"]):
                    result["codeURLEvidence"].append(evidence_item)
        result["codeURLs"] = sorted(set(result["codeURLs"]))
        copyright_holder = _text(article.find(".//article-meta/permissions/copyright-holder"))
        author_credit = _author_credit(article)
        result["authorCredit"] = author_credit
        parents = {child: parent for parent in article.iter() for child in parent}
        for number, node in enumerate(article.findall(".//fig")[:60], 1):
            graphic = node.find(".//graphic")
            href = graphic.get(XLINK, "") if graphic is not None else ""
            source_url = _media_match(href, media, image_only=True)
            caption = _text(node.find("caption"))
            permissions = node.find("permissions")
            own_license = permissions.find("license") if permissions is not None else None
            own_license_text = _license_value(own_license)
            figure_license = normalize_license(own_license_text) if own_license_text else result["license"]
            allowed = reuse_allowed(result["license"], caption + " " + _text(permissions), own_license_text)
            declared_label = _text(node.find("label"))
            label = declared_label or "Unlabelled figure (JATS id: " + (node.get("id") or "unknown") + ")"
            parsed_reference = figure_reference(declared_label) if declared_label else None
            ancestor = parents.get(node)
            while ancestor is not None:
                if ancestor.tag.rsplit("}", 1)[-1] == "supplementary-material" and parsed_reference:
                    parsed_reference["scope"] = "supplementary"
                    break
                ancestor = parents.get(ancestor)
            result["figures"].append({"id": node.get("id") or "figure-" + str(number), "label": label,
                "labelStatus": "source_declared" if declared_label else "unresolved",
                "reference": parsed_reference,
                "caption": caption[:6000], "sourceURL": source_url,
                "articleURL": record.get("paperURL") or "https://doi.org/" + doi,
                "asset": "", "sha256": "", "width": None, "height": None,
                "license": figure_license, "licenseURL": _license_url(own_license_text, result.get("licenseURL", "")),
                "attribution": f"{author_credit}. {record.get('paper', '')}. DOI: {doi}. " + (f"Copyright holder: {copyright_holder}. " if copyright_holder else "") + f"Source: NLM PMC OA Cloud. License: {figure_license}.",
                "reuseStatus": "allowed" if allowed and source_url else "link_only",
                "downloadStatus": "pending" if allowed and source_url else "not_permitted_or_unresolved",
                "limitations": []})
        # New automatic cases need a publishable main-text image. Prefer the
        # main namespace before panel richness so two elaborate supplements
        # cannot exhaust the download slots while a usable body figure exists.
        # Explicit curated selections retain their original namespace/number.
        prefer_body = (record.get("collectionBatch") == "automatic"
                       and not str(record.get("figure") or "").strip())
        def figure_priority(figure):
            preferred = str(record.get("figure") or "").lower()
            label = figure["label"].lower().replace(".", "")
            preferred_match = bool(preferred and label and label in preferred)
            panels = len(set(re.findall(r"(?:^|[\s(,;])([a-h])(?:[),.;]|\s)", figure["caption"].lower())))
            reference = figure.get("reference") or {}
            non_body = prefer_body and reference.get("scope") != "main"
            return (not preferred_match, non_body, -panels)
        eligible = sorted([f for f in result["figures"] if f["reuseStatus"] == "allowed"], key=figure_priority)
        requested = figure_reference(record.get("figure"))
        if requested:
            eligible = [f for f in eligible if (reference := f.get("reference")) and reference["scope"] == requested["scope"] and reference["number"] == requested["number"]]
            if not eligible:
                result["limitations"].append("Requested figure number not found with permitted cloud media; no substitute figure downloaded.")
            for figure in eligible:
                figure["requestedReference"] = requested
                if requested["panel"]:
                    figure["selectionNote"] = "Whole original figure retained; requested panel " + requested["panel"] + " is not cropped or verified against code."
        elif str(record.get("figure") or "").strip():
            eligible = []
            result["limitations"].append("Requested figure reference could not be resolved; no substitute figure downloaded.")
        for figure in eligible[:min(5, max(0, int(record.get("maxFigureDownloads", 2))))]:
            try:
                raw = client.get_bytes(figure["sourceURL"], max_bytes=min(MAX_IMAGE_BYTES, max_file_bytes))
                _verify_digest(raw, figure["sourceURL"])
                encoded, extension, width, height, derivative = _image(raw)
                source_hash = hashlib.sha256(raw).hexdigest()
                name = pmcid.lower() + "-" + source_hash[:16] + "." + extension
                _atomic_bytes(Path(asset_dir) / name, encoded)
                figure.update({"asset": "assets/" + name, "sha256": source_hash,
                               "assetSHA256": hashlib.sha256(encoded).hexdigest(), "width": width,
                               "height": height, "downloadStatus": "downloaded", "modification": derivative,
                               "resolutionStatus": "Original PMC cloud source dimensions; maximum publisher resolution not verified"})
            except Exception as exc:
                figure["downloadStatus"] = "failed"
                figure["limitations"].append("Figure verification/download failed: " + type(exc).__name__)
        for node in article.findall(".//supplementary-material")[:20]:
            reference = node.find(".//media")
            if reference is None:
                reference = node.find(".//graphic")
            if reference is None:
                reference = node.find(".//ext-link")
            href = reference.get(XLINK, "") if reference is not None else ""
            # Some publishers put xlink:href directly on supplementary-material.
            href = href or node.get(XLINK, "")
            source_url = _media_match(href, media)
            supplement = {"id": node.get("id") or "supplement-" + str(len(result["supplements"]) + 1),
                "label": _text(node.find("label")) or _text(node.find("caption"))[:150] or "Supporting information",
                "sourceURL": source_url, "license": result["license"], "sha256": "",
                "status": "pending" if include_supplements else "not_requested", "bytes": 0,
                "format": "", "privatePath": ""}
            if not source_url:
                supplement["status"] = "unresolved_link"
            result["supplements"].append(supplement)
        if include_supplements:
            for supplement in result["supplements"][:max_supplements]:
                if not supplement["sourceURL"]:
                    continue
                if not reuse_allowed(result["license"], supplement["label"]):
                    supplement["status"] = "link_only_license"
                    continue
                try:
                    raw = client.get_bytes(supplement["sourceURL"], max_bytes=max_file_bytes)
                    _verify_digest(raw, supplement["sourceURL"])
                    file_format = _supplement_format(raw, supplement["sourceURL"])
                    if file_format == "unsupported":
                        supplement["status"] = "unsupported_format_link_only"
                        continue
                    if file_format == "pdf":
                        from .pdf_validation import verify_pdf
                        validation = verify_pdf(raw, expected_doi=doi, expected_title=record.get("paper", ""), label=supplement["label"])
                        supplement["validation"] = validation
                        if validation.get("status") == "parser_unavailable":
                            supplement["status"] = "validation_unavailable_link_only"
                            continue
                        if validation.get("status") not in {"verified", "identity_unconfirmed"}:
                            supplement["status"] = validation.get("status") or "pdf_validation_failed"
                            continue
                    digest = hashlib.sha256(raw).hexdigest()
                    private = Path(asset_dir).parent.parent / ".ppl" / "downloads" / pmcid.lower()
                    filename = digest[:16] + "." + file_format
                    _atomic_bytes(private / filename, raw)
                    supplement.update({"status": "downloaded_private", "sha256": digest,
                                       "bytes": len(raw), "format": file_format})
                    if file_format == "pdf" and supplement.get("validation", {}).get("status") == "identity_unconfirmed":
                        supplement["status"] = "downloaded_private_identity_unconfirmed"
                    # No local private path is exposed in public data.
                except Exception as exc:
                    supplement["status"] = "failed"
                    supplement["error"] = type(exc).__name__
            for supplement in result["supplements"][max_supplements:]:
                if supplement["status"] == "pending":
                    supplement["status"] = "deferred_count_limit"
        result["status"] = "inspected"
        if not any(f["asset"] for f in result["figures"]):
            result["limitations"].append("No verified reusable image downloaded; figure links remain available.")
        result["limitations"].append("Image captions, script output names and category tags do not prove exact panel-to-code correspondence.")
        return result
    except Exception as exc:
        result["status"] = "unavailable"
        result["limitations"].append("PMC cloud inspection failed: " + type(exc).__name__)
        return result


def crop_approved_box(source_path, output_path, box, approval, license_code=""):
    """Optional deterministic crop of a human-approved whole-panel bounding box."""
    if not isinstance(approval, dict) or approval.get("approved") is not True or not approval.get("reviewer") or not approval.get("reason"):
        raise ValueError("Explicit approved box, reviewer and reason are required")
    if not reuse_allowed(license_code):
        raise ValueError("Image license does not permit automatic crop reuse")
    if not isinstance(box, (list, tuple)) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in box):
        raise ValueError("Box must contain four integer pixel coordinates")
    from PIL import Image
    raw = Path(source_path).read_bytes()
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        left, top, right, bottom = box
        if not (0 <= left < right <= width and 0 <= top < bottom <= height and right - left >= 32 and bottom - top >= 32):
            raise ValueError("Approved box outside image or too small")
        output = io.BytesIO()
        image.crop((left, top, right, bottom)).save(output, "PNG")
    encoded = output.getvalue()
    _atomic_bytes(output_path, encoded)
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "sourceSHA256": hashlib.sha256(raw).hexdigest(),
            "width": right - left, "height": bottom - top, "box": list(box), "approval": approval,
            "modification": "Cropped to approved panel box; labels, axes and legend require reviewer verification."}
