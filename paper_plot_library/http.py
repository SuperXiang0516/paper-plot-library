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
from typing import Literal
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
    def __init__(self, message, status=None, *, reason=None, retry_delay=None, retryable=None):
        super().__init__(message)
        self.status = status
        if reason == "response_json_malformed":
            self.reason = reason
        if type(retryable) is bool:
            self.retryable = retryable
        if (type(retry_delay) in (int, float) and 0 <= retry_delay <= 30
                and math.isfinite(retry_delay)):
            self.retry_delay = retry_delay


BudgetReason = Literal[
    "network_budget_exhausted", "collection_budget_exhausted", "ai_budget_exhausted", "session_time_exhausted",
]


class BudgetExceeded(FetchError):
    """Safe machine-readable reason for a request blocked before transport."""

    _MESSAGES = {
        "network_budget_exhausted": "本次网络请求预算已用完",
        "collection_budget_exhausted": "本次资料采集预算已用完；剩余请求为 AI 调用保留",
        "ai_budget_exhausted": "本次 AI 调用预算已用完",
        "session_time_exhausted": "连续补库已达到安全运行时间上限",
    }

    def __init__(self, reason: BudgetReason):
        if not isinstance(reason, str) or reason not in self._MESSAGES:
            raise ValueError("请求预算原因无效")
        self.reason: BudgetReason = reason
        super().__init__(self._MESSAGES[reason])


