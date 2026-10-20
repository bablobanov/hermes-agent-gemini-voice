"""Minimal Google REST client: every request goes through Hermes's plugin OAuth route, with bounded timeouts.

The plugin holds no token. Its Google client is declared under ``oauth:`` in plugin.yaml; the owner signs in
once with ``hermes auth add gemini-voice:google``, Hermes keeps the grant in the profile's auth.json, refreshes
it and attaches the bearer only for the declared hosts (``credentialed_provider_request``,
NousResearch/hermes-agent#139077).
"""

from __future__ import annotations

import http.client
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

OAUTH_ID = "google"  # the id of the plugin's entry under oauth: in plugin.yaml
SIGN_IN = "hermes auth add gemini-voice:google"
DEFAULT_TIMEOUT = 20.0
# Hermes AuthError codes a retry can clear (a token refresh that failed on the network or a 5xx);
# every other code needs the operator: the client in .env, or a new sign-in.
_ROUTE_RETRY_CODES = {"oauth_refresh_failed"}
# Codes fixed in the Hermes .env (the OAuth client of the owner's Google Cloud project), not by signing in again.
CLIENT_CODES = {"oauth_client_id_missing", "oauth_client_secret_missing", "invalid_client", "unauthorized_client"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED", "quotaExceeded"}
_DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}
# A dropped connection can surface while the body is read (http.client.IncompleteRead), not only as OSError.
_NETWORK_ERRORS = (OSError, http.client.HTTPException)
_SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}

# (method, url, headers, body, timeout) -> (status, headers, body)
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]


class GoogleError(Exception):
    """Base error with a short machine-readable ``code`` and the HTTP ``status`` (0 = no response)."""

    def __init__(self, code: str, message: str = "", status: int = 0):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.status = status


class AuthError(GoogleError):
    """Authorization is unusable (revoked, expired, wrong client, missing scope): needs the operator."""


class SignInError(AuthError):
    """Hermes refused before sending anything: no sign-in, a revoked one, or a client it cannot use."""


class TransientError(GoogleError):
    """Network failure, rate limit or server error: retry later."""


class ApiError(GoogleError):
    """Any other HTTP error (404, 412, 400, disabled API)."""


@dataclass
class Response:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)


def _parse_json(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"raw": raw[:200].decode("utf-8", "replace")}


