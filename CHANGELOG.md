# Changelog

## 0.1.0

- First release: the `gemini_voice` gateway platform. New Google Calendar events, Docs and Sheets with a label
  in the title, created by the account owner, become tasks for Hermes; the final reply replaces the
  plugin's acknowledgement in the same object for the Gemini app to read aloud
- The plugin's own OAuth token (`calendar.events.owned`, `drive.metadata.readonly`, `documents`,
  `spreadsheets`), refreshed in memory only; `hermes gemini-voice auth | check | status`
- Owner-only boundary: invitations, shared files, events added by other people and the plugin's own outputs
  are never tasks
- One shared chat, one task at a time; durable journal with at-most-once dispatch across restarts; answer
  timeout with a note in the object
- Cron delivery targets `gemini_voice:doc` and `gemini_voice:event-HHMM`
- Unattended: a broken token is reported as `retrying / auth_failed` in the gateway status, without a browser
  flow and without stopping the gateway
- Standard library only; unit tests without network, optional contract test against a Hermes checkout
