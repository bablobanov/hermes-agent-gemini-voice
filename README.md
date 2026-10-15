# Hermes Agent Gemini Voice

![Hermes Agent Gemini Voice: talk to your Hermes Agent from an Android phone with Gemini Live on Gemini 4 Argon](assets/hermes-agent-gemini-live-voice-android-argon.svg)

[简体中文](README.zh-Hans.md) · [Русский](README.ru.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

Talk to your Hermes Agent from the **Gemini app on your Android phone**: while walking, while driving, without
opening a chat. Say *"Create a document Hermes flights and put the text in it: compare the three cheapest
flights to Berlin on Friday"*. Hermes picks the document up, does the work with its usual tools and skills, and writes the
answer into the same document. Later you ask Gemini *"Read the document Hermes flights"* and listen to it.

No new app, no Gemini API key, no public endpoint: the channel is your own Google Calendar, Docs and Sheets.
Hermes reads them through the plugin's own Google OAuth token and the Gemini app reads them aloud.

Supports **Gemini 4 Argon** and works with earlier Gemini app versions. The plugin is not tied to a model: it
sees only the Google objects the app creates.

```text
Gemini app ──creates──► event / doc / sheet with "Hermes" in the title (your account)
                                  │ the plugin polls once a minute
                                  ▼
          gemini-voice ──task──► Hermes gateway (one chat, your usual tools and rules)
                                  │ final reply
                                  ▼
          the reply is written into the same object ──► "Read the document …" in Gemini
```

## What it does

- **Tasks in.** A new Google Calendar event, Doc or Sheet whose title contains a label word (default `Hermes`)
  becomes a message to Hermes. The plugin first writes `[Hermes] got it, working on it`, then replaces it with
  `[Hermes] reply:` and the answer
- **Digests out.** Any Hermes cron job can deliver into a new document (`gemini_voice:doc`) or into an event
  at a given time (`gemini_voice:event-0800`), so a morning briefing becomes *"What's on my calendar at 8?"*
- **One conversation.** All voice tasks go to one chat, like a private messenger chat. When Hermes needs a
  decision it asks in its reply, and you answer by dictating a new task
- **Only your own objects.** Invitations, documents shared with you and events other people put on your
  calendar are ignored, even with the label in the title (see [Safety](#safety-and-disclosure))

### How the Gemini app behaves (tested by the author, October 2026)

- One task = one new object, said in one sentence. Gemini does what sounds addressed to Gemini itself, so name
  the content explicitly:
  - document: say what goes into it, *"Create a document Hermes flights and put the text in it: compare the
    three cheapest flights to Berlin"*. Otherwise Gemini answers the request itself and the document gets only
    its title. Earlier Gemini app versions also took the shorter *"… with the text: …"*; on Gemini 4 Argon it
    gave only the title
  - event: put the task in the title as a name, not as a request, *"Create an event in Google Calendar today in
    an hour titled Hermes product of 17 and 23"*. Gemini does not carry a spoken description over into the
    event, and a request in the title (*"Hermes, multiply 17 by 23"*) it answers itself, leaving the title
    *"Hermes"*. Naming Google Calendar saves the question which calendar to use on phones that have their own
  - sheet: the same, title and rows named, *"Create a spreadsheet in Google Sheets titled Hermes shopping with
    the rows milk, bread, cheese"*. Hermes answers in the rows below
- Pick a label that speech recognition writes reliably. A short nickname can come out as other words (a Russian
  "Афоня" became "о фоне", "about the background"), and then the object is not a task; a full name worked
- Gemini cannot edit an existing document or add a row to an existing sheet, so the conversation continues
  with new objects
- Gemini does not announce that the reply is ready; you ask for it when you want it. For events the natural
  question works: *"What's on my calendar tomorrow at 10?"*. Asking by time is safer than by title: speech
  recognition may write the label in another alphabet (a Russian "Гермес" came out as "Hermes") and the title
  search is literal

### Tested live (Hermes 0.21.6, October 2026)

With the Gemini app on an Android phone.

Gemini 4 Argon (15 October 2026), dictated in Russian in the chat mode:

- *"Create a document Hermes capitals of Africa with the text: name three capitals of Africa"*: the document got
  only its title, *"Hermes - capital of Africa"*; the plugin took it and Hermes answered from the title (Africa is a
  continent with no single capital, for example Cairo, Nairobi, Addis Ababa)
- *"Create a document Hermes capitals of Africa and put the text in it: name three capitals of Africa"*: the
  document got the title and the text (speech recognition wrote "three" as "300"); the plugin took it and Hermes
  named three capitals
- *"Read the document Hermes - capital of Africa"* in Gemini Live: Gemini found both documents and read the
  answers aloud

Earlier Gemini app versions (14-15 October 2026):

- a document task: Hermes answered in the document, Gemini read the answer aloud
- an event task: Hermes answered in the event description
- a sheet task: Hermes answered in the rows below, Gemini read them
- a morning briefing delivered by cron into an 08:00 event
- a gateway restart leaves no stray document behind

Covered by the unit tests and the engine contract test, not yet live: objects from other accounts are ignored,
approvals, exactly once across a restart, a revoked token, a schedule set by voice.

## Requirements

- Hermes Agent **0.21.6 or newer** with the messaging gateway running (the plugin is a gateway platform).
  Tested live on 0.21.6; the engine contract test (61 checks) passes on 0.21.6 and 0.22.0. Hermes does not load
  platform plugins under `plugins.isolation: host`, so this plugin does not work in that mode
- The Google account used in the Gemini app on the phone
- Your own Google Cloud project for that account (free) with the **Google Calendar, Google Drive, Google Docs
  and Google Sheets APIs** enabled and an **OAuth client of type "Desktop app"**
- On the OAuth consent screen, press **Publish app** (status "In production"). In "Testing" status Google
  expires refresh tokens after 7 days and the channel silently stops working a week later. An unverified
  app is fine for your own account; you will see Google's "unverified app" notice once during consent

## Install

1. Install and enable the plugin (from the catalog, or clone this repository and copy
   `plugins/gemini-voice` into your Hermes plugins directory):

   ```sh
   hermes plugins install gemini-voice
   hermes plugins enable gemini-voice
   ```

2. Authorize the plugin's own Google access, once, in a terminal on the Hermes host. Have the OAuth
   client JSON at hand, or paste the client ID and secret when asked:

   ```sh
   hermes gemini-voice auth --client-secret-file <PATH_TO_CLIENT_JSON>
   ```

   The command prints a Google consent link. Open it in a browser signed in to the phone's Google account,
   allow all four permissions, then copy the address of the page that fails to load (`http://localhost:1/…`)
   and paste it back. The command stores `GEMINI_VOICE_CLIENT_ID`, `GEMINI_VOICE_CLIENT_SECRET` and
   `GEMINI_VOICE_REFRESH_TOKEN` in the Hermes `.env` and prints no secrets.

3. Check it and restart the gateway with your usual method:

   ```sh
   hermes gemini-voice check
   ```

   Expected: `TOKEN_OK scopes: calendar.events.owned drive.metadata.readonly documents spreadsheets` and
   `OK` for the Calendar, Drive, Docs and Sheets APIs.

4. Optional settings in `config.yaml` (defaults shown):

   ```yaml
   plugins:
     entries:
       gemini-voice:
         settings:
           labels: [Hermes]          # "Herm*" matches any word starting with "Herm"
           language: en              # en | ru, for the plugin's own status lines
           timezone: UTC             # IANA name, used for status times and event-HHMM delivery
           poll_seconds: 60
           max_age_hours: 24         # older objects are never tasks
           answer_timeout_minutes: 40
           max_tasks_per_day: 30
           sources: [calendar, docs, sheets]
   ```

   Voice tasks run with the platform's toolset, configured like any other platform
   (`platform_toolsets.gemini_voice`, default `hermes-gemini_voice`). Give it the same tools as your main
   chat if you want the same abilities by voice.

   If you turned on the runtime footer (`display.runtime_footer`, off by default), turn it off for this
   platform. Otherwise the footer line (`model · 4%`) lands in every reply and Gemini reads it aloud:

   ```sh
   hermes config set display.platforms.gemini_voice.runtime_footer.enabled false
   ```

## Using it

| Say to Gemini | What happens |
|---|---|
| "Create a document Hermes landlord and put the text in it: draft a reply to the landlord about the heating" | Hermes drafts it into the document |
| "Create an event in Google Calendar tomorrow at 9 titled Hermes documents for the passport office" | the answer lands in the event description |
| "Create a spreadsheet in Google Sheets titled Hermes expenses with the rows fuel 3000, parking 400" | Hermes handles it and answers in the next rows |
| "Read the document Hermes landlord" / "What's on my calendar tomorrow at 9?" | Gemini reads the reply |
| "Create a document Hermes landlord reply and put the text in it: yes, send it" | answers a question Hermes asked in its previous reply |

Deliver a cron job's output by voice-friendly target:

```sh
hermes cron create "30 7 * * *" "Morning briefing: mail, calendar, tasks" \
  --name "Briefing" --deliver gemini_voice:event-0800 --failure-deliver local
```

`gemini_voice:doc` creates a new document titled `[Hermes digest] <first line of the output>`; its text starts
with the same line. `gemini_voice:event-HHMM` creates (or updates, on the same day) a 15-minute event at that
local time. A bare `gemini_voice` goes to the platform's home channel, which exists only when you name one:
`GEMINI_VOICE_HOME_CHANNEL` (`doc` or `event-HHMM`; any other value falls back to `doc` with a warning) or a
`home_channel` under `platforms.gemini_voice` in `config.yaml`. Without one a bare `gemini_voice` is not delivered,
and Hermes's own notices to home channels never reach your Google account. With one, they land there too: the
"back online" message after a planned restart and storage warnings become a new document or create or update
an event, depending on the target; the shutdown notice is always refused. Output with attachments is refused, never
half-delivered. The
voice chat itself is not a delivery target. When you ask by voice for something later or on a schedule
(*"every day at 8 send me …"*), Hermes is told to create the cron job with one of these two targets.

## Safety and disclosure

- **Network:** only Google APIs (Calendar, Drive, Docs, Sheets) and `oauth2.googleapis.com`, once per poll and
  per task. No telemetry, no other hosts
- **Credentials:** the plugin uses only its own OAuth client and refresh token from the three
  `GEMINI_VOICE_*` variables. It never reads another tool's token files. Access tokens stay in memory; the
  only write of credentials is `hermes gemini-voice auth`, run by you, through Hermes's own `.env` writer
- **Google scopes:** `calendar.events.owned`, `drive.metadata.readonly`, `documents`, `spreadsheets`. No Gmail,
  no full Drive access. The plugin reads file metadata to find new objects, reads the text of labelled
  objects, and writes only into task objects and the objects its cron delivery creates
- **Who can give tasks:** whoever can create objects in this Google account, which normally means whoever
  has your phone with the Gemini app. Only objects the account owner created count: an event must be a
  one-off event the owner created and organizes with no other guests, not edited later by anyone; a Doc or
  Sheet must be owned by you, not shared, and last modified by you. Ownership is checked again right before
  a task starts, on the same version of an event or Doc that the task is read from and the acknowledgement is
  written onto (a change in between starts the check again). The platform is registered as `trusted_inbound` for that reason: events come from the
  Google account you authorized, not from other people
- **Calendar edit rights:** Google does not say who edited an event. A change made within two minutes of an
  event's creation is taken as the Gemini app finishing it; after that, the event's title, time and description
  must stay as the plugin first saw them (other fields such as colour may change), or the task is not run and
  the event gets a note saying so. Someone you gave "make changes to events" on your primary calendar could
  still change a brand-new event within its first two minutes: do not give that right to others, or leave
  `calendar` out of `sources`
- **Your text is never deleted.** The plugin replaces only the exact block it wrote itself, where it wrote it
  (in a Doc at the recorded position, in an event only as the very end of the description, in a Sheet in the
  recorded row), never by searching for `[Hermes]`; if anything changed there, the reply is appended instead.
  In Sheets the reply rows take the place of the plugin's acknowledgement in one atomic update, never written
  over existing rows, and the acknowledgement cell is emptied only through a server-side exact match: if you
  edited it, your edit stays, right below the reply
- **Same tools and rules as your other chats.** The plugin adds no approvals of its own and bypasses none.
  A command that needs a person's approval cannot be approved by voice: Hermes waits `approvals.timeout`
  (300 seconds by default) and then refuses it; repeat such a request in your messaging app. `clarify` is
  blocked on this platform because nobody can answer in the middle of a turn
- **Engine internals:** besides the documented plugin API, the plugin imports
  `gateway.session_context.get_session_env` (the `pre_tool_call` hook does not say which platform a tool call
  runs on, and `clarify` is blocked only here) and reads a few underscore attributes of its adapter base class,
  the gateway runner and gateway events: whether the voice chat's turn is still running, whether the gateway
  is resuming turns after a restart, the streamed final text, the status writer. `hermes plugins validate` on
  0.22.0 lists the import as a catalog rule 9 warning. The engine contract test covers each of them: run it
  against a new Hermes release before upgrading (see [Tests](#tests))
- **Background work:** a polling task inside the gateway process while the platform is connected
- **Nothing arrives later on its own.** The voice chat takes only the final reply of a task. Background
  processes do not wake it, and messages sent to it from other chats, attachments (files, images, audio) and
  approval prompts are refused, never reported as delivered. Results for later go through cron delivery to a
  new document or event (see [Using it](#using-it))
- **At most once:** every object is recorded before it is handed over and is never handed over again. Tasks
  run one at a time: the next starts only when the gateway has finished the previous turn. After a restart
  new tasks wait until the gateway has resumed the interrupted turn; Hermes reports that task in its object
  and asks whether to continue (answer with a new task). A reply without its anchor goes only to that
  interrupted task, never into another object. A final reply is saved in the plugin's journal the moment it
  arrives, so a restart before it reaches Google does not lose it; replies the gateway re-sends after a crash
  never choose an object (one the plugin already holds is acknowledged, any other is refused). If no reply
  comes within `answer_timeout_minutes`, the object gets a note that the task did not finish
- **Digests are never tasks:** documents created by cron delivery carry `[Hermes digest]` in the title and on
  the first line, and events carry a private marker, so an output is never read back as your instruction,
  even if its title contains the label
- **Unattended:** a revoked or expired token never opens a browser flow. The platform reports
  `retrying / auth_failed` in the gateway status (`gateway_state.json`, `hermes gateway status`), logs one
  line, re-checks every 15 minutes, and the gateway keeps running

## Status and troubleshooting

```sh
hermes gemini-voice status   # platform status, last successful poll, task counts (no titles or text)
hermes gemini-voice check    # token, scopes and the four APIs
```

| Status code | Meaning and fix |
|---|---|
| `auth_failed` | token revoked, expired or missing a scope: run `hermes gemini-voice auth`, restart the gateway |
| `google_unreachable` | network or Google outage: polling backs off up to 15 minutes; if only one API is down, the others keep working. A task Google does not answer for is retried with growing pauses for about 45 minutes, then closed with a note, written as soon as Google answers again |
| `api_disabled` | enable the four APIs in the OAuth client's Cloud project |

Limits: one Google account per Hermes profile, one poller per account; one task at a time; recurring events
are never tasks; no attachments, text only; a sheet is read up to row 500 and column Z (the task always says
so) and the reply goes into column A of the first sheet, at most 300 rows; event descriptions are
capped at 7,000 characters of reply; tasks older than `max_age_hours` when their turn comes are skipped with a
note; at most 30 tasks a day by default.

## Tests

```sh
python -B -m unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -v
```

The unit tests run without network and without Hermes, against an in-memory Google stand-in. The optional
engine contract test loads the plugin through a real Hermes checkout and skips without one (a skip is not a
pass):

```sh
GEMINI_VOICE_TEST_ENGINE_ROOT=<HERMES_CHECKOUT> GEMINI_VOICE_TEST_ENGINE_PYTHON=<HERMES_PYTHON> \
  python -B -m unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -p test_engine_contract.py -v
```

On an install managed by Hermes's environment manager (0.21.6 and newer), the engine's dependencies are visible
only through its launcher: leave the Python unset and run the tests through it.

```sh
GEMINI_VOICE_TEST_ENGINE_ROOT=<HERMES_CHECKOUT> \
  <HERMES_CHECKOUT>/.hermes/bin/hermes --run-module unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -v
```

## Project

- `plugins/gemini-voice/`: the plugin (standard library only, no Python dependencies) and its tests
- [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)

MIT © 2026 Ilya Balobanov. Independent integration; not an official Google or Nous Research product.
Android, Gemini and Google are trademarks of Google LLC.

The Android robot is reproduced or modified from work created and shared by Google and used according to terms described in the Creative Commons 3.0 Attribution License.
