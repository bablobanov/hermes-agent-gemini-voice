# Changelog

## 0.1.0

- First release: the `gemini_voice` gateway platform. New Google Calendar events, Docs and Sheets with a label
  in the title, created by the account owner, become tasks for Hermes; the final reply replaces the
  plugin's acknowledgement in the same object for the Gemini app to read aloud
- The plugin's own OAuth token (`calendar.events.owned`, `drive.metadata.readonly`, `documents`,
  `spreadsheets`), refreshed in memory only; `hermes gemini-voice auth | check | status`
- Owner-only boundary: invitations, shared files, events added or edited by other people, recurring events and
  the plugin's own outputs are never tasks; ownership is re-checked right before a task starts
- One shared chat, one task at a time (the next starts when the gateway finished the previous turn, and not
  while it resumes turns interrupted by a restart); replies are routed by their anchor, and a reply without one
  goes only to the task interrupted by the restart; durable journal with at-most-once dispatch across restarts;
  answer timeout with a note in the object; tasks that waited longer than `max_age_hours` are skipped with a note
- The voice chat takes only final replies: messages from other chats, attachments, approval prompts and replies
  with no task to go to are refused (final, never retried), never reported as delivered
- Writes replace only the exact block the plugin recorded, at the place it recorded (Doc position, end of the
  event description, Sheet row), and otherwise append; Sheets rows are inserted in one atomic update, never
  overwritten; replies are written as plain text and as plain-string cell values
- Cron delivery targets `gemini_voice:doc` (the title and the first line carry a `[Hermes digest]` signature)
  and `gemini_voice:event-HHMM`; the platform hint tells Hermes to use them for anything scheduled by voice
- Unattended: a broken token is reported as `retrying / auth_failed` in the gateway status, without a browser
  flow and without stopping the gateway; one refused or unreachable API does not stop the others or the replies
- Standard library only; unit tests without network, optional contract test against a Hermes checkout
