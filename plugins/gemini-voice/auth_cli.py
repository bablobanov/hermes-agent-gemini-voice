"""``hermes gemini-voice check | status``: diagnostics that never print a secret.

Sign-in belongs to Hermes: ``hermes auth add gemini-voice:google`` in a terminal on the Hermes host. Hermes keeps
the grant in the profile's auth.json and refreshes it; the plugin never sees a token. The running gateway never
starts a sign-in: a missing or revoked one shows up as a platform status instead. ``auth``, the plugin's own
sign-in before 0.2.0, now only points to that command.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, TextIO

from .google_api import (SIGN_IN, ApiError, AuthError, GoogleClient, SignInError, TransientError, Transport, auth_fix,
                         hermes_transport)

ENV_CLIENT_ID = "GEMINI_VOICE_CLIENT_ID"
ENV_CLIENT_SECRET = "GEMINI_VOICE_CLIENT_SECRET"
ENV_NAMES = (ENV_CLIENT_ID, ENV_CLIENT_SECRET)  # Hermes reads both for the sign-in and each refresh
API_PROBES = (
    ("Calendar", "https://www.googleapis.com/calendar/v3/calendars/primary/events", {"maxResults": 1}),
    ("Drive", "https://www.googleapis.com/drive/v3/files", {"pageSize": 1, "fields": "files(id)"}),
    ("Docs", "https://docs.googleapis.com/v1/documents/gemini-voice-probe-nonexistent", None),
    ("Sheets", "https://sheets.googleapis.com/v4/spreadsheets/gemini-voice-probe-nonexistent", None),
)


@dataclass
class CliIO:
    """Everything the commands touch outside the process, injectable for tests."""
    stdout: TextIO
    read_secret: Callable[[str], Optional[str]]
    transport: Transport = hermes_transport


def run_auth(out: TextIO) -> int:
    print(f"Since gemini-voice 0.2.0 Hermes signs in to Google for the plugin. Run `{SIGN_IN}` in a terminal "
          "on the Hermes host; Hermes stores and refreshes the token. Then run `hermes gemini-voice check`.", file=out)
    return 2


def run_check(io: CliIO) -> int:
    """One request to each API through Hermes's route; prints per-API results, never secrets."""
    out = io.stdout
    if not io.read_secret(ENV_CLIENT_ID):  # the secret stays Hermes's: a missing one comes back from the route
        print(f"NOT_CONFIGURED: missing {ENV_CLIENT_ID} in the Hermes .env", file=out)
        return 1
    try:
        failures, auth_codes = _probe_apis(GoogleClient(transport=io.transport), out)
    except SignInError as exc:  # Hermes sent nothing; its own words name the reason
        print(f"AUTH_FAILED: {exc}", file=out)
        print(f"To fix it: {auth_fix(exc.code)}", file=out)
        return 1
    for code in dict.fromkeys(auth_codes):  # Google refused the token itself: the code only, never its text
        print(f"AUTH_FAILED: {code}\nTo fix it: {auth_fix(code)}", file=out)
    return 0 if not failures else 1


def _probe_apis(client: GoogleClient, out: TextIO) -> "tuple[int, list[str]]":
    failures, auth_codes = 0, []
    for name, url, params in API_PROBES:
        try:
            client.request("GET", url, params=params)
            result = "OK"
        except SignInError:
            raise  # Hermes sent nothing: the same answer for every API
        except ApiError as exc:
            result = "OK" if exc.status == 404 else f"FAILED {exc.code}"
        except AuthError as exc:
            result = f"FAILED {exc.code}"
            auth_codes.append(exc.code)
        except TransientError as exc:
            result = f"FAILED {exc.code}"
        failures += 0 if result == "OK" else 1
        print(f"{name} API: {result}", file=out)
    return failures, auth_codes


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
    sub = parser.add_subparsers(dest="gemini_voice_command", metavar="{check,status}")
    sub.add_parser("check", help="one request to each of the four Google APIs through Hermes's sign-in")
    sub.add_parser("status", help="show the platform status and task counts")
    moved = sub.add_parser("auth", help=f"moved to `{SIGN_IN}`")
    moved.add_argument("--client-secret-file", help=argparse.SUPPRESS)  # accepted so the pointer still prints


def default_io(read_secret: Callable[[str], Optional[str]]) -> CliIO:
    return CliIO(stdout=sys.stdout, read_secret=read_secret)


def handle(args: argparse.Namespace, io: CliIO, store: Any) -> int:
    command = getattr(args, "gemini_voice_command", None)
    if command == "auth":
        return run_auth(io.stdout)
    if command == "check":
        return run_check(io)
    if command == "status":
        return run_status(store, io.stdout)
    print("usage: hermes gemini-voice {check,status}", file=io.stdout)
    return 2


__all__: Sequence[str] = ("setup_parser", "handle", "default_io", "run_check", "run_status")
