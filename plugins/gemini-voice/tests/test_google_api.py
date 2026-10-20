import json
import sys
import types
import unittest
from unittest import mock

from _load import load
from fake_google import FakeGoogle

api = load("google_api")
DRIVE = "https://www.googleapis.com/drive/v3/files"


def client(fake):
    return api.GoogleClient(transport=fake)


class ClientTests(unittest.TestCase):
    def test_no_token_from_the_plugin(self):
        fake = FakeGoogle()  # raises if the plugin sends an Authorization header
        self.assertEqual(client(fake).request("GET", DRIVE).status, 200)

    def test_401_is_final(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            return 401, {}, b"{}"
        with self.assertRaises(api.AuthError) as ctx:
            client(transport).request("GET", DRIVE)
        self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertEqual(len(calls), 1)  # Hermes refreshes by expiry; a retry would only repeat the 401

    def test_a_refusal_of_the_route_passes_through(self):
        fake = FakeGoogle()
        fake.refusal = api.SignInError("not_signed_in", "Run `hermes auth add gemini-voice:google`.")
        with self.assertRaises(api.SignInError):
            client(fake).request("GET", DRIVE)
        self.assertEqual(fake.calls, [])

    def test_network_failure_is_transient(self):
        fake = FakeGoogle()
        fake.offline = True
        with self.assertRaises(api.TransientError) as ctx:
            client(fake).request("GET", DRIVE)
        self.assertEqual(ctx.exception.code, "network")

    def test_connection_dropped_while_reading_is_transient(self):
        import http.client

        def dropping(*_args):
            raise http.client.IncompleteRead(b"{", 120)
        with self.assertRaises(api.TransientError):
            client(dropping).request("GET", DRIVE)


class NotSignedIn(RuntimeError):
    pass


class HermesAuthError(RuntimeError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class HttpxError(Exception):
    pass


class RouteTests(unittest.TestCase):
    """``hermes_transport`` against a stand-in for Hermes's modules: what goes in, what each refusal becomes."""

    def setUp(self):
        self.sent = []
        self.raise_exc = None
        self.reply = types.SimpleNamespace(status=200, headers={"content-type": "application/json"}, body=b'{"ok": 1}')

        def credentialed_provider_request(provider, method, url, *, json=None, headers=None, timeout=30.0):
            self.sent.append({"provider": provider, "method": method, "url": url, "json": json,
                              "headers": dict(headers or {}), "timeout": timeout})
            if self.raise_exc is not None:
                raise self.raise_exc
            return self.reply

        hermes_cli = types.ModuleType("hermes_cli")
        constants = types.ModuleType("hermes_cli.auth_constants")
        constants.AuthError = HermesAuthError
        requests = types.ModuleType("hermes_cli.plugin_provider_requests")
        requests.ProviderNotSignedIn = NotSignedIn
        requests.credentialed_provider_request = credentialed_provider_request
        httpx = types.ModuleType("httpx")
        httpx.HTTPError = HttpxError
        patcher = mock.patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.auth_constants": constants,
                                                "hermes_cli.plugin_provider_requests": requests, "httpx": httpx})
        patcher.start()
        self.addCleanup(patcher.stop)

    def failure(self, exc):
        self.raise_exc = exc
        with self.assertRaises(api.GoogleError) as ctx:
            api.GoogleClient().request("GET", DRIVE)
        return ctx.exception

    def test_request_goes_to_the_route_as_json_without_a_token(self):
        resp = api.GoogleClient().request("POST", "https://docs.googleapis.com/v1/documents/d:batchUpdate",
                                          body={"requests": [{"insertText": {"text": "Привет"}}]},
                                          headers={"If-Match": "e1"})
        self.assertEqual(resp.body, {"ok": 1})
        sent = self.sent[0]
        self.assertEqual(sent["provider"], "google")
        self.assertEqual(sent["json"], {"requests": [{"insertText": {"text": "Привет"}}]})
        self.assertEqual(sent["headers"]["If-Match"], "e1")
        self.assertEqual(sent["headers"]["Accept"], "application/json")
        self.assertNotIn("Authorization", sent["headers"])
        self.assertEqual(api.GoogleClient().request("GET", DRIVE).status, 200)
        self.assertIsNone(self.sent[1]["json"])

    def test_not_signed_in_keeps_hermes_words(self):
        exc = self.failure(NotSignedIn("Not signed in to google for plugin 'gemini-voice' in this Hermes profile. "
                                       "Run `hermes auth add gemini-voice:google`."))
        self.assertIsInstance(exc, api.SignInError)
        self.assertEqual(exc.code, "not_signed_in")
        self.assertIn("hermes auth add gemini-voice:google", str(exc))

    def test_client_problems_need_the_operator(self):
        for code in ("oauth_client_id_missing", "oauth_client_secret_missing", "invalid_client",
                     "unauthorized_client", "invalid_scope"):
            exc = self.failure(HermesAuthError(f"gemini-voice:google: refused ({code}).", code))
            self.assertIsInstance(exc, api.SignInError, code)
            self.assertEqual(exc.code, code)

    def test_a_failed_refresh_is_retried_later(self):
        exc = self.failure(HermesAuthError("gemini-voice:google: the token endpoint failed with HTTP 503.",
                                           "oauth_refresh_failed"))
        self.assertIsInstance(exc, api.TransientError)
        self.assertEqual(exc.code, "oauth_refresh_failed")

    def test_lock_timeout_and_network_are_transient(self):
        self.assertEqual(self.failure(TimeoutError("auth store lock")).code, "auth_store_busy")
        exc = self.failure(HttpxError("ConnectError"))
        self.assertIsInstance(exc, api.TransientError)
        self.assertEqual((exc.code, str(exc)), ("network", "network: HttpxError"))
        self.assertIsInstance(self.failure(ConnectionResetError("reset")), api.TransientError)

    def test_refusals_of_the_route_need_the_operator(self):
        refused = self.failure(PermissionError("Refusing to send the google sign-in to example.com"))
        self.assertIsInstance(refused, api.SignInError)  # PermissionError is an OSError: never "network"
        self.assertEqual(refused.code, "route_refused")
        self.assertEqual(self.failure(ValueError("No signed-in requests for provider 'google'")).code, "route_refused")
        unexpected = self.failure(KeyError("access_token"))
        self.assertEqual((unexpected.code, str(unexpected)), ("route_failed", "route_failed: KeyError"))

    def test_hermes_without_the_route(self):
        with mock.patch.dict(sys.modules, {"hermes_cli.plugin_provider_requests": None}):
            exc = self.failure(None)
        self.assertEqual(exc.code, "hermes_too_old")
        self.assertEqual(self.sent, [])


class ErrorMappingTests(unittest.TestCase):
    def check(self, status, reason, expected, code):
        def transport(method, url, headers, body, timeout):
            payload = {"error": {"code": status, "message": "m", "errors": [{"reason": reason}]}}
            return status, {}, json.dumps(payload).encode()
        with self.assertRaises(expected) as ctx:
            api.GoogleClient(transport=transport).request("GET", "https://example.test/x")
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
