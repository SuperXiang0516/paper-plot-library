"""Transport audit tests; only fake network events, never real API calls."""
import base64
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request

from paper_plot_library.http import FetchError, HttpClient, SafeRedirect, validate_url


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
        with self.assertRaises(FetchError):
            client.get_json("https://api.github.com/test", ttl=0)
        self.assertEqual((client.requests, len(opener.calls)), (1, 1))

    def test_post_retry_attempts_consume_paid_budget(self):
        client, opener = self.client([http_error(500), http_error(500), Response()], max_post_attempts=2)
        with self.assertRaises(FetchError):
            client.post_json("https://api.deepseek.com/chat/completions", {"x": 1}, headers={"Authorization": "Bearer DO_NOT_LEAK"})
        self.assertEqual((client.post_attempts, len(opener.calls)), (2, 2))
        self.assertEqual(list(Path(self.temp.name).glob("http-*.json")), [])

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