def validate_url(url):
    try:
        if not isinstance(url, str) or any(ord(c) < 32 for c in url):
            raise ValueError()
        parts = urlsplit(url)
        permitted = parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and not parts.username and not parts.password and parts.port in (None, 443)
    except (ValueError, TypeError, AttributeError):
        raise FetchError("远程 URL 格式无效", retryable=False) from None
    if not permitted:
        raise FetchError("远程来源不在许可 HTTPS 接口名单中", retryable=False)
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
                 opener=None, sleep=time.sleep, clock=time.time, max_post_attempts=5,
                 reserved_post_requests=0):
        if type(reserved_post_requests) is not int or reserved_post_requests < 0:
            raise ValueError("reserved_post_requests 必须为非负整数")
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
        self.reserved_post_requests = max(0, min(reserved_post_requests, max_requests, max_post_attempts))
        self._last_host_call = {}
        self.session_deadline = None
        self.deadline_clock = time.monotonic
        self.time_budget_exhausted = False

    def _time_remaining(self):
        deadline = self.session_deadline
        if deadline is None:
            return None
        try:
            remaining = deadline - self.deadline_clock()
        except (TypeError, ValueError, OverflowError):
            remaining = 0
        if not isinstance(remaining, (int, float)) or not math.isfinite(remaining) or remaining <= 0:
            self.time_budget_exhausted = True
            raise BudgetExceeded("session_time_exhausted")
        return remaining

    def bounded_sleep(self, seconds):
        remaining = self._time_remaining()
        if remaining is not None and seconds >= remaining:
            self.time_budget_exhausted = True
            raise BudgetExceeded("session_time_exhausted")
        self.sleep(seconds)
        self._time_remaining()

    def _request(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000, payload=None,
                 attempt_limit=None):
        if attempt_limit is not None and (type(attempt_limit) is not int or not 1 <= attempt_limit <= 10):
            raise FetchError("单次请求尝试次数设置无效", retryable=False)
        transport_attempts = self.retries if attempt_limit is None else min(self.retries, attempt_limit)
        parts = validate_url(url)
        try:
            if params:
                query = parts.query + ("&" if parts.query else "") + urlencode(params)
                url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
            body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            identity = json.dumps([url, headers or {}, hashlib.sha256(body or b"").hexdigest()], sort_keys=True)
        except (ValueError, TypeError, UnicodeError):
            raise FetchError("请求参数格式无效", retryable=False) from None
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
        for attempt in range(transport_attempts):
            self._time_remaining()
            if self.requests >= self.max_requests:
                raise BudgetExceeded("network_budget_exhausted")
            if body is not None and self.post_attempts >= self.max_post_attempts:
                raise BudgetExceeded("ai_budget_exhausted")
            if body is None:
                reserved = min(self.reserved_post_requests, max(0, self.max_post_attempts - self.post_attempts))
                if self.requests >= self.max_requests - reserved:
                    raise BudgetExceeded("collection_budget_exhausted")
            host = parts.hostname
            # NCBI key or no key: shared 3 req/s conservative default; other APIs also paced.
            interval = .36 if host == "eutils.ncbi.nlm.nih.gov" else .5 if host == "www.easyscholar.cc" else .12
            delay = interval - (self.clock() - self._last_host_call.get(host, 0))
            if delay > 0:
                self.bounded_sleep(delay)
            remaining = self._time_remaining()
            self.requests += 1
            if body is not None:
                self.post_attempts += 1
            self._last_host_call[host] = self.clock()
            try:
                request = Request(url, data=body, headers=request_headers)
                timeout = min(self.timeout, remaining) if remaining is not None else self.timeout
                with self.opener.open(request, timeout=timeout) as response:
                    final = response.geturl() if hasattr(response, "geturl") else url
                    validate_url(final)
                    length = response.headers.get("Content-Length")
                    if length and (not str(length).isdigit()):
                        raise FetchError(f"{host}: 响应长度无效", retryable=False)
                    if length and int(length) > max_bytes:
                        raise FetchError(f"{host}: 文件超过体积上限", retryable=False)
                    value = response.read(max_bytes + 1)
                    self._time_remaining()
                    if len(value) > max_bytes:
                        raise FetchError(f"{host}: 响应超过体积上限", retryable=False)
                if body is None and ttl > 0:
                    write_json(cache, {"at": self.clock(), "data": base64.b64encode(value).decode()})
                return value
            except HTTPError as exc:
                status = exc.code
                retry = status == 429 or 500 <= status <= 599
                delay = min(30, 2 ** attempt)
                advised_delay = None
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if retry_after:
                    try:
                        delay = min(30, max(0, float(retry_after)))
                        advised_delay = delay
                    except ValueError:
                        try:
                            date = parsedate_to_datetime(retry_after)
                            delay = min(30, max(0, date.timestamp() - self.clock()))
                            advised_delay = delay
                        except (ValueError, TypeError):
                            pass
                if not retry or attempt + 1 == transport_attempts:
                    raise FetchError(f"{host}: HTTP {status}", status,
                                     retry_delay=advised_delay if retry else None) from None
                self.bounded_sleep(delay)
            except (URLError, TimeoutError, OSError, http.client.HTTPException):
                if attempt + 1 == transport_attempts:
                    raise FetchError(f"{host}: 网络暂不可达") from None
                self.bounded_sleep(2 ** attempt)
        raise FetchError("网络请求未完成")

    def get_bytes(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000):
        return self._request(url, params, headers, ttl, max_bytes)

    def get_text(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000):
        try:
            return self.get_bytes(url, params, headers, ttl, max_bytes).decode("utf-8-sig")
        except UnicodeError:
            raise FetchError("远程文本不是有效 UTF-8", retryable=False) from None

    def get_json(self, url, params=None, headers=None, ttl=86400, max_bytes=6000000):
        try:
            return json.loads(self.get_text(url, params, headers, ttl, max_bytes))
        except (ValueError, TypeError):
            raise FetchError("远程返回的不是有效 JSON", retryable=False) from None

    def post_json(self, url, payload, headers=None, max_bytes=1000000, *, attempt_limit=None):
        try:
            return json.loads(self._request(url, headers=headers, ttl=0, max_bytes=max_bytes, payload=payload,
                                            attempt_limit=attempt_limit))
        except (ValueError, TypeError):
            raise FetchError("AI 接口返回的不是有效 JSON", reason="response_json_malformed") from None
