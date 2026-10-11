"""Commit-pinned, read-only source inspection. Nothing in a repository is executed."""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit

GITHUB = "https://api.github.com/repos/"
LANGUAGES = {".py": "Python", ".r": "R", ".rmd": "R", ".ipynb": "Python notebook"}
MAX_SOURCE = 200000


def _repository_url(record):
    urls = [record.get("codeURL"), record.get("scriptURL")]
    urls.extend(record.get("article", {}).get("codeURLs") or [])
    urls.extend(record.get("codeURLs") or [])
    matches = []
    for value in urls:
        if not isinstance(value, str):
            continue
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
            continue
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and all(re.fullmatch(r"[\w.-]+", p) and p not in {".", ".."} for p in parts[:2]):
            repo = parts[1].removesuffix(".git")
            # Slash-containing branch names cannot be unambiguously parsed here.
            path = unquote("/".join(parts[4:])) if len(parts) > 4 and parts[2] == "blob" else ""
            matches.append((parts[0], repo, path))
    if not matches:
        return None
    owner, repo, _ = matches[0]
    # Known exact script for the same author repository takes precedence over
    # its generic landing URL; never switch to another cited repository.
    return next((match for match in matches if match[:2] == (owner, repo) and match[2]), matches[0])


def _language(path):
    return LANGUAGES.get(PurePosixPath(path).suffix.lower(), "")


def _safe_repo_path(path):
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        return ""
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or len(path) > 400:
        return ""
    return str(parsed)


def _source_text(raw, path):
    text = raw.decode("utf-8-sig")
    if path.lower().endswith(".ipynb"):
        doc = json.loads(text)
        cells = doc.get("cells", [])
        if not isinstance(cells, list):
            raise ValueError("notebook cells malformed")
        chunks = []
        for index, cell in enumerate(cells[:1000]):
            if cell.get("cell_type") == "code":
                content = cell.get("source", [])
                if isinstance(content, list):
                    content = "".join(content)
                chunks.append(f"# Notebook cell {index + 1}\n" + str(content))
        # Ignore outputs: may contain private data or unsafe rich HTML.
        return "\n\n".join(chunks), "notebook code-cell concatenation"
    return text, "source file"


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_name(node.value) + "." + node.attr
    return ""


def _literal_arg(call):
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for keyword in call.keywords:
        if keyword.arg in {"filepath_or_buffer", "path", "filename", "fname"} and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return ""


def inspect_source(text, path, url=""):
    """Expose only observed IO paths / field references and line provenance."""
    lines = text.splitlines()
    inputs, outputs, fields, evidence = [], [], [], []
    language = _language(path)
    if language.startswith("Python"):
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = _call_name(node.func)
                    arg = _literal_arg(node)
                    if name.split(".")[-1] in {"read_csv", "read_table", "read_excel", "read_json", "read_h5ad", "read_h5mu", "loadtxt", "genfromtxt", "read_pickle", "read_parquet", "read_feather"}:
                        reader_options = {}
                        for keyword in node.keywords:
                            if keyword.arg in {"header", "sep", "delimiter", "skiprows", "index_col"} and isinstance(keyword.value, ast.Constant):
                                reader_options[keyword.arg] = keyword.value.value
                            if keyword.arg == "names":
                                reader_options["namesProvided"] = True
                        inputs.append({"path": arg, "reader": name, "line": node.lineno, "status": "literal_path" if arg else "dynamic_path", "format": PurePosixPath(arg).suffix.lstrip(".") or "unknown", "readerOptions": reader_options})
                    if name.split(".")[-1] in {"savefig", "save", "write_image"} and arg:
                        outputs.append({"path": arg, "writer": name, "line": node.lineno})
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    fields.append({"name": node.slice.value, "line": node.lineno, "kind": "code_field_reference"})
        except (SyntaxError, ValueError, RecursionError):
            evidence.append({"type": "limitation", "text": "Python source could not be parsed; notebook magics or dynamic syntax require review."})
    elif language == "R":
        for number, line in enumerate(lines, 1):
            for match in re.finditer(r"\b(read\.csv|read\.table|read\.delim|read_csv|read_tsv|read_excel|readRDS|load|fread)\s*\(\s*(['\"])(.*?)\2", line):
                options = {}
                if re.search(r"\bheader\s*=\s*(?:FALSE|F)\b", line):
                    options["header"] = None
                elif re.search(r"\bheader\s*=\s*(?:TRUE|T)\b", line):
                    options["header"] = 0
                elif match.group(1) == "read.table":
                    options["header"] = None  # base R default; do not invent headers.
                inputs.append({"path": match.group(3), "reader": match.group(1), "line": number, "status": "literal_path", "format": PurePosixPath(match.group(3)).suffix.lstrip(".") or "unknown", "readerOptions": options})
            for match in re.finditer(r"\b(ggsave|pdf|png|jpeg|svg|tiff)\s*\(\s*(?:filename\s*=\s*)?(['\"])(.*?)\2", line):
                outputs.append({"path": match.group(3), "writer": match.group(1), "line": number})
            for match in re.finditer(r"\$([A-Za-z][\w.]*)|\[\[\s*['\"]([^'\"]+)['\"]\s*\]\]", line):
                fields.append({"name": match.group(1) or match.group(2), "line": number, "kind": "code_field_reference"})
    for item in inputs + outputs:
        number = item["line"]
        evidence.append({"type": "io_reference", "line": number, "url": url + f"#L{number}" if url else "",
                         "text": lines[number - 1].strip()[:240]})
    deduped_fields = {}
    for item in fields:
        if len(item["name"]) < 120:
            deduped_fields.setdefault(item["name"], item)
    return {"inputs": inputs[:30], "outputs": outputs[:30], "fields": list(deduped_fields.values())[:80], "evidence": evidence[:40]}


