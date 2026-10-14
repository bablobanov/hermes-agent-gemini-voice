# Changelog

## 0.1.0

- First release: the `gemini_voice` gateway platform. New Google Calendar events, Docs and Sheets with a label
  in the title, created by the account owner, become tasks for Hermes; the final reply replaces the
  plugin's acknowledgement in the same object for the Gemini app to read aloud
- The plugin's own OAuth token (`calendar.events.owned`, `drive.metadata.readonly`, `documents`,
  `spreadsheets`), refreshed in memory only; `hermes gemini-voice auth | check | status`
- Owner-only boundary: invitations, shared files, events added or edited by other people, recurring events and
  the plugin's own outputs are never tasks; ownership is re-checked right before a task starts, on the same
  version of an event or Doc that the task is read from and the acknowledgement is written onto (a change in
  between starts the check again; one that keeps changing is retried later); an event's title, time and
  description must still be what the plugin first saw, what its own writes made them, or a change made within
  two minutes of creation, else the task is not run and the event gets a note; the event acknowledgement is
  recorded before it is written, so a crash right after it never makes it look foreign
- One shared chat, one task at a time (the next starts when the gateway finished the previous turn, and not
  while it resumes turns interrupted by a restart); replies are routed by their anchor, and a reply without one
  goes only to the task interrupted by the restart; a final reply is journaled the moment it arrives; durable
  journal with at-most-once dispatch across restarts;
  answer timeout with a note in the object; tasks that waited longer than `max_age_hours` are skipped with a note
- The voice chat takes only final replies: messages from other chats, attachments, approval prompts and replies
  with no task to go to are refused (final, never retried), never reported as delivered; replies the gateway
  re-sends after a crash never choose an object (one the plugin already holds is acknowledged)
- Writes replace only the exact block the plugin recorded, at the place it recorded (Doc position, end of the
  event description, Sheet row), and otherwise append; Sheets reply rows take the acknowledgement's place in one
  atomic update, never overwriting a row, and the acknowledgement cell is emptied only on a server-side exact
  match; replies are written as plain text and as plain-string cell values
- Cron delivery targets `gemini_voice:doc` (the title and the first line carry a `[Hermes digest]` signature)
  and `gemini_voice:event-HHMM`; the platform hint tells Hermes to use them for anything scheduled by voice;
  output with attachments is refused inside and outside the gateway; the platform has a home channel only when
  one is named (`GEMINI_VOICE_HOME_CHANNEL`, one of the two targets, else `doc`, or `home_channel` in
  `config.yaml`), so by default Hermes's own notices to home channels (shutting down, back online, storage
  warnings) never become Google objects; a shutdown notice to a named home channel is refused
- Unattended: a broken token is reported as `retrying / auth_failed` in the gateway status, without a browser
  flow and without stopping the gateway; one refused or unreachable API does not stop the others or the replies;
  Drive files created in the last 15 minutes are read on every poll and the rest of the window is walked over
  the next polls, and a calendar pass longer than one poll goes on from the last change read (by time, never by
  page token); a second holding more changes than a poll reads is read deeper in the same poll, so a burst of
  changes hides nothing; a task Google does not answer for is retried with growing pauses for about 45
  minutes, then closed with a note that is written as soon as Google answers again
- Standard library only; unit tests without network, optional contract test against a Hermes checkout (passes
  on 0.21.6 and 0.22.0; on an install managed by Hermes's environment manager it runs through the engine's
  launcher, `hermes --run-module unittest`)
