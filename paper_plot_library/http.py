"""Bounded HTTPS, transient retries and private hashed caches.

Transport intentionally does not expose URLs containing query credentials in errors.
"""
import hashlib
import base64
import binascii
import http.client
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .storage import read_json, write_json

ALLOWED_HOSTS = frozenset({
    "www.ebi.ac.uk", "europepmc.org", "api.github.com", "raw.githubusercontent.com",
    "pmc-oa-opendata.s3.amazonaws.com", "eutils.ncbi.nlm.nih.gov", "api.openalex.org",
    "api.crossref.org", "www.easyscholar.cc", "api.deepseek.com",
})


class FetchError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def validate_url(url):
    try:
        if not isinstance(url, str) or any(ord(c) < 32 for c in url):
            raise ValueError()
        parts = urlsplit(url)
        permitted = parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and not parts.username and not parts.password and parts.port in (None, 443)
    except (ValueError, TypeError, AttributeError):
        raise FetchError("远程 URL 格式无效") from None
    if not permitted:
        raise FetchError("远程来源不在许可 HTTPS 接口名单中")
    return parts


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = validate_url(newurl)
        old = validate_url(req.full_url)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and new.hostname != old.hostname:
            redirected.remove_header("Authorization")
        return redirected


class HttpClient:
    def __init__(self, cache_dir, max_requests=150, timeout=25, retries=3,
                 opener=None, sleep=time.sleep, clock=time.time, max_post_attempts=5):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or build_opener(SafeRedirect())
        self.sleep = sleep
        self.clock = clock
        self.requests = 0
        self.post_attempts = 0
        self.max_post_attempts = max_post_attempts
        self._last_host_call = {}

    def _request(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000, payload=None):
        parts = validate_url(url)
        try:
            if params:
                query = parts.query + ("&" if parts.query else "") + urlencode(params)
                url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
            body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            identity = json.dumps([url, headers or {}, hashlib.sha256(body or b"").hexdigest()], sort_keys=True)
        except (ValueError, TypeError, UnicodeError):
            raise FetchError("请求参数格式无效") from None
        digest = hashlib.sha256(identity.encode()).hexdigest()
        cache = self.cache_dir / f"http-{digest}.json"
        if body is None and ttl > 0:
            try:
                saved = read_json(cache)
                at = saved.get("at") if isinstance(saved, dict) else None
                if isinstance(at, (int, float)) and math.isfinite(at) and 0 <= self.clock() - at < ttl:
                    value = base64.b64decode(saved["data"], validate=True)
                    if len(value) <= max_bytes:
                        return value
            except (ValueError, TypeError, KeyError, OSError, binascii.Error):
                pass  # Cache is disposable; corrupt cache never blocks a safe fresh fetch.
        request_headers = {"User-Agent": "paper-plot-library/0.1 (+lawful bounded research)", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        for attempt in range(self.retries):
            if self.requests >= self.max_requests:
                raise FetchError("本次网络请求预算已用完")
            if body is not None and self.post_attempts >= self.max_post_attempts:
                raise FetchError("本次 AI 调用预算已用完")
            host = parts.hostname
            # NCBI key or no key: shared 3 req/s conservative default; other APIs also paced.
            interval = .36 if host == "eutils.ncbi.nlm.nih.gov" else .5 if host == "www.easyscholar.cc" else .12
            delay = interval - (self.clock() - self._last_host_call.get(host, 0))
            if delay > 0:
                self.sleep(delay)
            self.requests += 1
            if body is not None:
                self.post_attempts += 1
            self._last_host_call[host] = self.clock()
            try:
                request = Request(url, data=body, headers=request_headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    final = response.geturl() if hasattr(response, "geturl") else url
                    validate_url(final)
                    length = response.headers.get("Content-Length")
                    if length and (not str(length).isdigit()):
                        raise FetchError(f"{host}: 响应长度无效")
                    if length and int(length) > max_bytes:
                        raise FetchError(f"{host}: 文件超过体积上限")
                    value = response.read(max_bytes + 1)
                    if len(value) > max_bytes:
                        raise FetchError(f"{host}: 响应超过体积上限")
                if body is None and ttl > 0:
                    write_json(cache, {"at": self.clock(), "data": base64.b64encode(value).decode()})
                return value
            except HTTPError as exc:
                status = exc.code
                retry = status == 429 or 500 <= status <= 599
                delay = min(30, 2 ** attempt)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if retry_after:
                    try:
                        delay = min(30, max(0, float(retry_after)))
                    except ValueError:
                        try:
                            date = parsedate_to_datetime(retry_after)
                            delay = min(30, max(0, date.timestamp() - self.clock()))
                        except (ValueError, TypeError):
                            pass
                if not retry or attempt + 1 == self.retries:
                    raise FetchError(f"{host}: HTTP {status}", status) from None
                self.sleep(delay)
            except (URLError, TimeoutError, OSError, http.client.HTTPException):
                if attempt + 1 == self.retries:
                    raise FetchError(f"{host}: 网络暂不可达") from None
                self.sleep(2 ** attempt)
        raise FetchError("网络请求未完成")

    def get_bytes(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000):
        return self._request(url, params, headers, ttl, max_bytes)

    def get_text(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000):
        try:
            return self.get_bytes(url, params, headers, ttl, max_bytes).decode("utf-8-sig")
        except UnicodeError:
            raise FetchError("远程文本不是有效 UTF-8") from None

    def get_json(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000):
        try:
            return json.loads(self.get_text(url, params, headers, ttl, max_bytes))
        except (ValueError, TypeError):
            raise FetchError("远程返回的不是有效 JSON") from None

    def post_json(self, url, payload, headers=None, max_bytes=1000000):
        try:
            return json.loads(self._request(url, headers=headers, ttl=0, max_bytes=max_bytes, payload=payload))
        except (ValueError, TypeError):
            raise FetchError("AI 接口返回的不是有效 JSON") from None
