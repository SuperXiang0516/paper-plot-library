"""Transport audit tests; only fake network events, never real API calls."""
import base64
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from paper_plot_library.http import BudgetExceeded, FetchError, HttpClient, SafeRedirect, validate_url


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Response:
    def __init__(self, data=b'{"ok":true}', url="https://api.github.com/test", headers=None, failure=None):
        self.data, self.url = data, url
        self.headers = headers or {}
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, maximum):
        if self.failure:
            raise self.failure
        return self.data[:maximum]


class Opener:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


def http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return HTTPError("https://api.github.com/test?api_key=DO_NOT_LEAK", code, "secret error DO_NOT_LEAK", headers, io.BytesIO(b"secret body"))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = Clock()

    def client(self, events, **kwargs):
        opener = Opener(events)
        client = HttpClient(self.temp.name, opener=opener, sleep=self.clock.sleep, clock=self.clock.now, **kwargs)
        return client, opener

    def test_allowlist_rejects_non_https_credentials_ports_and_lookalikes(self):
        for url in ("http://api.github.com/a", "https://user:secret@api.github.com/a", "https://api.github.com:8080/a",
                    "https://api.github.com.evil.invalid/a", "https://127.0.0.1/a", "file:///tmp/a", "https://localhost/a"):
            with self.subTest(url=url), self.assertRaises(FetchError):
                validate_url(url)

    def test_malformed_url_is_sanitized_fetch_error(self):
        for url in ("https://[bad", "https://api.github.com:DO_NOT_LEAK/x", None, {}):
            with self.subTest(url=url):
                try:
                    validate_url(url)
                except FetchError as error:
                    self.assertNotIn("DO_NOT_LEAK", str(error))
                else:
                    self.fail("invalid URL must raise FetchError")

    def test_429_retry_after_and_request_count(self):
        client, opener = self.client([http_error(429, "7"), Response()])
        self.assertEqual(client.get_json("https://api.github.com/test", ttl=0), {"ok": True})
        self.assertEqual(client.requests, 2)
        self.assertEqual(len(opener.calls), 2)
        self.assertIn(7, self.clock.sleeps)

    def test_retry_after_http_date_is_bounded(self):
        client, _ = self.client([http_error(503, "Thu, 01 Jan 1970 00:17:00 GMT"), Response()])
        client.get_bytes("https://api.github.com/test", ttl=0)
        self.assertIn(20, self.clock.sleeps)

    def test_5xx_retries_and_404_does_not(self):
        client, _ = self.client([http_error(500), http_error(502), Response()])
        client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(client.requests, 3)
        self.assertEqual(self.clock.sleeps, [1, 2])
        client, opener = self.client([http_error(404), Response()])
        with self.assertRaises(FetchError) as caught:
            client.get_bytes("https://api.github.com/test?api_key=DO_NOT_LEAK", ttl=0)
        self.assertEqual(caught.exception.status, 404)
        self.assertNotIn("DO_NOT_LEAK", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_url_error_secret_details_are_not_exposed(self):
        client, _ = self.client([URLError("secret token DO_NOT_LEAK")] * 3)
        with self.assertRaises(FetchError) as caught:
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertNotIn("DO_NOT_LEAK", str(caught.exception))

    def test_retry_attempts_consume_global_request_budget(self):
        client, opener = self.client([http_error(500), Response()], max_requests=1)
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(caught.exception.reason, "network_budget_exhausted")
        self.assertEqual((client.requests, len(opener.calls)), (1, 1))

    def test_post_retry_attempts_consume_paid_budget(self):
        client, opener = self.client([http_error(500), http_error(500), Response()], max_post_attempts=2)
        with self.assertRaises(BudgetExceeded) as caught:
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1}, headers={"Authorization": "Bearer DO_NOT_LEAK"})
        self.assertEqual(caught.exception.reason, "ai_budget_exhausted")
        self.assertNotIn("DO_NOT_LEAK", str(caught.exception))
        self.assertEqual((client.post_attempts, len(opener.calls)), (2, 2))
        self.assertEqual(list(Path(self.temp.name).glob("http-*.json")), [])

    def test_budget_reasons_are_typed_safe_fetch_errors(self):
        reasons = ("network_budget_exhausted", "collection_budget_exhausted", "ai_budget_exhausted")
        for reason in reasons:
            with self.subTest(reason=reason):
                error = BudgetExceeded(reason)
                self.assertIsInstance(error, FetchError)
                self.assertEqual(error.reason, reason)
                self.assertIsNone(error.status)
                self.assertNotIn("DO_NOT_LEAK", str(error))
        for reason in ("DO_NOT_LEAK", None, {}, True):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                BudgetExceeded(reason)
            self.assertNotIn("DO_NOT_LEAK", str(caught.exception))

    def test_default_reservation_keeps_full_get_budget(self):
        client, opener = self.client([Response(), Response()], max_requests=2)
        self.assertEqual(client.reserved_post_requests, 0)
        for _ in range(2):
            client.get_json("https://api.github.com/test", ttl=0)
        with self.assertRaises(BudgetExceeded) as caught:
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual(caught.exception.reason, "network_budget_exhausted")
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (2, 0, 2))

    def test_gets_leave_reserved_attempts_for_post(self):
        client, opener = self.client([Response()] * 4, max_requests=4, max_post_attempts=2,
                                     reserved_post_requests=2)
        for _ in range(2):
            client.get_json("https://api.github.com/test", ttl=0)
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/test?api_key=DO_NOT_LEAK", ttl=0)
        self.assertEqual(caught.exception.reason, "collection_budget_exhausted")
        self.assertNotIn("DO_NOT_LEAK", str(caught.exception))
        self.assertEqual((client.requests, len(opener.calls)), (2, 2))
        for _ in range(2):
            self.assertEqual(client.post_json("https://api.deepseek.com/chat/completions", {"x": 1}), {"ok": True})
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (4, 2, 4))

    def test_reservation_shrinks_with_remaining_paid_attempts(self):
        client, opener = self.client([Response()] * 6, max_requests=6, max_post_attempts=3,
                                     reserved_post_requests=3)
        for _ in range(2):
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        for _ in range(3):
            client.get_json("https://api.github.com/test", ttl=0)
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(caught.exception.reason, "collection_budget_exhausted")
        self.assertEqual((client.requests, client.post_attempts), (5, 2))
        client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (6, 3, 6))

    def test_reservation_ceiling_stays_while_more_paid_attempts_remain(self):
        client, opener = self.client([Response()] * 4, max_requests=6, max_post_attempts=4,
                                     reserved_post_requests=2)
        client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        for _ in range(3):
            client.get_json("https://api.github.com/test", ttl=0)
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(caught.exception.reason, "collection_budget_exhausted")
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (4, 1, 4))

    def test_failed_paid_attempts_also_release_the_reservation(self):
        client, opener = self.client([http_error(500), http_error(500), Response(), Response()],
                                     max_requests=4, max_post_attempts=2, reserved_post_requests=2)
        with self.assertRaises(BudgetExceeded) as caught:
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual(caught.exception.reason, "ai_budget_exhausted")
        for _ in range(2):
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (4, 2, 4))

    def test_get_retries_cannot_use_reserved_attempts(self):
        client, opener = self.client([http_error(500), http_error(500), Response()],
                                     max_requests=3, max_post_attempts=1, reserved_post_requests=1)
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(caught.exception.reason, "collection_budget_exhausted")
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (2, 0, 2))
        client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (3, 1, 3))

    def test_post_retries_use_reserved_attempts_and_respect_total_cap(self):
        client, opener = self.client([Response(), Response(), http_error(500), http_error(500), Response()],
                                     max_requests=5, max_post_attempts=3, reserved_post_requests=3)
        for _ in range(2):
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(client.post_json("https://api.deepseek.com/chat/completions", {"x": 1}), {"ok": True})
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (5, 3, 5))
        with self.assertRaises(BudgetExceeded) as caught:
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual(caught.exception.reason, "network_budget_exhausted")
        self.assertEqual(len(opener.calls), 5)

    def test_post_retry_cannot_exceed_total_budget_when_paid_budget_remains(self):
        client, opener = self.client([http_error(500), Response()], max_requests=1,
                                     max_post_attempts=3, reserved_post_requests=3)
        with self.assertRaises(BudgetExceeded) as caught:
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual(caught.exception.reason, "network_budget_exhausted")
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (1, 1, 1))

    def test_fresh_get_cache_works_at_collection_and_total_limits(self):
        client, opener = self.client([Response(), Response(), Response()], max_requests=3,
                                     max_post_attempts=1, reserved_post_requests=1)
        url = "https://api.github.com/test?api_key=DO_NOT_LEAK"
        client.get_json(url)
        client.get_json("https://api.github.com/other", ttl=0)
        self.assertEqual(client.get_json(url), {"ok": True})
        self.assertEqual((client.requests, len(opener.calls)), (2, 2))
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/other", ttl=0)
        self.assertEqual(caught.exception.reason, "collection_budget_exhausted")
        client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual(client.get_json(url), {"ok": True})
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (3, 1, 3))
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/uncached", ttl=0)
        self.assertEqual(caught.exception.reason, "network_budget_exhausted")

    def test_budget_guards_run_before_request_creation_pacing_or_open(self):
        cases = (({"max_requests": 0}, "get", "network_budget_exhausted"),
                 ({"max_requests": 1, "max_post_attempts": 1, "reserved_post_requests": 1},
                  "get", "collection_budget_exhausted"),
                 ({"max_post_attempts": 0}, "post", "ai_budget_exhausted"))
        for kwargs, method, reason in cases:
            with self.subTest(reason=reason):
                client, opener = self.client([], **kwargs)
                with patch("paper_plot_library.http.Request") as request, self.assertRaises(BudgetExceeded) as caught:
                    if method == "post":
                        client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
                    else:
                        client.get_json("https://api.github.com/test", ttl=0)
                self.assertEqual(caught.exception.reason, reason)
                request.assert_not_called()
                self.assertEqual(opener.calls, [])
                self.assertEqual(self.clock.sleeps, [])
                self.assertEqual((client.requests, client.post_attempts), (0, 0))

    def test_reservation_is_bounded_by_total_and_paid_caps(self):
        cases = ((2, 5, 5, 2), (5, 2, 5, 2), (0, 5, 5, 0), (5, 0, 5, 0),
                 (2, 5, 10 ** 100, 2))
        for total, paid, reserved, expected in cases:
            with self.subTest(total=total, paid=paid, reserved=reserved):
                client, opener = self.client([], max_requests=total, max_post_attempts=paid,
                                             reserved_post_requests=reserved)
                self.assertEqual(client.reserved_post_requests, expected)
                self.assertEqual(opener.calls, [])

    def test_reservation_above_total_does_not_increase_the_hard_cap(self):
        client, opener = self.client([Response()], max_requests=1, max_post_attempts=5,
                                     reserved_post_requests=5)
        with self.assertRaises(BudgetExceeded) as caught:
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual(caught.exception.reason, "collection_budget_exhausted")
        client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        for method in ("get", "post"):
            with self.subTest(method=method), self.assertRaises(BudgetExceeded) as caught:
                if method == "post":
                    client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
                else:
                    client.get_json("https://api.github.com/test", ttl=0)
            self.assertEqual(caught.exception.reason, "network_budget_exhausted")
        self.assertEqual((client.max_requests, client.requests, client.post_attempts, len(opener.calls)), (1, 1, 1, 1))

    def test_zero_paid_cap_removes_reservation_and_keeps_get_budget(self):
        client, opener = self.client([Response(), Response()], max_requests=2, max_post_attempts=0,
                                     reserved_post_requests=5)
        self.assertEqual(client.reserved_post_requests, 0)
        with self.assertRaises(BudgetExceeded) as caught:
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1})
        self.assertEqual(caught.exception.reason, "ai_budget_exhausted")
        for _ in range(2):
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual((client.requests, client.post_attempts, len(opener.calls)), (2, 0, 2))

    def test_invalid_reservation_is_rejected_without_opener_creation(self):
        for reserved in (-1, True, 1.5, float("nan"), float("inf"), "DO_NOT_LEAK", None, {}):
            with self.subTest(reserved=reserved), patch("paper_plot_library.http.build_opener") as build:
                with self.assertRaises(ValueError) as caught:
                    HttpClient(self.temp.name, reserved_post_requests=reserved)
                self.assertNotIn("DO_NOT_LEAK", str(caught.exception))
                build.assert_not_called()

    def test_hashed_cache_has_no_request_url_or_credentials(self):
        client, opener = self.client([Response()])
        url = "https://api.github.com/test"
        headers = {"Authorization": "Bearer DO_NOT_LEAK"}
        client.get_json(url, params={"api_key": "DO_NOT_LEAK"}, headers=headers)
        client.get_json(url, params={"api_key": "DO_NOT_LEAK"}, headers=headers)
        self.assertEqual(len(opener.calls), 1)
        cache = next(Path(self.temp.name).glob("http-*.json"))
        text = cache.read_text()
        self.assertNotIn("DO_NOT_LEAK", text)
        self.assertNotIn(url, text)
        self.assertEqual(json.loads(text).keys(), {"at", "data"})

    def test_broken_cache_recovers_via_network(self):
        cases = ["{invalid", "{}", '{"at":"wrong","data":"e30="}', '{"at":1000,"data":"***"}',
                 '{"at":1000}', '[]']
        for case in cases:
            with self.subTest(case=case):
                client, opener = self.client([Response(), Response()])
                url = "https://api.github.com/test?case=" + str(cases.index(case))
                client.get_json(url)
                cache = sorted(Path(self.temp.name).glob("http-*.json"), key=lambda p: p.stat().st_mtime_ns)[-1]
                cache.write_text(case)
                self.assertEqual(client.get_json(url), {"ok": True})
                self.assertEqual(len(opener.calls), 2)

    def test_future_cache_timestamp_is_not_perpetually_fresh(self):
        client, opener = self.client([Response(), Response(data=b'{"new":true}')])
        client.get_json("https://api.github.com/test")
        cache = next(Path(self.temp.name).glob("http-*.json"))
        cache.write_text(json.dumps({"at": 99999999999999, "data": base64.b64encode(b'{"old":true}').decode()}))
        self.assertEqual(client.get_json("https://api.github.com/test"), {"new": True})
        self.assertEqual(len(opener.calls), 2)

    def test_oversized_response_is_rejected_without_retry(self):
        client, opener = self.client([Response(data=b"x" * 11)])
        with self.assertRaises(FetchError):
            client.get_bytes("https://api.github.com/test", max_bytes=10, ttl=0)
        self.assertEqual(len(opener.calls), 1)

    def test_malformed_content_length_is_safe(self):
        client, _ = self.client([Response(headers={"Content-Length": "DO_NOT_LEAK"})])
        try:
            client.get_bytes("https://api.github.com/test", ttl=0)
        except FetchError as error:
            self.assertNotIn("DO_NOT_LEAK", str(error))
        else:
            self.fail("invalid response length should be rejected")

    def test_incomplete_read_retries_and_never_leaks_partial_body(self):
        client, _ = self.client([Response(failure=http.client.IncompleteRead(b"DO_NOT_LEAK", 10)), Response()])
        self.assertEqual(client.get_json("https://api.github.com/test", ttl=0), {"ok": True})
        self.assertEqual(client.requests, 2)

    def test_json_and_utf8_errors_are_sanitized(self):
        for data in (b"DO_NOT_LEAK", b"\xff"):
            client, _ = self.client([Response(data=data)])
            with self.assertRaises(FetchError) as caught:
                client.get_json("https://api.github.com/test", ttl=0)
            self.assertNotIn("DO_NOT_LEAK", str(caught.exception))

    def test_invalid_query_params_are_sanitized_fetch_error(self):
        client, opener = self.client([])
        try:
            client.get_bytes("https://api.github.com/test", params="DO_NOT_LEAK", ttl=0)
        except FetchError as error:
            self.assertNotIn("DO_NOT_LEAK", str(error))
        else:
            self.fail("invalid parameter container must raise FetchError")
        self.assertEqual(opener.calls, [])

    def test_nonfinite_post_json_is_rejected_before_network(self):
        client, opener = self.client([Response()])
        with self.assertRaises(FetchError):
            client.post_json("https://api.deepseek.com/chat/completions", {"temperature": float("nan")})
        self.assertEqual(opener.calls, [])

    def test_redirects_drop_auth_cross_host_but_keep_same_host(self):
        handler = SafeRedirect()
        request = Request("https://api.github.com/test", headers={"Authorization": "Bearer test"})
        cross = handler.redirect_request(request, None, 302, "", {}, "https://raw.githubusercontent.com/a")
        same = handler.redirect_request(request, None, 302, "", {}, "https://api.github.com/next")
        self.assertIsNone(cross.get_header("Authorization"))
        self.assertEqual(same.get_header("Authorization"), "Bearer test")
        with self.assertRaises(FetchError):
            handler.redirect_request(request, None, 302, "", {}, "https://evil.invalid/a")

    def test_easyscholar_requests_respect_two_per_second(self):
        response = Response(url="https://www.easyscholar.cc/open/getPublicationRank")
        client, _ = self.client([response, response])
        client.get_json("https://www.easyscholar.cc/open/getPublicationRank", ttl=0)
        client.get_json("https://www.easyscholar.cc/open/getPublicationRank", ttl=0)
        self.assertGreaterEqual(sum(self.clock.sleeps), 0.5)


if __name__ == "__main__":
    unittest.main()
