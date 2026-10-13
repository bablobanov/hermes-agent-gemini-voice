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


def make_client(fake):
    return api.GoogleClient(api.TokenSource("a", "b", "c", transport=fake), transport=fake)


def event(**overrides):
    base = {"id": "e1", "summary": "Афоня тест календарь", "created": NEW, "status": "confirmed",
            "eventType": "default", "creator": {"self": True}, "organizer": {"self": True}}
    base.update(overrides)
    return base


def drive_file(**overrides):
    base = {"id": "f1", "name": "Афоня тест журавль", "mimeType": sources.DOC_MIME, "createdTime": NEW,
            "ownedByMe": True, "shared": False, "lastModifyingUser": {"me": True}}
    base.update(overrides)
    return base


class OwnerBoundaryTests(unittest.TestCase):
    def reason(self, verdict):
        return "" if verdict.accepted else verdict.reason

    def test_owner_event_is_accepted(self):
        verdict = sources.check_event(event(), PATTERNS, SINCE)
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.obj.key, "cal.e1")

    def test_foreign_events_are_rejected_even_with_label(self):
        cases = {
            "not_organizer": event(organizer={"email": "x@example.com"}),  # invitation from another account
            "not_creator": event(creator={"email": "x@example.com"}),  # someone wrote on the owner's calendar
            "has_attendees": event(attendees=[{"self": True}, {"email": "x@example.com"}]),
            "event_type": event(eventType="fromGmail"),
            "cancelled": event(status="cancelled"),
            "too_old": event(created=OLD),
            "no_label": event(summary="Встреча с бухгалтером"),
            "plugin_output": event(extendedProperties={"private": {"gemini_voice": "0800_20261014"}}),
            "already_handled": event(description="x\n" + textutil.block("Hermes: got it")),
        }
        for expected, item in cases.items():
            self.assertEqual(self.reason(sources.check_event(item, PATTERNS, SINCE)), expected)

    def test_owner_only_attendee_is_fine(self):
        self.assertTrue(sources.check_event(event(attendees=[{"self": True}]), PATTERNS, SINCE).accepted)

    def test_owner_file_is_accepted(self):
        verdict = sources.check_file(drive_file(), PATTERNS, SINCE, [])
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.obj.key, "doc.f1")
        sheet = sources.check_file(drive_file(mimeType=sources.SHEET_MIME), PATTERNS, SINCE, [])
        self.assertEqual(sheet.obj.key, "sheet.f1")

    def test_foreign_files_are_rejected_even_with_label(self):
        cases = {
            "not_owned": drive_file(ownedByMe=False),  # shared with the owner by someone else
            "shared": drive_file(shared=True),  # e.g. ownership transferred: the previous owner still edits
            "modified_by_other": drive_file(lastModifyingUser={"me": False}),
            "mime": drive_file(mimeType="application/pdf"),
            "too_old": drive_file(createdTime=OLD),
            "no_label": drive_file(name="Квартальный отчёт"),
            "plugin_output": drive_file(id="out1"),
        }
        for expected, item in cases.items():
            self.assertEqual(self.reason(sources.check_file(item, PATTERNS, SINCE, ["out1"])), expected)

    def test_missing_ownership_fields_fail_closed(self):
        item = drive_file()
        del item["ownedByMe"], item["shared"]
        self.assertFalse(sources.check_file(item, PATTERNS, SINCE, []).accepted)
        bare = event()
        del bare["creator"]
        self.assertFalse(sources.check_event(bare, PATTERNS, SINCE).accepted)