def _error_reasons(body: Any) -> Tuple[List[str], str]:
    """Reasons and message from a Google JSON error body (``error.errors[]`` and ``error.details[]``)."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return [], ""
    reasons = [str(e.get("reason")) for e in error.get("errors") or [] if isinstance(e, dict) and e.get("reason")]
    reasons += [str(d.get("reason")) for d in error.get("details") or [] if isinstance(d, dict) and d.get("reason")]
    if error.get("status"):
        reasons.append(str(error["status"]))
    return reasons, str(error.get("message") or "")[:200]


def _route_error(exc: Exception, not_signed_in: type, hermes_auth_error: type, network: tuple) -> GoogleError:
    """Hermes's refusal or failure as the plugin's error: the operator must act, or a retry will do.

    Hermes rebuilds every error past its checks without traceback, chain or request, so none carries a token.
    The texts Hermes writes itself are kept (they name the fix, e.g. ``hermes auth add ...``, and never echo
    the token endpoint's body); a network or unexpected failure keeps only its type."""
    if isinstance(exc, not_signed_in):  # no grant, a revoked one, or an oauth: entry changed since sign-in
        return SignInError("not_signed_in", str(exc)[:300])
    if isinstance(exc, hermes_auth_error):
        code = str(getattr(exc, "code", "") or "hermes_auth_error")
        # Hermes words these itself and never echoes the token endpoint's body.
        return (TransientError if code in _ROUTE_RETRY_CODES else SignInError)(code, str(exc)[:300])
    if isinstance(exc, TimeoutError):  # Hermes waited too long for its auth store lock; an OSError, so first
        return TransientError("auth_store_busy", "the Hermes auth store stayed locked")
    if isinstance(exc, (PermissionError, ValueError)):  # a host outside oauth: hosts, or a broken oauth: block
        return SignInError("route_refused", f"{type(exc).__name__}: {str(exc)[:300]}")
    if isinstance(exc, network):
        return TransientError("network", type(exc).__name__)
    return SignInError("route_failed", type(exc).__name__)


def auth_fix(code: str) -> str:
    """What the operator does about an authorization failure with this code; never a secret or a token."""
    if code in CLIENT_CODES:
        return ("check GEMINI_VOICE_CLIENT_ID and GEMINI_VOICE_CLIENT_SECRET in the Hermes .env, then restart the "
                "gateway")
    if code in ("not_signed_in", "insufficient_scope"):
        return f"run `{SIGN_IN}` on the Hermes host and allow all four permissions; polling resumes by itself"
    if code == "unauthorized":
        return ("nothing at first: Hermes refreshes the token when it expires and polling retries every 15 minutes "
                "(`hermes auth refresh gemini-voice:google` refreshes it now); if it keeps failing, "
                f"run `{SIGN_IN}`")
    if code == "hermes_too_old":
        return "update Hermes to a release with the plugin OAuth route"
    # route_refused, route_failed, invalid_scope and anything new: a new sign-in would not help
    return "check that this Hermes release supports this version of the plugin (the reason is above)"


def hermes_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float,
) -> Tuple[int, Dict[str, str], bytes]:
    """Send through Hermes's route for the plugin's ``oauth:`` entry: Hermes attaches the token, refreshes it,
    sends only to the declared hosts and follows no redirect. The plugin never receives the token."""
    try:
        import httpx
        from hermes_cli.auth_constants import AuthError as HermesAuthError
        from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request
    except ImportError as exc:
        raise SignInError("hermes_too_old", f"this Hermes has no plugin OAuth route ({exc.name})") from None
    try:
        resp = credentialed_provider_request(OAUTH_ID, method, url, json=json.loads(body) if body else None,
                                             headers=headers, timeout=timeout)
    except Exception as exc:
        raise _route_error(exc, ProviderNotSignedIn, HermesAuthError, (OSError, httpx.HTTPError)) from None
    return resp.status, dict(resp.headers), resp.body


class GoogleClient:
    """JSON requests to Google. The default transport is Hermes's OAuth route; tests pass a fake one."""

    def __init__(self, transport: Transport = hermes_transport, timeout: float = DEFAULT_TIMEOUT):
        self._transport = transport
        self._timeout = timeout

    def request(self, method: str, url: str, *, params: Optional[Mapping[str, Any]] = None,
                body: Any = None, headers: Optional[Mapping[str, str]] = None) -> Response:
        full_url = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        sent_headers = {"Accept": "application/json"}
        if data is not None:
            sent_headers["Content-Type"] = "application/json; charset=utf-8"
        sent_headers.update(headers or {})
        # A 401 is final: Hermes refreshes the grant by its expiry, and a revoked one fails the next refresh.
        status, resp_headers, raw = self._send(method, full_url, sent_headers, data)
        return self._check(status, resp_headers, raw)

    def _send(self, method: str, url: str, headers: Dict[str, str],
              data: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        try:
            return self._transport(method, url, headers, data, self._timeout)
        except _NETWORK_ERRORS as exc:
            raise TransientError("network", type(exc).__name__) from None

    @staticmethod
    def _check(status: int, headers: Dict[str, str], raw: bytes) -> Response:
        body = _parse_json(raw)
        if 200 <= status < 300:
            return Response(status, body, headers)
        reasons, message = _error_reasons(body)
        if status == 401:
            raise AuthError("unauthorized", message, status)
        if status in (408, 429) or status >= 500 or (status == 403 and _RATE_REASONS.intersection(reasons)):
            raise TransientError(f"http_{status}", message, status)
        if status == 403 and _SCOPE_REASONS.intersection(reasons):
            raise AuthError("insufficient_scope", message, status)
        if status == 403 and _DISABLED_REASONS.intersection(reasons):
            raise ApiError("api_disabled", message, status)
        raise ApiError(f"http_{status}", message, status)
