"""Minimal Google REST client on the standard library: in-memory OAuth refresh and bounded timeouts.

The plugin never writes tokens at runtime. The refresh token comes from the plugin's own environment
variables; access tokens live only in this process's memory.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
)
DEFAULT_TIMEOUT = 20.0
_EXPIRY_MARGIN = 60.0
_AUTH_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope"}
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


class TransientError(GoogleError):
    """Network failure, rate limit or server error: retry later."""


class ApiError(GoogleError):
    """Any other HTTP error (404, 412, 400, disabled API)."""


@dataclass
class Response:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)


def urllib_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float,
) -> Tuple[int, Dict[str, str], bytes]:
    """Plain HTTPS request; HTTP errors come back as a status, network errors raise ``OSError`` or
    ``http.client.HTTPException`` (a connection dropped while the body is read)."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read() if exc.fp is not None else b""
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, payload or b""


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


class TokenSource:
    """Exchanges the plugin's own refresh token for short-lived access tokens, cached in memory."""

    def __init__(
        self, client_id: str, client_secret: str, refresh_token: str,
        transport: Transport = urllib_transport, clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._form = {"client_id": client_id, "client_secret": client_secret,
                      "refresh_token": refresh_token, "grant_type": "refresh_token"}
        self._transport, self._clock, self._timeout = transport, clock, timeout
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self.granted_scopes: Tuple[str, ...] = ()

    def token(self) -> str:
        """A valid access token, refreshed when missing or about to expire."""
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at - _EXPIRY_MARGIN:
                self._refresh()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _refresh(self) -> None:
        body = urllib.parse.urlencode(self._form).encode("ascii")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        try:
            status, _, raw = self._transport("POST", TOKEN_URL, headers, body, self._timeout)
        except _NETWORK_ERRORS as exc:
            raise TransientError("network", type(exc).__name__) from None
        payload = _parse_json(raw)
        if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
            self._token = str(payload["access_token"])
            self._expires_at = self._clock() + float(payload.get("expires_in") or 3600)
            self.granted_scopes = tuple(str(payload.get("scope") or "").split())
            return
        self._raise_refresh_error(status, payload)

    @staticmethod
    def _raise_refresh_error(status: int, payload: Any) -> None:
        code = str(payload.get("error") or "") if isinstance(payload, dict) else ""
        if code in _AUTH_ERRORS or status in (400, 401):
            raise AuthError(code or f"http_{status}", "token refresh refused", status)
        raise TransientError(f"http_{status}", "token endpoint unavailable", status)


class GoogleClient:
    """JSON requests with a bearer token; one transparent retry after a 401."""

    def __init__(self, tokens: TokenSource, transport: Transport = urllib_transport,
                 timeout: float = DEFAULT_TIMEOUT):
        self.tokens = tokens
        self._transport = transport
        self._timeout = timeout

    def request(self, method: str, url: str, *, params: Optional[Mapping[str, Any]] = None,
                body: Any = None, headers: Optional[Mapping[str, str]] = None) -> Response:
        full_url = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in (1, 2):
            sent_headers = {"Authorization": f"Bearer {self.tokens.token()}", "Accept": "application/json"}
            if data is not None:
                sent_headers["Content-Type"] = "application/json; charset=utf-8"
            sent_headers.update(headers or {})
            status, resp_headers, raw = self._send(method, full_url, sent_headers, data)
            if status == 401 and attempt == 1:
                self.tokens.invalidate()
                continue
            return self._check(status, resp_headers, raw)
        raise AuthError("unauthorized", "access token rejected twice", 401)  # pragma: no cover

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
