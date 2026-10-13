"""``hermes gemini-voice auth | check | status``: one-time operator authorization and diagnostics.

The OAuth browser step exists only here, in an interactive terminal. The running gateway never starts an
authorization flow; a broken token shows up as a platform status instead. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import secrets
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, TextIO, Tuple

from .google_api import (SCOPES, TOKEN_URL, ApiError, AuthError, GoogleClient, GoogleError, TokenSource,
                         TransientError, Transport, urllib_transport)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REDIRECT_URI = "http://localhost:1"
ENV_CLIENT_ID = "GEMINI_VOICE_CLIENT_ID"
ENV_CLIENT_SECRET = "GEMINI_VOICE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "GEMINI_VOICE_REFRESH_TOKEN"
ENV_NAMES = (ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_REFRESH_TOKEN)
API_PROBES = (
    ("Calendar", "https://www.googleapis.com/calendar/v3/calendars/primary/events", {"maxResults": 1}),
    ("Drive", "https://www.googleapis.com/drive/v3/files", {"pageSize": 1, "fields": "files(id)"}),
    ("Docs", "https://docs.googleapis.com/v1/documents/gemini-voice-probe-nonexistent", None),
    ("Sheets", "https://sheets.googleapis.com/v4/spreadsheets/gemini-voice-probe-nonexistent", None),
)


@dataclass
class CliIO:
    """Everything the commands touch outside the process, injectable for tests."""
    stdin: TextIO
    stdout: TextIO
    read_secret: Callable[[str], Optional[str]]
    save_env: Callable[[str, str], Any]
    ask: Callable[[str], str]
    ask_secret: Callable[[str], str]
    transport: Transport = urllib_transport


def pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def consent_url(client_id: str, challenge: str, state: str) -> str:
    params = {"client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
              "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def parse_redirect(pasted: str, expected_state: str) -> str:
    """The authorization code from the pasted redirect address (or a bare code)."""
    value = pasted.strip()
    if not value.lower().startswith("http"):
        if not value:
            raise ValueError("nothing was pasted")
        return value
    query = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
    if query.get("error"):
        raise ValueError(f"Google returned an error: {query['error'][0]}")
    if (query.get("state") or [""])[0] != expected_state:
        raise ValueError("the state in the address does not match this session; start again")
    code = (query.get("code") or [""])[0]
    if not code:
        raise ValueError("no code in the pasted address")
    return code


def exchange_code(code: str, client_id: str, client_secret: str, verifier: str, transport: Transport) -> Dict[str, Any]:
    form = {"grant_type": "authorization_code", "code": code, "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": REDIRECT_URI, "code_verifier": verifier}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    status, _, raw = transport("POST", TOKEN_URL, headers, urllib.parse.urlencode(form).encode("ascii"), 20.0)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        payload = {}
    if status != 200:
        raise ValueError(f"token exchange refused: {payload.get('error') or status}")
    return payload


def load_client_file(path: str) -> Tuple[str, str]:
    """Client id and secret from a Google OAuth client JSON (``installed`` or ``web``)."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    section = data.get("installed") or data.get("web") or {}
    client_id, client_secret = section.get("client_id"), section.get("client_secret")
    if not client_id or not client_secret:
        raise ValueError("not a Google OAuth client file (no installed/web client_id and client_secret)")
    return str(client_id), str(client_secret)


def _client(args: argparse.Namespace, io: CliIO) -> Tuple[str, str]:
    if getattr(args, "client_secret_file", None):
        return load_client_file(args.client_secret_file)
    client_id, client_secret = io.read_secret(ENV_CLIENT_ID), io.read_secret(ENV_CLIENT_SECRET)
    if client_id and client_secret:
        return client_id, client_secret
    return io.ask("OAuth client ID: ").strip(), io.ask_secret("OAuth client secret (hidden): ").strip()


def run_auth(args: argparse.Namespace, io: CliIO) -> int:
    """Interactive consent; saves the plugin's own three variables through Hermes's .env writer."""
    out = io.stdout
    if not io.stdin.isatty():
        print("gemini-voice auth needs an interactive terminal; it never runs under cron or the gateway.", file=out)
        return 2
    try:
        client_id, client_secret = _client(args, io)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        print("1. Open this address in a browser signed in to the Google account Hermes should serve:", file=out)
        print(consent_url(client_id, challenge, state), file=out)
        print("2. Allow every requested permission. The browser then fails to open a localhost page;", file=out)
        print("   that is expected. Copy the whole address from the address bar.", file=out)
        code = parse_redirect(io.ask("3. Paste the address here: "), state)
        tokens = exchange_code(code, client_id, client_secret, verifier, io.transport)
    except (OSError, ValueError) as exc:
        print(f"Authorization failed: {exc}", file=out)
        return 1
    return _save_tokens(tokens, client_id, client_secret, io)