def _input_path(script_path, literal, tree_paths):
    if not isinstance(literal, str) or "\\" in literal or "\x00" in literal or PurePosixPath(literal).is_absolute() or len(literal) > 400:
        return ""
    def normalize(value):
        parts = []
        for part in PurePosixPath(value).parts:
            if part == "..":
                if not parts:
                    return ""
                parts.pop()
            elif part != ".":
                parts.append(part)
        return "/".join(parts)
    # Scripts can run from repository root or their own directory. Both are
    # represented as candidates; parent traversal is allowed only inside repo.
    for candidate in [normalize(literal), normalize(str(PurePosixPath(script_path).parent / literal))]:
        if candidate in tree_paths:
            return candidate
    return ""


def _actual_header(raw, extension, options=None):
    """Do not deserialize scientific objects. Delimited text header only, no rows."""
    options = options or {}
    if ("header" in options and options["header"] is None) or options.get("namesProvided"):
        return []
    if extension not in {".csv", ".tsv", ".txt"} or b"\x00" in raw:
        return []
    text = raw.decode("utf-8-sig")
    delimiter = options.get("sep") or options.get("delimiter") or ("\t" if extension == ".tsv" else ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        return []
    skip = options.get("skiprows", 0)
    header = options.get("header", 0)
    if isinstance(skip, bool) or not isinstance(skip, int) or isinstance(header, bool) or not isinstance(header, int) or not 0 <= skip + header <= 20:
        return []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for _ in range(skip + header):
        next(reader, None)
    row = next(reader, [])
    return row[:80] if row and len(row) <= 80 and all(len(v) < 120 for v in row) else []


def inspect_repository(client, record, max_files=6):
    found = _repository_url(record)
    base_result = {"status": "not_found", "url": "", "commit": "", "license": "unknown",
                   "scripts": [], "inputs": [], "figureMappings": [], "limitations": [],
                   "authorCodeExecuted": False}
    if not found:
        base_result["limitations"] = ["No exact public GitHub repository URL found in supplied evidence."]
        return base_result
    owner, repo, selected_path = found
    base = GITHUB + quote(owner) + "/" + quote(repo)
    base_result["url"] = "https://github.com/" + owner + "/" + repo
    try:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if getattr(client, "github_token", ""):
            headers["Authorization"] = "Bearer " + client.github_token
        metadata = client.get_json(base, ttl=86400, headers=headers)
        if metadata.get("private") in (True, "true", "yes") or str(metadata.get("visibility") or "").lower() in {"private", "internal"}:
            base_result["status"] = "link_only_private"
            base_result["publicAccessRequired"] = True
            base_result["limitations"] = ["Repository is private/internal. Public source access is required; no commit, tree, code or input paths were fetched."]
            return base_result
        license_info = metadata.get("license") or {}
        spdx = license_info.get("spdx_id")
        base_result["license"] = spdx if spdx and spdx not in {"NOASSERTION", "OTHER"} else "unknown"
        default_branch = metadata.get("default_branch") or "main"
        commit_info = client.get_json(base + "/commits/" + quote(default_branch, safe=""), ttl=3600, headers=headers)
        commit = commit_info.get("sha", "")
        if not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", commit):
            raise ValueError("unverified repository commit")
        base_result["commit"] = commit
        tree = client.get_json(base + "/git/trees/" + commit, params={"recursive": "1"}, max_bytes=6000000, headers=headers)
        entries = {item["path"]: item for item in tree.get("tree", []) if item.get("type") == "blob" and _safe_repo_path(item.get("path"))}
        if tree.get("truncated"):
            base_result["limitations"].append("GitHub tree truncated: not all repository files were inspected.")
        candidates = [path for path, item in entries.items() if _language(path) and int(item.get("size") or 0) <= MAX_SOURCE]
        def priority(path):
            lower = path.lower()
            return (path != selected_path, not bool(re.search(r"fig|plot|visual|analysis|paper", lower)), lower)
        candidates.sort(key=priority)
        header_fetches = 0
        for path in candidates[:min(12, max(1, int(max_files)))]:
            raw_url = "https://raw.githubusercontent.com/" + owner + "/" + repo + "/" + commit + "/" + quote(path, safe="/")
            source_url = base_result["url"] + "/blob/" + commit + "/" + quote(path, safe="/")
            try:
                raw = client.get_bytes(raw_url, max_bytes=MAX_SOURCE)
                text, line_scope = _source_text(raw, path)
                observed = inspect_source(text, path, source_url if line_scope == "source file" else "")
            except Exception as exc:
                base_result["limitations"].append("Source unavailable: " + path + " (" + type(exc).__name__ + ")")
                continue
            script = {"path": path, "url": source_url, "sha256": hashlib.sha256(raw).hexdigest(),
                      "language": _language(path), "inputs": observed["inputs"], "outputs": observed["outputs"],
                      "fields": observed["fields"], "evidence": observed["evidence"], "lineScope": line_scope}
            base_result["scripts"].append(script)
            for value in observed["inputs"]:
                actual = _input_path(path, value["path"], entries)
                spec = {"name": value["path"] or "dynamic input path", "path": actual,
                        "reader": value["reader"], "format": value["format"],
                        "status": "repository_file_found" if actual else "path_not_resolved",
                        "orientation": "unknown", "scale": "unknown", "fields": [],
                        "observedFieldReferences": observed["fields"], "scriptURL": source_url,
                        "line": value["line"], "url": "", "readerOptions": value.get("readerOptions", {})}
                if actual:
                    spec["url"] = base_result["url"] + "/blob/" + commit + "/" + quote(actual, safe="/")
                    item = entries[actual]
                    # Never fetch pickle/RDS/h5ad or large raw scientific datasets.
                    if header_fetches < 2 and int(item.get("size") or 0) <= 100000:
                        extension = PurePosixPath(actual).suffix.lower()
                        if extension in {".csv", ".tsv", ".txt"}:
                            header_fetches += 1
                            try:
                                data = client.get_bytes("https://raw.githubusercontent.com/" + owner + "/" + repo + "/" + commit + "/" + quote(actual, safe="/"), max_bytes=100000)
                                header = _actual_header(data, extension, value.get("readerOptions", {}))
                                spec["fields"] = [{"name": name, "kind": "actual_file_header"} for name in header]
                                spec["headerSHA256"] = hashlib.sha256(data).hexdigest()
                                spec["status"] = "header_observed" if header else "header_unresolved"
                            except Exception:
                                spec["status"] = "header_fetch_failed"
                base_result["inputs"].append(spec)
            for output in observed["outputs"]:
                figure = re.search(r"(?:fig(?:ure)?)[\s_.-]*(\d+[a-z]?)", PurePosixPath(output["path"]).name, re.I)
                if figure:
                    base_result["figureMappings"].append({"figureLabel": "Figure " + figure.group(1), "output": output["path"],
                        "scriptURL": source_url, "line": output["line"], "status": "candidate",
                        "evidence": "Explicit output filename; not verified against article caption or panels."})
        base_result["status"] = "inspected" if base_result["scripts"] else "no_scripts_found"
        base_result["limitations"].append("Static inspection only. No code, installer, notebook, pickle or RDS was executed.")
        if base_result["license"] == "unknown":
            base_result["limitations"].append("Repository license unverified: link-only, no copied or redistributed annotated author code.")
        return base_result
    except Exception as exc:
        base_result["status"] = "unavailable"
        base_result["errorStatus"] = getattr(exc, "status", None)
        # Transport errors are sanitized by our own HttpClient. Never expose an
        # unknown third-party exception string or URL/query credentials.
        from .http import FetchError
        base_result["lastError"] = str(exc)[:160] if isinstance(exc, FetchError) and "https://" not in str(exc) and "http://" not in str(exc) else type(exc).__name__
        base_result["limitations"].append("GitHub inspection failed: " + type(exc).__name__)
        return base_result
