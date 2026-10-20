# Changelog

## 0.2.0

- Hermes signs in to Google for the plugin. The plugin declares its Google client under `oauth:` in
  `plugin.yaml`; the owner signs in once with `hermes auth add gemini-voice:google`, and Hermes keeps the grant in
  the profile's `auth.json`, refreshes it and attaches the token only to requests for `www.googleapis.com`,
  `docs.googleapis.com` and `sheets.googleapis.com` (`credentialed_provider_request`, NousResearch/hermes-agent#141510).
  The plugin no longer holds, refreshes or writes a token: its own consent flow, token refresh and `.env` writes
  are gone, and `GEMINI_VOICE_REFRESH_TOKEN` is no longer read
- Requires Hermes 0.23.0 or newer, the first release with the plugin OAuth route; on an older Hermes use 0.1.3
- `hermes gemini-voice auth` only points to `hermes auth add gemini-voice:google`; `hermes gemini-voice check`
  sends one request to each of the four APIs through the sign-in Hermes holds
- No sign-in, a revoked one, or a client Hermes cannot use is `auth_failed` with the fix in the status; after a
  new sign-in polling resumes by itself within 15 minutes, without a restart. A token refresh that fails on the
  network or with a 5xx is retried like any Google outage. A 401 from Google is final: Hermes refreshes the token
  by its expiry, so the plugin no longer retries
- Upgrading from 0.1.x: sign in with `hermes auth add gemini-voice:google`, delete `GEMINI_VOICE_REFRESH_TOKEN`
  from the Hermes `.env`, restart the gateway
- The engine contract test drives Hermes's real plugin OAuth route with only the network faked (77 checks)
- README: the diagram says "one session per object" instead of "one chat"

## 0.1.3

- One object, one session: every voice task (an event, a document or a sheet) is a Hermes session of its own,
  with no history shared between tasks. Current rules, skills and memory apply to each task and context never
  piles up; before, all tasks shared one conversation that never ended, because Hermes no longer resets gateway
  conversations on a timer. The object's key is the thread of the owner's chat, so replies, their routing by
  object and cron delivery work as before. When Hermes asks a question, answer with a new complete task
- README, updated after the tag (plugin code unchanged): install from this repository,
  `hermes plugins install bablobanov/hermes-agent-gemini-voice/plugins/gemini-voice`; the catalog entry is
  submitted and waits for an OAuth route for plugins. The engine contract test counts 64 checks and passes on
  0.21.6, 0.22.0 and 0.22.2

## 0.1.2

- The descriptions lead with what the plugin is for: on Android you give Hermes a task without opening a
  messenger, by calling Gemini with its button (hold the power button, or the assistant gesture), even from the
  lock screen; one sentence by voice, the answer read aloud. README (en, ru, zh-Hans) and `plugin.yaml`

## 0.1.1

- The plugin description recommends the document form that works on Gemini 4 Argon, *"create a document
  Hermes ... and put the text in it: ..."*; the shorter *"... with the text: ..."* gives Argon only the title.
  Same as the README and the catalog entry
- README: the live probes of the plugin ran on 14-15 October 2026 (not 13-15: 13 October was a proof of
  concept without the plugin)

## 0.1.0

- First release: the `gemini_voice` gateway platform. New Google Calendar events, Docs and Sheets with a label
  in the title, created by the account owner, become tasks for Hermes; the final reply replaces the
  plugin's acknowledgement in the same object for the Gemini app to read aloud
- Supports Gemini 4 Argon and works with earlier Gemini app versions
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
  warnings) never become Google objects; a shutdown notice to a named home channel is refused; the cron
  scheduler's wrapper (`Cronjob Response: <name>`, the job id line and the "To stop or manage this job" line,
  `cron.wrap_response`) is removed when the text has exactly that shape with the same name at both ends and
  reads that way in only one place (read in one linear pass), so the
  object is titled and read as the job's own output (an empty output gets the default `Digest <time>` title);
  any other text is delivered as it is
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
