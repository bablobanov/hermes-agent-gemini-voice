import argparse
import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from _load import load
from fake_google import ALL_SCOPES, FakeGoogle

cli = load("auth_cli")
api = load("google_api")


class Tty(io.StringIO):
    def isatty(self):
        return True


class Terminal:
    """A scripted operator: answers prompts, records saved variables, captures output."""

    def __init__(self, answers, secrets=None, tty=True, token_reply=None):
        self.answers = list(answers)
        self.saved = {}
        self.out = io.StringIO()
        self.prompts = []
        self.token_reply = token_reply or {"refresh_token": "1//REFRESH-SECRET", "access_token": "at",
                                           "scope": ALL_SCOPES}
        self.token_form = None
        self.io = cli.CliIO(stdin=Tty() if tty else io.StringIO(), stdout=self.out,
                            read_secret=lambda name: (secrets or {}).get(name), save_env=self.saved.__setitem__,
                            ask=self._ask, ask_secret=self._ask, transport=self._transport)

    def _ask(self, prompt):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        return answer(self) if callable(answer) else answer

    def _transport(self, method, url, headers, body, timeout):
        self.token_form = dict(urllib.parse.parse_qsl(body.decode()))
        return 200, {}, json.dumps(self.token_reply).encode()

    def consent_params(self):
        url = [line for line in self.out.getvalue().splitlines() if line.startswith("https://accounts.google.com")][0]
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def redirect_with_state(term):
    state = term.consent_params()["state"]
    return f"http://localhost:1/?state={state}&code=4/CODE&scope={urllib.parse.quote(ALL_SCOPES)}"


def args(**kw):
    return argparse.Namespace(gemini_voice_command="auth", client_secret_file=kw.get("file"))


class AuthTests(unittest.TestCase):
    def test_full_consent_saves_three_variables_and_prints_no_secret(self):
        term = Terminal(["client-id.apps.googleusercontent.com", "CLIENT-SECRET", redirect_with_state])
        self.assertEqual(cli.run_auth(args(), term.io), 0)
        self.assertEqual(set(term.saved), set(cli.ENV_NAMES))
        self.assertEqual(term.saved[cli.ENV_REFRESH_TOKEN], "1//REFRESH-SECRET")
        printed = term.out.getvalue()
        self.assertNotIn("REFRESH-SECRET", printed)
        self.assertNotIn("CLIENT-SECRET", printed)
        params = term.consent_params()
        self.assertEqual(params["scope"].split(), list(api.SCOPES))
        self.assertEqual((params["access_type"], params["prompt"], params["code_challenge_method"]),
                         ("offline", "consent", "S256"))
        self.assertEqual(term.token_form["grant_type"], "authorization_code")
        self.assertEqual(term.token_form["code"], "4/CODE")
        self.assertTrue(term.token_form["code_verifier"])

    def test_refuses_without_a_terminal(self):
        term = Terminal([], tty=False)
        self.assertEqual(cli.run_auth(args(), term.io), 2)
        self.assertEqual(term.prompts, [])
        self.assertEqual(term.saved, {})

    def test_state_mismatch_is_rejected(self):
        term = Terminal(["id", "secret", "http://localhost:1/?state=forged&code=4/CODE"])
        self.assertEqual(cli.run_auth(args(), term.io), 1)
        self.assertIn("state", term.out.getvalue())
        self.assertEqual(term.saved, {})

    def test_partial_consent_saves_nothing(self):
        scope = "https://www.googleapis.com/auth/documents"
        term = Terminal(["id", "secret", redirect_with_state],
                        token_reply={"refresh_token": "r", "scope": scope})
        self.assertEqual(cli.run_auth(args(), term.io), 3)
        self.assertIn("calendar.events.owned", term.out.getvalue())
        self.assertEqual(term.saved, {})

    def test_missing_refresh_token(self):
        term = Terminal(["id", "secret", redirect_with_state], token_reply={"access_token": "a", "scope": ALL_SCOPES})
        self.assertEqual(cli.run_auth(args(), term.io), 1)
        self.assertIn("myaccount.google.com/permissions", term.out.getvalue())

    def test_client_file_is_used_when_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            path.write_text(json.dumps({"installed": {"client_id": "file-id", "client_secret": "file-secret"}}))
            term = Terminal([redirect_with_state])
            self.assertEqual(cli.run_auth(args(file=str(path)), term.io), 0)
        self.assertEqual(term.saved[cli.ENV_CLIENT_ID], "file-id")
        self.assertEqual(len(term.prompts), 1)


class CheckAndStatusTests(unittest.TestCase):
    def make_io(self, fake, secrets):
        out = io.StringIO()
        return cli.CliIO(stdin=io.StringIO(), stdout=out, read_secret=secrets.get, save_env=lambda *_: None,
                         ask=input, ask_secret=input, transport=fake), out

    def test_check_reports_scopes_and_apis(self):
        fake = FakeGoogle()
        secrets = {name: "x" for name in cli.ENV_NAMES}
        cli_io, out = self.make_io(fake, secrets)
        self.assertEqual(cli.run_check(cli_io), 0)
        text = out.getvalue()
        self.assertIn("TOKEN_OK scopes: calendar.events.owned drive.metadata.readonly documents spreadsheets", text)
        for name in ("Calendar", "Drive", "Docs", "Sheets"):
            self.assertIn(f"{name} API: OK", text)

    def test_check_revoked(self):
        fake = FakeGoogle()
        fake.revoked = True
        cli_io, out = self.make_io(fake, {name: "x" for name in cli.ENV_NAMES})
        self.assertEqual(cli.run_check(cli_io), 1)
        self.assertIn("AUTH_FAILED: invalid_grant", out.getvalue())

    def test_check_not_configured(self):
        cli_io, out = self.make_io(FakeGoogle(), {})
        self.assertEqual(cli.run_check(cli_io), 1)
        self.assertIn("NOT_CONFIGURED", out.getvalue())

    def test_status_prints_counts_without_titles(self):
        store = {"journal": {"status": {"state": "retrying", "code": "auth_failed", "message": "m", "at": "t"},
                             "objects": {"doc.1": {"state": "done", "title": "Секретное название"},
                                         "doc.2": {"state": "done"}, "cal.3": {"state": "failed"}}}}

        class Store:
            def get(self, key, default=None):
                return store.get(key, default)
        out = io.StringIO()
        self.assertEqual(cli.run_status(Store(), out), 0)
        self.assertIn("status: retrying auth_failed", out.getvalue())
        self.assertIn("tasks: done=2, failed=1", out.getvalue())
        self.assertNotIn("Секретное", out.getvalue())


if __name__ == "__main__":
    unittest.main()
