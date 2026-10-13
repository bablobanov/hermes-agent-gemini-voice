import unittest
from datetime import datetime, timedelta, timezone

from _load import load
from fake_google import FakeGoogle

api = load("google_api")
sources = load("sources")
textutil = load("textutil")

SINCE = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
NEW = "2026-10-13T17:00:37.984Z"
OLD = "2026-10-12T09:00:00Z"
PATTERNS = textutil.compile_labels(["Афон*", "Hermes"])
ACK = textutil.block("got it")


def make_client(fake):
    return api.GoogleClient(api.TokenSource("a", "b", "c", transport=fake), transport=fake)


def event(**overrides):
    base = {"id": "e1", "summary": "Афоня тест календарь", "created": NEW, "updated": NEW, "status": "confirmed",
            "eventType": "default", "creator": {"self": True}, "organizer": {"self": True}}
    base.update(overrides)
    return base


def drive_file(**overrides):
    base = {"id": "f1", "name": "Афоня тест журавль", "mimeType": sources.DOC_MIME, "createdTime": NEW,
            "ownedByMe": True, "shared": False, "lastModifyingUser": {"me": True}}
    base.update(overrides)
    return base


def reason(verdict):
    return "" if verdict.accepted else verdict.reason


class OwnerBoundaryTests(unittest.TestCase):
    def test_owner_event_is_accepted(self):
        verdict = sources.check_event(event(), PATTERNS, SINCE)
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.obj.key, "cal.e1")

    def test_foreign_or_unusual_events_are_rejected_even_with_label(self):
        cases = {
            "not_organizer": event(organizer={"email": "x@example.com"}),  # invitation from another account
            "not_creator": event(creator={"email": "x@example.com"}),  # someone wrote on the owner's calendar
            "has_attendees": event(attendees=[{"self": True}, {"email": "x@example.com"}]),
            "event_type": event(eventType="fromGmail"),
            "cancelled": event(status="cancelled"),
            "recurring": event(recurrence=["RRULE:FREQ=DAILY"]),
            "edited_after_creation": event(updated="2026-10-13T17:30:00Z"),
            "too_old": event(created=OLD, updated=OLD),
            "no_label": event(summary="Встреча с бухгалтером"),
            "plugin_output": event(extendedProperties={"private": {"gemini_voice": "0800_20261014"}}),
        }
        for expected, item in cases.items():
            self.assertEqual(reason(sources.check_event(item, PATTERNS, SINCE)), expected)
        self.assertEqual(reason(sources.check_event(event(recurringEventId="series1"), PATTERNS, SINCE)), "recurring")

    def test_owner_only_attendee_and_quick_edit_are_fine(self):
        self.assertTrue(sources.check_event(event(attendees=[{"self": True}]), PATTERNS, SINCE).accepted)
        self.assertTrue(sources.check_event(event(updated="2026-10-13T17:05:00Z"), PATTERNS, SINCE).accepted)

    def test_owner_file_is_accepted(self):
        verdict = sources.check_file(drive_file(), PATTERNS, SINCE, [])
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.obj.key, "doc.f1")
        self.assertEqual(sources.check_file(drive_file(mimeType=sources.SHEET_MIME), PATTERNS, SINCE, []).obj.key,
                         "sheet.f1")

    def test_foreign_files_are_rejected_even_with_label(self):
        cases = {
            "not_owned": drive_file(ownedByMe=False),  # shared with the owner by someone else
            "shared": drive_file(shared=True),  # e.g. ownership transferred: the previous owner still edits
            "modified_by_other": drive_file(lastModifyingUser={"me": False}),
            "mime": drive_file(mimeType="application/pdf"),
            "trashed": drive_file(trashed=True),
            "too_old": drive_file(createdTime=OLD),
            "no_label": drive_file(name="Квартальный отчёт"),
            "plugin_output": drive_file(id="out1"),
        }
        for expected, item in cases.items():
            self.assertEqual(reason(sources.check_file(item, PATTERNS, SINCE, ["out1"])), expected)

    def test_missing_ownership_fields_fail_closed(self):
        item = drive_file()
        del item["ownedByMe"], item["shared"]
        self.assertFalse(sources.check_file(item, PATTERNS, SINCE, []).accepted)
        bare = event()
        del bare["creator"]
        self.assertFalse(sources.check_event(bare, PATTERNS, SINCE).accepted)

    def test_calendar_listing_is_not_expanded_and_reports_truncation(self):
        fake = FakeGoogle()
        fake.add_event("Афоня", created=NEW, recurrence=["RRULE:FREQ=DAILY"])
        verdicts, complete = sources.Calendar(make_client(fake)).candidates(PATTERNS, SINCE, SINCE)
        listing = [c for c in fake.calls if c[0] == "GET" and c[1] == sources.CAL_EVENTS][-1]
        self.assertEqual(listing[2]["singleEvents"], "false")
        self.assertTrue(complete)
        self.assertEqual([v.reason for v in verdicts], ["recurring"])

    def test_paged_reports_an_incomplete_listing(self):
        class Endless:
            def request(self, method, url, params=None, **_):
                return api.Response(200, {"items": [{"id": "x"}], "nextPageToken": "more"})
        items, complete = sources._paged(Endless(), "u", {}, "items")
        self.assertFalse(complete)
        self.assertEqual(len(items), sources.MAX_PAGES)

    def test_drive_recheck(self):
        fake = FakeGoogle()
        doc = fake.add_doc("Афоня", "x", created=NEW)
        drive = sources.Drive(make_client(fake))
        self.assertEqual(drive.recheck(doc), "")
        fake.files[doc]["shared"] = True
        self.assertEqual(drive.recheck(doc), "shared")


