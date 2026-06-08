import tempfile
import unittest
from pathlib import Path

from proxy import ProfileProxyStore, redact_proxy_url, request_proxies, validate_proxy_url


class ProxyTests(unittest.TestCase):
    def test_accepts_supported_proxy_schemes(self):
        for value in (
            "http://localhost:8080",
            "https://user:pass@example.com:8443",
            "socks5://127.0.0.1:1080",
            "socks5h://user:pass@example.com:1080",
        ):
            self.assertEqual(validate_proxy_url(value), value)

    def test_rejects_invalid_scheme_or_missing_port(self):
        with self.assertRaises(ValueError):
            validate_proxy_url("ftp://example.com:21")
        with self.assertRaises(ValueError):
            validate_proxy_url("http://example.com")

    def test_maps_proxy_to_http_and_https_and_redacts_credentials(self):
        value = "socks5h://secret:password@example.com:1080"
        self.assertEqual(request_proxies(value), {"http": value, "https": value})
        redacted = redact_proxy_url(value)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("password", redacted)
        self.assertEqual(redacted, "socks5h://***:***@example.com:1080")

    def test_stores_proxy_per_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with ProfileProxyStore(path) as store:
                store.set(4, "http://proxy.example:8080")
                self.assertEqual(store.get(4), "http://proxy.example:8080")
                store.set(4, None)
                self.assertIsNone(store.get(4))


if __name__ == "__main__":
    unittest.main()
