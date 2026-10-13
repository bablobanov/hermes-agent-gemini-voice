# Security

Hermes Agent Gemini Voice turns objects in one Google account into tasks for one Hermes profile. It is not a
multi-user service. Only the current 0.x line is in scope for fixes; there is no guaranteed response time.

Trust boundary: a task is accepted only from a Google object the authorized account itself created (see
"Safety and disclosure" in the README). Anyone who controls that Google account, or the phone running the
Gemini app with it, can give Hermes tasks with the toolset you configured for the `gemini_voice` platform.
Keep Hermes's normal approvals and your agent rules in force, and give this platform only the tools you are
comfortable using by voice.

Do not open a public issue for a vulnerability and never post tokens, client secrets, document contents or
logs. Use this repository's **Security → Report a vulnerability** (GitHub private vulnerability reporting).
Include the affected version, a minimal synthetic reproducer and the impact.

If a token or client secret leaks: revoke the app's access at https://myaccount.google.com/permissions, reset
the OAuth client secret in Google Cloud Console, run `hermes gemini-voice auth` again and restart the gateway.
Disable the plugin (`hermes plugins disable gemini-voice`) while you investigate.