class CalendarWriteTests(unittest.TestCase):
    def test_reply_replaces_exactly_our_acknowledgement(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня тест", created=NEW, description="Купить молоко")
        cal = sources.Calendar(make_client(fake))
        _, locator = cal.write(event_id, ACK, None)
        self.assertEqual(fake.events[event_id]["description"], "Купить молоко\n\n[Hermes] got it")
        _, locator = cal.write(event_id, textutil.block("reply:", "Молоко куплено"), locator)
        self.assertEqual(fake.events[event_id]["description"], "Купить молоко\n\n[Hermes] reply:\nМолоко куплено")
        patch = [c for c in fake.calls if c[0] == "PATCH"][-1]
        self.assertEqual(patch[2]["sendUpdates"], "none")

    def test_owner_text_with_a_marker_is_never_removed(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня", created=NEW, description="старый ответ:\n[Hermes] reply: копия")
        cal = sources.Calendar(make_client(fake))
        _, locator = cal.write(event_id, ACK, None)
        cal.write(event_id, textutil.block("reply:", "новое"), locator)
        self.assertTrue(fake.events[event_id]["description"].startswith("старый ответ:\n[Hermes] reply: копия\n\n"))

    def test_text_added_after_our_block_is_kept(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня", created=NEW, description="задача")
        cal = sources.Calendar(make_client(fake))
        _, locator = cal.write(event_id, ACK, None)
        fake.events[event_id]["description"] += "\nдописал владелец"
        cal.write(event_id, textutil.block("reply:", "ответ"), locator)
        description = fake.events[event_id]["description"]
        self.assertIn("[Hermes] got it\nдописал владелец", description)
        self.assertTrue(description.endswith("[Hermes] reply:\nответ"))

    def test_concurrent_edit_is_retried(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня", created=NEW)
        cal = sources.Calendar(make_client(fake))
        original_get = cal._get
        calls = [0]

        def stale_once(eid):
            body = dict(original_get(eid))
            calls[0] += 1
            if calls[0] == 1:
                body["etag"] = '"stale"'
            return body
        cal._get = stale_once
        self.assertEqual(cal.write(event_id, ACK, None)[0], 200)

    def test_html_description(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня", created=NEW, description="Список<br><b>дел</b>")
        cal = sources.Calendar(make_client(fake))
        _, locator = cal.write(event_id, ACK, None)
        cal.write(event_id, textutil.block("reply:", "A & B"), locator)
        self.assertEqual(fake.events[event_id]["description"],
                         "Список<br><b>дел</b><br><br>\n[Hermes] reply:<br>\nA &amp; B")
        self.assertEqual(sources.html_to_text("a<br>b &amp; <b>c</b>"), "a\nb & c")

    def test_read_excludes_only_our_block(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня найди рецепт", created=NEW, description="борщ")
        cal = sources.Calendar(make_client(fake))
        _, locator = cal.write(event_id, ACK, None)
        self.assertEqual(cal.read(event_id, locator), "Афоня найди рецепт\nWhen: 2026-10-14T10:00:00+05:00\nборщ")


class DocsTests(unittest.TestCase):
    def test_ack_then_reply_replaces_exactly_the_ack(self):
        fake = FakeGoogle()
        doc_id = fake.add_doc("Афоня тест журавль", "Список дел\nКупить молоко\nПозвонить Сергею\n\n", created=NEW)
        docs = sources.Docs(make_client(fake))
        _, locator = docs.write(doc_id, ACK, None)
        self.assertTrue(fake.doc_text(doc_id).endswith("\n\n[Hermes] got it\n"))
        self.assertEqual(docs.read(doc_id, "Афоня тест журавль", locator),
                         "Афоня тест журавль\nСписок дел\nКупить молоко\nПозвонить Сергею")
        docs.write(doc_id, textutil.block("reply:", "Сергею позвонить в 18:00"), locator)
        self.assertEqual(fake.doc_text(doc_id), "Список дел\nКупить молоко\nПозвонить Сергею\n\n"
                                                "[Hermes] reply:\nСергею позвонить в 18:00\n")

    def test_owner_text_is_never_deleted(self):
        fake = FakeGoogle()
        owner = "заметки\n[Hermes] старый ответ, вставленный вручную\nещё строка\n"
        doc_id = fake.add_doc("Афоня", owner, created=NEW)
        docs = sources.Docs(make_client(fake))
        _, locator = docs.write(doc_id, ACK, None)
        fake.docs[doc_id]["text"] = fake.doc_text(doc_id) + "дописано после квитанции\n"
        docs.write(doc_id, textutil.block("reply:", "ответ"), locator)
        text = fake.doc_text(doc_id)
        self.assertTrue(text.startswith(owner))
        self.assertIn("[Hermes] reply:\nответ\nдописано после квитанции\n", text)
        self.assertNotIn("got it", text)

    def test_missing_ack_means_append(self):
        fake = FakeGoogle()
        doc_id = fake.add_doc("Афоня", "текст", created=NEW)
        docs = sources.Docs(make_client(fake))
        docs.write(doc_id, textutil.block("reply:", "ответ"), {"text": "[Hermes] не тот текст"})
        self.assertEqual(fake.doc_text(doc_id), "текст\n[Hermes] reply:\nответ\n")

    def test_empty_document_and_revision_guard(self):
        fake = FakeGoogle()
        doc_id = fake.add_doc("Hermes", "", created=NEW)
        sources.Docs(make_client(fake)).write(doc_id, ACK, None)
        self.assertEqual(fake.doc_text(doc_id), "[Hermes] got it\n")
        batch = [c for c in fake.calls if c[1].endswith(":batchUpdate")][-1]
        self.assertEqual(batch[3]["writeControl"], {"requiredRevisionId": "r1"})

    def test_output_document_carries_the_signature(self):
        fake = FakeGoogle()
        docs = sources.Docs(make_client(fake))
        doc_id = docs.create_blank("Радар Gemini 20.10")
        self.assertEqual(docs.fill_output(doc_id, "Радар Gemini 20.10", "T1: нет\nT2: нет"), 200)
        self.assertEqual(fake.docs[doc_id]["title"], "Радар Gemini 20.10")
        self.assertEqual(fake.doc_text(doc_id), "[Hermes digest] Радар Gemini 20.10\nT1: нет\nT2: нет\n")
        self.assertTrue(textutil.is_output(docs.read(doc_id, "Радар Gemini 20.10").split("\n", 1)[1]))


class SheetsTests(unittest.TestCase):
    def test_reply_rows_are_inserted_under_the_ack_as_raw_strings(self):
        fake = FakeGoogle()
        sheet_id = fake.add_sheet("Афоня тест цапля", [["молоко", "хлеб", "сыр"]], created=NEW)
        sheets = sources.Sheets(make_client(fake))
        _, locator = sheets.write(sheet_id, ACK, None)
        self.assertEqual(locator, {"row": 2, "text": ACK})
        fake.sheets[sheet_id].append(["владелец дописал строку"])
        sheets.write(sheet_id, textutil.block("reply:", '=IMPORTXML("http://evil")\nвторая строка'), locator)
        self.assertEqual(fake.sheets[sheet_id], [
            ["молоко", "хлеб", "сыр"], ["[Hermes] reply:"], ['=IMPORTXML("http://evil")'], ["вторая строка"],
            ["владелец дописал строку"]])
        self.assertTrue(all(w["valueInputOption"] == "RAW" for w in fake.sheet_writes))
        self.assertEqual(sheets.read(sheet_id, "Афоня тест цапля"), "Афоня тест цапля\nмолоко, хлеб, сыр\n"
                         "[Hermes] reply:\n=IMPORTXML(\"http://evil\")\nвторая строка\nвладелец дописал строку")

    def test_large_sheet_is_appended_not_overwritten(self):
        fake = FakeGoogle()
        rows = [[f"r{i}"] for i in range(1, 702)]
        sheet_id = fake.add_sheet("Афоня", rows, created=NEW)
        sources.Sheets(make_client(fake)).write(sheet_id, ACK, None)
        self.assertEqual(fake.sheets[sheet_id][:701], rows)
        self.assertEqual(fake.sheets[sheet_id][701], [ACK])
        self.assertEqual(fake.sheet_writes[-1]["insertDataOption"], "INSERT_ROWS")

    def test_read_skips_our_ack_row(self):
        fake = FakeGoogle()
        sheet_id = fake.add_sheet("Афоня", [["a", "b"]], created=NEW)
        sheets = sources.Sheets(make_client(fake))
        _, locator = sheets.write(sheet_id, ACK, None)
        self.assertEqual(sheets.read(sheet_id, "Афоня", locator), "Афоня\na, b")

    def test_long_lines_are_wrapped_and_capped(self):
        lines = sources.sheet_lines(("слово " * 400).strip() + "\n" + "\n".join(["x"] * 500))
        self.assertTrue(all(len(line) <= sources.SHEET_CELL_LIMIT for line in lines))
        self.assertEqual(len(lines), sources.SHEET_MAX_ROWS)
        self.assertEqual(sources.quote_sheet("Bob's list"), "'Bob''s list'")
        self.assertEqual(sources.first_row("'Sheet 1'!A12:A14"), 12)


class OutputEventTests(unittest.TestCase):
    def test_slot_today_or_tomorrow(self):
        zone = timezone(timedelta(hours=5))
        morning = datetime(2026, 10, 14, 2, 30, tzinfo=timezone.utc)  # 07:30 local
        self.assertEqual(sources.slot_start("0800", zone, morning).isoformat(), "2026-10-14T08:00:00+05:00")
        evening = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # 20:00 local, 12 h past the slot
        self.assertEqual(sources.slot_start("0800", zone, evening).isoformat(), "2026-10-15T08:00:00+05:00")

    def test_same_slot_and_day_updates_instead_of_duplicating(self):
        fake = FakeGoogle()
        out = sources.CalendarOutput(make_client(fake))
        zone = timezone(timedelta(hours=5))
        now = datetime(2026, 10, 14, 2, 30, tzinfo=timezone.utc)
        first, _ = out.upsert("0800", "Брифинг", "почта: 3 письма", zone, "Asia/Yekaterinburg", now)
        second, _ = out.upsert("0800", "Брифинг", "почта: 4 письма", zone, "Asia/Yekaterinburg", now)
        self.assertEqual(first, second)
        self.assertEqual(len(fake.events), 1)
        created = fake.events[first]
        self.assertEqual(created["description"], "почта: 4 письма")
        self.assertEqual(created["start"]["timeZone"], "Asia/Yekaterinburg")
        self.assertEqual(reason(sources.check_event(created, textutil.compile_labels(["Брифинг"]), SINCE)),
                         "plugin_output")


if __name__ == "__main__":
    unittest.main()
