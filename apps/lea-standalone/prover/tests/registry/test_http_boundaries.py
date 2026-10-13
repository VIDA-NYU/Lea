"""Real urllib redirect processing with synthetic transport; no network or keys.
Run: python -m tests.registry.test_http_boundaries
"""
import io
import unittest
import urllib.request
from email.message import Message
from unittest.mock import patch
from urllib.response import addinfourl

from lea.http_tools import make_handler


class HttpBoundaryTests(unittest.TestCase):
    def test_redirects_never_issue_an_authenticated_second_request(self):
        for code in (301, 302, 303, 307, 308):
            for location in ("http://127.0.0.1:8001/", "https://192.168.1.1/", "https://other.example/", "/next"):
                with self.subTest(code=code, location=location):
                    seen = []
                    class Transport(urllib.request.HTTPSHandler):
                        def https_open(self, request):
                            seen.append((request.full_url, request.get_header("Authorization")))
                            headers = Message()
                            headers["Location"] = location
                            result = addinfourl(io.BytesIO(b"redirect"), headers, request.full_url, code)
                            result.msg = "Redirect"
                            return result
                    build = urllib.request.build_opener
                    def opener(*handlers):
                        return build(urllib.request.ProxyHandler({}), Transport(), *handlers)
                    with patch("lea.http_tools._is_public", return_value=True), patch.dict("os.environ", {"TEST_KEY": "dummy-secret"}), patch("urllib.request.build_opener", opener):
                        result = make_handler({"url": "https://public.example/", "auth_key_name": "TEST_KEY"})({})
                    self.assertEqual(seen, [("https://public.example/", "Bearer dummy-secret")])
                    self.assertIn("redirect", result.lower())
                    self.assertNotIn("dummy-secret", result)

    def test_public_https_success_keeps_authentication_and_timeout(self):
        seen = []
        class Opener:
            def open(self, request, timeout):
                seen.append((request.get_header("X-api-key"), timeout))
                return addinfourl(io.BytesIO(b"success dummy-secret"), Message(), request.full_url, 200)
        with patch("lea.http_tools._is_public", return_value=True), patch.dict("os.environ", {"TEST_KEY": "dummy-secret"}), patch("urllib.request.build_opener", return_value=Opener()):
            result = make_handler({"url": "https://public.example/", "auth_key_name": "TEST_KEY", "auth_header": "X-Api-Key: {key}", "timeout": 7})({})
        self.assertEqual(seen, [("dummy-secret", 7)])
        self.assertEqual(result, "success [redacted]")


if __name__ == "__main__":
    unittest.main()
