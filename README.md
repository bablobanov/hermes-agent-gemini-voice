# Hermes Agent Gemini Voice

![Hermes Agent Gemini Voice](assets/hero.png)

[简体中文](README.zh-Hans.md) · [Русский](README.ru.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

Talk to your Hermes Agent from the **Gemini app on your Android phone**: while walking, while driving, without
opening a chat. Say *"Create a document: Hermes, compare the three cheapest flights to Berlin on Friday"*.
Hermes picks the document up, does the work with its usual tools and skills, and writes the answer into the
same document. Later you ask Gemini *"Read the document Hermes flights"* and listen to it.

No new app, no Gemini API key, no public endpoint: the channel is your own Google Calendar, Docs and Sheets.
Hermes reads them through the plugin's own Google OAuth token and the Gemini app reads them aloud.

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

- One task = one new object, said in one sentence: *"Create a document Hermes … with the text …"*,
  *"Create an event tomorrow at 10: Hermes …"*, *"Create a sheet Hermes shopping: milk, bread, cheese"*
- Gemini cannot edit an existing document or add a row to an existing sheet, so the conversation continues
  with new objects
- Gemini does not announce that the reply is ready; you ask for it when you want it. For events the natural
  question works: *"What's on my calendar tomorrow at 10?"*

## Requirements

- Hermes Agent **0.21.6 or newer** with the messaging gateway running (the plugin is a gateway platform)
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

## Using it

| Say to Gemini | What happens |
|---|---|
| "Create a document: Hermes, draft a reply to the landlord about the heating" | Hermes drafts it into the document |
| "Create an event tomorrow at 9: Hermes, what do I need for the passport office" | the answer lands in the event description |
| "Create a sheet Hermes expenses: fuel 3000" | Hermes handles it and answers in the next rows |
| "Read the document Hermes landlord" / "What's on my calendar tomorrow at 9?" | Gemini reads the reply |
| "Create a document: Hermes, yes, send it" | answers a question Hermes asked in its previous reply |

Deliver a cron job's output by voice-friendly target:

```sh
hermes cron create "30 7 * * *" "Morning briefing: mail, calendar, tasks" \
  --name "Briefing" --deliver gemini_voice:event-0800 --failure-deliver local
```

`gemini_voice:doc` creates a new document titled with the first line of the output.
`gemini_voice:event-HHMM` creates (or updates, on the same day) a 15-minute event at that local time.

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
  has your phone with the Gemini app. Only objects the account owner created count: for events the owner
  must be the creator and the organizer with no other guests; for Docs and Sheets the file must be owned by
  you, not shared, and last modified by you. The platform is registered as `trusted_inbound` for that reason:
  events come from the Google account you authorized, not from other people
- **Same tools and rules as your other chats.** The plugin adds no approvals of its own and bypasses none.
  Commands that need a person's approval cannot be approved by voice, so Hermes refuses them on this channel;
  repeat such a request in your messaging app. `clarify` is blocked on this platform because nobody can answer
  in the middle of a turn
- **Background work:** a polling task inside the gateway process while the platform is connected
- **At most once:** every object is recorded before it is handed over; after a restart a task is never
  started again. Hermes finishes an interrupted turn itself, or after `answer_timeout_minutes` the object gets
  a note that the task did not finish
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
| `google_unreachable` | network or Google outage: retried with backoff up to 15 minutes |
| `api_disabled` | enable the four APIs in the OAuth client's Cloud project |

Limits: one Google account per Hermes profile, one poller per account; the reply goes into column A of the
first sheet; event descriptions are capped at 7,000 characters of reply; at most 30 tasks a day by default.

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

## Project

- `plugins/gemini-voice/`: the plugin (standard library only, no Python dependencies) and its tests
- [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)

MIT © 2026 Ilya Balobanov. Independent integration; not an official Google or Nous Research product.
Gemini and Google are trademarks of Google LLC.
