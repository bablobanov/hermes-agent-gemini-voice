import unittest

from _load import load
from fake_google import FakeGoogle

api = load("google_api")


def client(fake, clock=None):
    tokens = api.TokenSource("cid", "csecret", "rtoken-SECRET", transport=fake,
                             clock=clock or (lambda: 1000.0))
    return api.GoogleClient(tokens, transport=fake)


class TokenTests(unittest.TestCase):
    def test_refresh_once_and_cache(self):
        fake = FakeGoogle()
        c = client(fake)
        c.request("GET", "https://www.googleapis.com/drive/v3/files")
        c.request("GET", "https://www.googleapis.com/drive/v3/files")
        self.assertEqual(fake.refreshes, 1)
        self.assertEqual(fake.calls[0][3]["grant_type"], "refresh_token")

    def test_refresh_again_near_expiry(self):
        fake = FakeGoogle()
        now = [1000.0]
        c = client(fake, clock=lambda: now[0])
        c.request("GET", "https://www.googleapis.com/drive/v3/files")
        now[0] += 3600 - 30  # inside the 60 s margin
        c.request("GET", "https://www.googleapis.com/drive/v3/files")
        self.assertEqual(fake.refreshes, 2)

    def test_revoked_token_is_an_auth_error_without_secrets(self):
        fake = FakeGoogle()
        fake.revoked = True
        with self.assertRaises(api.AuthError) as ctx:
            client(fake).request("GET", "https://www.googleapis.com/drive/v3/files")
        self.assertEqual(ctx.exception.code, "invalid_grant")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_401_triggers_one_refresh_and_retry(self):
        fake = FakeGoogle()
        c = client(fake)
        c.request("GET", "https://www.googleapis.com/drive/v3/files")
        fake.reject_access_once = True
        self.assertEqual(c.request("GET", "https://www.googleapis.com/drive/v3/files").status, 200)
        self.assertEqual(fake.refreshes, 2)

    def test_network_failure_is_transient(self):
        fake = FakeGoogle()
        fake.offline = True
        with self.assertRaises(api.TransientError) as ctx:
            client(fake).request("GET", "https://www.googleapis.com/drive/v3/files")
        self.assertEqual(ctx.exception.code, "network")

    def test_connection_dropped_while_reading_is_transient(self):
        import http.client
        fake = FakeGoogle()
        c = client(fake)
        c.request("GET", "https://www.googleapis.com/drive/v3/files")

        def dropping(*_args):
            raise http.client.IncompleteRead(b"{", 120)
        c._transport = dropping
        with self.assertRaises(api.TransientError):
            c.request("GET", "https://www.googleapis.com/drive/v3/files")


class ErrorMappingTests(unittest.TestCase):
    def check(self, status, reason, expected, code):
        def transport(method, url, headers, body, timeout):
            if "oauth2" in url:
                return 200, {}, b'{"access_token": "at", "expires_in": 3600}'
            payload = {"error": {"code": status, "message": "m", "errors": [{"reason": reason}]}}
            import json
            return status, {}, json.dumps(payload).encode()
        tokens = api.TokenSource("a", "b", "c", transport=transport)
        with self.assertRaises(expected) as ctx:
            api.GoogleClient(tokens, transport=transport).request("GET", "https://example.test/x")
        self.assertEqual(ctx.exception.code, code)

    def test_mapping(self):
        self.check(503, "backendError", api.TransientError, "http_503")
        self.check(408, "", api.TransientError, "http_408")
        self.check(429, "rateLimitExceeded", api.TransientError, "http_429")
        self.check(403, "userRateLimitExceeded", api.TransientError, "http_403")
        self.check(403, "insufficientPermissions", api.AuthError, "insufficient_scope")
        self.check(403, "accessNotConfigured", api.ApiError, "api_disabled")
        self.check(404, "notFound", api.ApiError, "http_404")
        self.check(412, "conditionNotMet", api.ApiError, "http_412")


if __name__ == "__main__":
    unittest.main()
