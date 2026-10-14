"""What the platform entry tells the gateway: a home channel only when the operator names one."""

from __future__ import annotations

import unittest
from unittest import mock

from _load import load

CREDENTIALS = {"GEMINI_VOICE_CLIENT_ID": "id", "GEMINI_VOICE_CLIENT_SECRET": "secret",
               "GEMINI_VOICE_REFRESH_TOKEN": "token"}


class HomeChannelTests(unittest.TestCase):
    def enablement(self, **env: str):
        package = load("")
        values = {**CREDENTIALS, **env}
        with mock.patch.object(package, "_read_secret", lambda name: values.get(name) or None):
            return package._env_enablement()

    def test_no_home_channel_unless_named(self):
        # Hermes broadcasts its own notices to every home channel: each would become a new Google object.
        self.assertIsNone(self.enablement())

    def test_named_home_channel(self):
        self.assertEqual(self.enablement(GEMINI_VOICE_HOME_CHANNEL="doc")["home_channel"]["chat_id"], "doc")
        event = self.enablement(GEMINI_VOICE_HOME_CHANNEL="event-0730")["home_channel"]
        self.assertEqual(event["chat_id"], "event-0730")

    def test_invalid_home_channel_falls_back_to_a_document(self):
        with self.assertLogs("gemini_voice", "WARNING") as logs:
            home = self.enablement(GEMINI_VOICE_HOME_CHANNEL="event-2561")["home_channel"]
        self.assertEqual(home["chat_id"], "doc")
        self.assertIn("GEMINI_VOICE_HOME_CHANNEL", logs.output[0])

    def test_not_configured_without_credentials(self):
        package = load("")
        with mock.patch.object(package, "_read_secret", lambda name: "doc" if name.endswith("HOME_CHANNEL") else None):
            self.assertIsNone(package._env_enablement())


if __name__ == "__main__":
    unittest.main()
