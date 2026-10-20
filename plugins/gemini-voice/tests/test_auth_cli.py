import argparse
import io
import json
import unittest

from _load import load
from fake_google import DOCS, FakeGoogle

cli = load("auth_cli")
api = load("google_api")
CONFIGURED = {name: "x" for name in cli.ENV_NAMES}


class AuthPointerTests(unittest.TestCase):
    def test_auth_points_to_the_hermes_sign_in(self):
        out = io.StringIO()
        self.assertEqual(cli.run_auth(out), 2)  # nothing was signed in: not a success
        self.assertIn("hermes auth add gemini-voice:google", out.getvalue())

    def test_the_old_command_line_still_reaches_the_pointer(self):
        parser = argparse.ArgumentParser()
        cli.setup_parser(parser)
        args = parser.parse_args(["auth", "--client-secret-file", "client.json"])
        out = io.StringIO()
        self.assertEqual(cli.handle(args, cli.CliIO(stdout=out, read_secret=CONFIGURED.get), None), 2)
        self.assertIn("hermes auth add gemini-voice:google", out.getvalue())


class CheckAndStatusTests(unittest.TestCase):
    def make_io(self, fake, secrets):
        out = io.StringIO()
        return cli.CliIO(stdout=out, read_secret=secrets.get, transport=fake), out

    def test_check_probes_the_four_apis(self):
        fake = FakeGoogle()
        cli_io, out = self.make_io(fake, CONFIGURED)
        self.assertEqual(cli.run_check(cli_io), 0)
        for name in ("Calendar", "Drive", "Docs", "Sheets"):
            self.assertIn(f"{name} API: OK", out.getvalue())
        self.assertEqual(len(fake.calls), 4)

    def test_check_without_a_sign_in_names_the_command_once(self):
        fake = FakeGoogle()
        fake.refusal = api.SignInError("not_signed_in", "Not signed in to google for plugin 'gemini-voice' in this "
                                       "Hermes profile. Run `hermes auth add gemini-voice:google`.")
        cli_io, out = self.make_io(fake, CONFIGURED)
        self.assertEqual(cli.run_check(cli_io), 1)
        text = out.getvalue()
        self.assertIn("AUTH_FAILED: not_signed_in", text)
        self.assertIn("To fix it: run `hermes auth add gemini-voice:google`", text)
        self.assertNotIn(" API: ", text)

    def test_check_never_reads_the_client_secret(self):
        asked = []

        def read_secret(name):
            asked.append(name)
            if name == "GEMINI_VOICE_CLIENT_SECRET":
                raise AssertionError("the plugin read the client secret; it is Hermes's to read")
            return "id"
        fake = FakeGoogle()
        fake.refusal = api.SignInError("oauth_client_secret_missing", "GEMINI_VOICE_CLIENT_SECRET is not set.")
        out = io.StringIO()
        self.assertEqual(cli.run_check(cli.CliIO(stdout=out, read_secret=read_secret, transport=fake)), 1)
        self.assertEqual(asked, ["GEMINI_VOICE_CLIENT_ID"])
        self.assertIn("To fix it: check GEMINI_VOICE_CLIENT_ID and GEMINI_VOICE_CLIENT_SECRET in the Hermes .env",
                      out.getvalue())

    def test_check_names_the_api_google_refuses(self):
        fake = FakeGoogle()
        fake.refuse[("GET", DOCS + "/gemini-voice-probe-nonexistent")] = (403, "insufficientPermissions")
        cli_io, out = self.make_io(fake, CONFIGURED)
        self.assertEqual(cli.run_check(cli_io), 1)
        self.assertIn("Docs API: FAILED insufficient_scope", out.getvalue())
        self.assertIn("Sheets API: OK", out.getvalue())
        self.assertIn("AUTH_FAILED: insufficient_scope", out.getvalue())
        self.assertIn("To fix it: run `hermes auth add gemini-voice:google`", out.getvalue())

    def test_check_a_401_names_its_fix_and_never_google_text(self):
        def transport(method, url, headers, body, timeout):
            payload = {"error": {"code": 401, "message": "CANARY-401" + chr(10) + "second line",
                                 "status": "UNAUTHENTICATED"}}
            return 401, {}, json.dumps(payload).encode()
        out = io.StringIO()
        self.assertEqual(cli.run_check(cli.CliIO(stdout=out, read_secret=CONFIGURED.get, transport=transport)), 1)
        text = out.getvalue()
        self.assertEqual(text.count("AUTH_FAILED: unauthorized"), 1)  # once, not once per API
        self.assertIn("hermes auth refresh gemini-voice:google", text)
        self.assertNotIn("CANARY", text)

    def test_check_not_configured(self):
        fake = FakeGoogle()
        cli_io, out = self.make_io(fake, {})
        self.assertEqual(cli.run_check(cli_io), 1)
        self.assertIn("NOT_CONFIGURED: missing GEMINI_VOICE_CLIENT_ID in the Hermes .env", out.getvalue())
        self.assertEqual(fake.calls, [])

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