def _save_tokens(tokens: Dict[str, Any], client_id: str, client_secret: str, io: CliIO) -> int:
    out = io.stdout
    refresh = str(tokens.get("refresh_token") or "")
    if not refresh:
        print("Google returned no refresh token. Remove this app at https://myaccount.google.com/permissions "
              "and run the command again.", file=out)
        return 1
    missing = [s for s in SCOPES if s not in str(tokens.get("scope") or "").split()]
    if missing:
        print("Not every permission was granted; nothing was saved. Missing: " +
              ", ".join(s.rsplit("/", 1)[-1] for s in missing), file=out)
        return 3
    for name, value in ((ENV_CLIENT_ID, client_id), (ENV_CLIENT_SECRET, client_secret), (ENV_REFRESH_TOKEN, refresh)):
        io.save_env(name, value)
    print(f"Saved {', '.join(ENV_NAMES)} to the Hermes .env. Restart the gateway to start polling.", file=out)
    return 0


def run_check(io: CliIO) -> int:
    """Refresh the token once and probe each API; prints scopes and per-API results, never secrets."""
    out = io.stdout
    values = {name: io.read_secret(name) for name in ENV_NAMES}
    absent = [name for name, value in values.items() if not value]
    if absent:
        print(f"NOT_CONFIGURED: missing {', '.join(absent)}", file=out)
        return 1
    tokens = TokenSource(values[ENV_CLIENT_ID] or "", values[ENV_CLIENT_SECRET] or "",
                         values[ENV_REFRESH_TOKEN] or "", transport=io.transport)
    try:
        tokens.token()
    except GoogleError as exc:
        print(f"{'AUTH_FAILED' if isinstance(exc, AuthError) else 'UNREACHABLE'}: {exc.code}", file=out)
        return 1
    missing = [s.rsplit("/", 1)[-1] for s in SCOPES if s not in tokens.granted_scopes]
    print("TOKEN_OK scopes: " + " ".join(s.rsplit("/", 1)[-1] for s in tokens.granted_scopes), file=out)
    if missing:
        print("MISSING_SCOPES: " + " ".join(missing), file=out)
    failures = _probe_apis(GoogleClient(tokens, transport=io.transport), out)
    return 0 if not missing and not failures else 1


def _probe_apis(client: GoogleClient, out: TextIO) -> int:
    failures = 0
    for name, url, params in API_PROBES:
        try:
            client.request("GET", url, params=params)
            result = "OK"
        except ApiError as exc:
            result = "OK" if exc.status == 404 else f"FAILED {exc.code}"
        except (AuthError, TransientError) as exc:
            result = f"FAILED {exc.code}"
        failures += 0 if result == "OK" else 1
        print(f"{name} API: {result}", file=out)
    return failures


def run_status(store: Any, out: TextIO) -> int:
    """Platform status and journal counts from the plugin's state file (no titles, no task text)."""
    data = store.get("journal", None) or {}
    status = data.get("status") or {}
    print(f"status: {status.get('state', 'unknown')} {status.get('code') or ''} at {status.get('at') or '-'}".rstrip(), file=out)
    if status.get("message"):
        print(f"message: {status['message']}", file=out)
    print(f"watching since: {data.get('since') or '-'}; last successful poll: {data.get('last_poll_ok') or '-'}", file=out)
    counts: Dict[str, int] = {}
    for record in (data.get("objects") or {}).values():
        counts[record.get("state", "?")] = counts.get(record.get("state", "?"), 0) + 1
    print("tasks: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"), file=out)
    return 0


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="gemini_voice_command", metavar="{auth,check,status}")
    auth = sub.add_parser("auth", help="authorize the plugin's own Google access (interactive, once)")
    auth.add_argument("--client-secret-file", help="Google OAuth client JSON to take the client id/secret from")
    sub.add_parser("check", help="verify the token and the four Google APIs")
    sub.add_parser("status", help="show the platform status and task counts")


def default_io(read_secret: Callable[[str], Optional[str]], save_env: Callable[[str, str], Any]) -> CliIO:
    return CliIO(stdin=sys.stdin, stdout=sys.stdout, read_secret=read_secret, save_env=save_env,
                 ask=input, ask_secret=getpass.getpass)


def handle(args: argparse.Namespace, io: CliIO, store: Any) -> int:
    command = getattr(args, "gemini_voice_command", None)
    if command == "auth":
        return run_auth(args, io)
    if command == "check":
        return run_check(io)
    if command == "status":
        return run_status(store, io.stdout)
    print("usage: hermes gemini-voice {auth,check,status}", file=io.stdout)
    return 2


__all__: Sequence[str] = ("setup_parser", "handle", "default_io", "run_auth", "run_check", "run_status")
