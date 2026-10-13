# Changelog

## 0.1.0

- First release: the `gemini_voice` gateway platform. New Google Calendar events, Docs and Sheets with a label
  in the title, created by the account owner, become tasks for Hermes; the final reply replaces the
  plugin's acknowledgement in the same object for the Gemini app to read aloud
- The plugin's own OAuth token (`calendar.events.owned`, `drive.metadata.readonly`, `documents`,
  `spreadsheets`), refreshed in memory only; `hermes gemini-voice auth | check | status`
- Owner-only boundary: invitations, shared files, events added or edited by other people, recurring events and
  the plugin's own outputs are never tasks; ownership is re-checked right before a task starts
- One shared chat, one task at a time (the next starts when the gateway finished the previous turn); replies
  are routed by their anchor; durable journal with at-most-once dispatch across restarts; answer timeout with a
  note in the object; tasks that waited longer than `max_age_hours` are skipped with a note
- Writes replace only the exact block the plugin recorded and otherwise append; Sheets rows are inserted,
  never overwritten; replies are written as plain text and as RAW cell values
- Cron delivery targets `gemini_voice:doc` (documents start with a `[Hermes digest]` signature line) and
  `gemini_voice:event-HHMM`
- Unattended: a broken token is reported as `retrying / auth_failed` in the gateway status, without a browser
  flow and without stopping the gateway; one refused API does not stop the others
- Standard library only; unit tests without network, optional contract test against a Hermes checkout