class CalendarWriteTests(unittest.TestCase):
    def test_reply_replaces_previous_block_and_sends_etag(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня тест", created=NEW, description="Купить молоко")
        cal = sources.Calendar(make_client(fake))
        cal.write(event_id, textutil.block("got it"))
        cal.write(event_id, textutil.block("reply:", "Молоко куплено"))
        description = fake.events[event_id]["description"]
        self.assertEqual(description, "Купить молоко\n\n[Hermes] reply:\nМолоко куплено")
        self.assertNotIn("got it", description)
        patch = [c for c in fake.calls if c[0] == "PATCH"][-1]
        self.assertEqual(patch[2]["sendUpdates"], "none")

    def test_concurrent_edit_is_retried(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня", created=NEW)
        cal = sources.Calendar(make_client(fake))
        original_get = cal._get

        def stale_once(eid, _calls=[0]):
            body = dict(original_get(eid))
            _calls[0] += 1
            if _calls[0] == 1:
                body["etag"] = '"stale"'
            return body
        cal._get = stale_once
        self.assertEqual(cal.write(event_id, textutil.block("ok")), 200)

    def test_html_description_keeps_html(self):
        merged = sources.merge_description("Список<br><b>дел</b>", textutil.block("reply:", "A & B"))
        self.assertEqual(merged, "Список<br><b>дел</b><br><br>\n[Hermes] reply:<br>\nA &amp; B")
        self.assertEqual(sources.html_to_text("a<br>b &amp; <b>c</b>"), "a\nb & c")

    def test_html_description_block_is_replaced_not_stacked(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня", created=NEW, description="Список<br><b>дел</b>")
        cal = sources.Calendar(make_client(fake))
        cal.write(event_id, textutil.block("got it"))
        cal.write(event_id, textutil.block("reply:", "готово"))
        description = fake.events[event_id]["description"]
        self.assertEqual(description, "Список<br><b>дел</b><br><br>\n[Hermes] reply:<br>\nготово")
        self.assertEqual(cal.read(event_id), "Афоня\nWhen: 2026-10-14T10:00:00+05:00\nСписок\nдел")

    def test_read_gives_title_time_and_owner_text_only(self):
        fake = FakeGoogle()
        event_id = fake.add_event("Афоня найди рецепт", created=NEW,
                                  description="борщ\n\n" + textutil.block("Hermes: got it"))
        text = sources.Calendar(make_client(fake)).read(event_id)
        self.assertEqual(text, "Афоня найди рецепт\nWhen: 2026-10-14T10:00:00+05:00\nборщ")


class DocsTests(unittest.TestCase):
    def test_ack_then_reply_replace_each_other(self):
        fake = FakeGoogle()
        doc_id = fake.add_doc("Афоня тест журавль", "Список дел\nКупить молоко\nПозвонить Сергею\n\n", created=NEW)
        docs = sources.Docs(make_client(fake))
        docs.write(doc_id, textutil.block("got it"))
        self.assertTrue(fake.doc_text(doc_id).endswith("\n\n[Hermes] got it\n"))
        docs.write(doc_id, textutil.block("reply:", "Сергею позвонить в 18:00"))
        text = fake.doc_text(doc_id)
        self.assertNotIn("got it", text)
        self.assertEqual(text, "Список дел\nКупить молоко\nПозвонить Сергею\n\n"
                               "[Hermes] reply:\nСергею позвонить в 18:00\n")
        self.assertEqual(docs.read(doc_id, "Афоня тест журавль"),
                         "Афоня тест журавль\nСписок дел\nКупить молоко\nПозвонить Сергею")

    def test_empty_document_gets_block_without_leading_blank_line(self):
        fake = FakeGoogle()
        doc_id = fake.add_doc("Hermes", "", created=NEW)
        sources.Docs(make_client(fake)).write(doc_id, textutil.block("got it"))
        self.assertEqual(fake.doc_text(doc_id), "[Hermes] got it\n")

    def test_write_uses_revision_guard(self):
        fake = FakeGoogle()
        doc_id = fake.add_doc("Hermes", "x", created=NEW)
        sources.Docs(make_client(fake)).write(doc_id, textutil.block("Hermes: got it"))
        batch = [c for c in fake.calls if c[1].endswith(":batchUpdate")][-1]
        self.assertEqual(batch[3]["writeControl"], {"requiredRevisionId": "r1"})

    def test_create_document(self):
        fake = FakeGoogle()
        doc_id, status = sources.Docs(make_client(fake)).create("Радар Gemini 20.10", "T1: нет\nT2: нет")
        self.assertEqual(status, 200)
        self.assertEqual(fake.docs[doc_id]["title"], "Радар Gemini 20.10")
        self.assertEqual(fake.doc_text(doc_id), "T1: нет\nT2: нет\n")


class SheetsTests(unittest.TestCase):
    def test_reply_goes_below_owner_rows_as_raw_strings(self):
        fake = FakeGoogle()
        sheet_id = fake.add_sheet("Афоня тест цапля", [["молоко", "хлеб", "сыр"]], created=NEW)
        sheets = sources.Sheets(make_client(fake))
        sheets.write(sheet_id, textutil.block("got it"))
        sheets.write(sheet_id, textutil.block("reply:", "=IMPORTXML(\"http://evil\")\nвторая строка"))
        rows = fake.sheets[sheet_id]
        self.assertEqual(rows[0], ["молоко", "хлеб", "сыр"])
        self.assertEqual(rows[1], ["[Hermes] reply:"])
        self.assertEqual(rows[2], ['=IMPORTXML("http://evil")'])
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(w["valueInputOption"] == "RAW" for w in fake.sheet_writes))
        self.assertEqual(sheets.read(sheet_id, "Афоня тест цапля"), "Афоня тест цапля\nмолоко, хлеб, сыр")

    def test_long_lines_are_wrapped_and_capped(self):
        lines = sources.sheet_lines(("слово " * 400).strip() + "\n" + "\n".join(["x"] * 500))
        self.assertTrue(all(len(line) <= sources.SHEET_CELL_LIMIT for line in lines))
        self.assertEqual(len(lines), sources.SHEET_MAX_ROWS)

    def test_quote_sheet(self):
        self.assertEqual(sources.quote_sheet("Bob's list"), "'Bob''s list'")


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
        self.assertEqual(sources.check_event(created, textutil.compile_labels(["Брифинг"]), SINCE).reason,
                         "plugin_output")


if __name__ == "__main__":
    unittest.main()
