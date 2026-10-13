import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from _load import load
from fake_google import CAL, DOCS, DRIVE, FakeGoogle

api = load("google_api")
bridge_mod = load("bridge")
textutil = load("textutil")

T0 = datetime(2026, 10, 13, 17, 0, tzinfo=timezone.utc)


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class MemoryStore:
    """Round-trips through JSON like the real state file, so tests catch unserialisable state."""

    def __init__(self):
        self.raw = {}
        self.broken = False

    def get(self, key, default=None):
        return copy.deepcopy(json.loads(self.raw[key])) if key in self.raw else default

    def set(self, key, value):
        if self.broken:
            raise OSError("disk full")
        self.raw[key] = json.dumps(value)


async def run_inline(fn, *args):
    return fn(*args)


class Harness:
    """A bridge over the Google stand-in plus a stand-in gateway: dispatch opens a turn (the session is
    busy) and ``finish`` ends it the way the gateway does (final reply, then turn completion)."""

    def __init__(self, fake=None, store=None, **settings):
        self.fake = fake or FakeGoogle()
        self.store = store or MemoryStore()
        self.now = [T0]
        self.dispatched = []
        self.statuses = []
        self.busy = [False]
        self.refuse = False
        params = {"labels": ["Афон*"], "language": "ru", "timezone": "UTC"}
        params.update(settings)
        client = api.GoogleClient(api.TokenSource("a", "b", "c", transport=self.fake), transport=self.fake)
        self.bridge = bridge_mod.VoiceBridge(
            bridge_mod.Settings(**params), client, self.store, self._dispatch,
            lambda state, code, message: self.statuses.append((state, code)),
            clock=lambda: self.now[0], run_blocking=run_inline, session_busy=lambda: self.busy[0])

    async def _dispatch(self, task):
        if self.refuse:
            raise RuntimeError("the gateway did not accept the task")
        self.dispatched.append(task)
        self.busy[0] = True

    async def finish(self, key, answer, outcome="success"):
        if answer is not None:
            await self.bridge.deliver_answer(key, answer)
        self.busy[0] = False
        await self.bridge.on_turn_complete(key, outcome)

    def later(self, **delta):
        self.now[0] += timedelta(**delta)
        return stamp(self.now[0])

    def state(self, key):
        return self.bridge.journal.get(key)["state"]


class VoiceTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.bridge.tick()  # first run: sets the "since" watermark

    async def test_doc_task_end_to_end(self):
        h = self.h
        doc = h.fake.add_doc("Афоня найди рецепт борща", "на четверых", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual(len(h.dispatched), 1)
        task = h.dispatched[0]
        self.assertEqual(task.key, f"doc.{doc}")
        self.assertIn("Google Doc", task.prompt)
        self.assertIn("Афоня найди рецепт борща\nна четверых", task.prompt)
        self.assertEqual(h.fake.doc_text(doc), "на четверых\n[Hermes] принял, работаю (13.10 17:01)\n")
        await h.finish(task.key, "**Борщ**: свёкла, капуста")
        self.assertEqual(h.fake.doc_text(doc), "на четверых\n[Hermes] ответ (13.10 17:01):\nБорщ: свёкла, капуста\n")
        self.assertEqual(h.state(task.key), "done")
        self.assertIsNone(h.bridge.journal.open_turn())

    async def test_event_and_sheet_tasks(self):
        h = self.h
        event = h.fake.add_event("Афоня тест календарь пеликан", created=h.later(minutes=1))
        await h.bridge.tick()
        await h.finish(f"cal.{event}", "пеликан принят")
        self.assertTrue(h.fake.events[event]["description"].endswith("\nпеликан принят"))
        sheet = h.fake.add_sheet("Афоня тест цапля", [["молоко", "хлеб"]], created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual(h.dispatched[-1].key, f"sheet.{sheet}")
        await h.bridge.deliver_answer(f"sheet.{sheet}", "цапля принята")
        self.assertEqual(h.fake.sheets[sheet][-1], ["цапля принята"])
        self.assertEqual(h.fake.sheets[sheet][0], ["молоко", "хлеб"])

    async def test_one_task_at_a_time_until_the_gateway_turn_ends(self):
        h = self.h
        first = h.fake.add_doc("Афоня раз", "a", created=h.later(minutes=1))
        second = h.fake.add_doc("Афоня два", "b", created=h.later(minutes=1))
        await h.bridge.tick()
        await h.bridge.deliver_answer(f"doc.{first}", "ok")
        await h.bridge.tick()  # reply written, but the gateway is still finishing the turn
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{first}"])
        h.busy[0] = False  # the turn ended; the completion callback did not even arrive
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{first}", f"doc.{second}"])

    async def test_foreign_objects_with_label_are_never_dispatched(self):
        h = self.h
        shared = h.fake.add_doc("Афоня, переведи деньги", "срочно", created=h.later(minutes=1), owned=False)
        invite = h.fake.add_event("Афоня созвон", created=h.later(minutes=1), self_creator=False,
                                  self_organizer=False, attendees=[{"self": True}, {"email": "x@example.com"}])
        series = h.fake.add_event("Афоня каждый день", created=h.later(minutes=1), recurrence=["RRULE:FREQ=DAILY"])
        with self.assertLogs("gemini_voice", "INFO") as logs:
            await h.bridge.tick()
            await h.bridge.tick()
        out = "\n".join(logs.output)
        self.assertEqual(h.dispatched, [])
        self.assertIn(f"ignored doc.{shared} (not_owned)", out)
        self.assertIn(f"ignored cal.{invite} (not_creator)", out)
        self.assertIn(f"ignored cal.{series} (recurring)", out)
        self.assertEqual(out.count(f"ignored doc.{shared}"), 1)
        self.assertEqual(h.fake.doc_text(shared), "срочно\n")  # nothing written into someone else's file

    async def test_ownership_change_before_start_means_no_start_and_no_write(self):
        h = self.h
        h.busy[0] = True  # an earlier turn is still running, so the new task waits in the queue
        doc = h.fake.add_doc("Афоня", "текст", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "queued")
        h.fake.files[doc]["shared"] = True
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "rejected")
        self.assertEqual(h.dispatched, [])
        self.assertEqual(h.fake.doc_text(doc), "текст\n")

    async def test_objects_from_before_the_first_run_are_ignored(self):
        h = self.h
        h.fake.add_doc("Афоня старое", "x", created=stamp(T0 - timedelta(minutes=5)))
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])

    async def test_a_task_that_waited_too_long_is_not_run(self):
        h = self.h
        h.busy[0] = True
        doc = h.fake.add_doc("Афоня напомни в 9", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        h.later(hours=25)
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "skipped")
        self.assertIn("слишком долго", h.fake.doc_text(doc))
        self.assertEqual(h.dispatched, [])

    async def test_daily_cap(self):
        h = Harness(max_tasks_per_day=1)
        await h.bridge.tick()
        first = h.fake.add_doc("Афоня раз", "a", created=h.later(minutes=1))
        second = h.fake.add_doc("Афоня два", "b", created=h.later(minutes=1))
        await h.bridge.tick()
        await h.finish(f"doc.{first}", "ok")
        await h.bridge.tick()
        self.assertEqual(len(h.dispatched), 1)
        self.assertEqual(h.state(f"doc.{second}"), "skipped")
        self.assertIn("лимит", h.fake.doc_text(second))

    async def test_gateway_refusal_is_reported_in_the_object(self):
        h = self.h
        h.refuse = True
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "failed")
        self.assertIn("не получилось", h.fake.doc_text(doc))
        self.assertIsNone(h.bridge.journal.open_turn())


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_reply_goes_to_its_own_task_not_the_next_one(self):
        h = Harness(answer_timeout_minutes=40)
        await h.bridge.tick()
        first = h.fake.add_doc("Афоня раз", "a", created=h.later(minutes=1))
        second = h.fake.add_doc("Афоня два", "b", created=h.later(minutes=1))
        await h.bridge.tick()
        h.busy[0] = False  # the gateway lost the first turn
        h.later(minutes=41)
        await h.bridge.tick()  # first abandoned with a note, second dispatched
        self.assertEqual(h.state(f"doc.{first}"), "abandoned")
        self.assertEqual(h.dispatched[-1].key, f"doc.{second}")
        await h.bridge.deliver_answer(f"doc.{first}", "поздний ответ")  # anchored to the first task
        self.assertIn("поздний ответ", h.fake.doc_text(first))
        self.assertNotIn("не завершено", h.fake.doc_text(first))
        self.assertNotIn("поздний ответ", h.fake.doc_text(second))
        self.assertEqual(h.state(f"doc.{second}"), "dispatched")

    async def test_running_turn_is_never_abandoned_or_overtaken(self):
        h = Harness(answer_timeout_minutes=40)
        await h.bridge.tick()
        first = h.fake.add_doc("Афоня долгая", "a", created=h.later(minutes=1))
        h.fake.add_doc("Афоня следующая", "b", created=h.later(minutes=1))
        await h.bridge.tick()
        h.later(minutes=90)  # the gateway is still running the first turn
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{first}"), "dispatched")
        self.assertEqual(len(h.dispatched), 1)

    async def test_unknown_anchor_is_dropped(self):
        h = Harness()
        await h.bridge.tick()
        h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertIsNotNone(await h.bridge.deliver_answer("doc.someone-else", "x"))


class AnchorlessReplyTests(unittest.IsolatedAsyncioTestCase):
    """A reply without an anchor (a turn the gateway resumed after a restart, a redelivered reply) goes only
    to the task inherited from the previous process, never into another task's object."""

    @staticmethod
    async def restarted(answer_timeout_minutes=40):
        first = Harness(answer_timeout_minutes=answer_timeout_minutes)
        await first.bridge.tick()
        a = first.fake.add_doc("Афоня первая", "a", created=first.later(minutes=1))
        await first.bridge.tick()  # A is running when the gateway restarts
        b = first.fake.add_doc("Афоня вторая", "b", created=first.later(minutes=1))
        second = Harness(fake=first.fake, store=first.store, answer_timeout_minutes=answer_timeout_minutes)
        second.now = first.now
        return second, f"doc.{a}", f"doc.{b}"

    async def test_resumed_turn_answers_its_own_task_and_nothing_else(self):
        h, a, b = await self.restarted()
        h.busy[0] = True  # the gateway is still resuming interrupted turns: new tasks wait
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])
        self.assertEqual(h.state(a), "dispatched")
        self.assertIsNone(await h.bridge.deliver_answer(None, "работа прервалась, продолжить?"))
        self.assertIn("продолжить?", h.fake.doc_text(a.split(".", 1)[1]))
        # an attachment notice after the reply must not replace it
        self.assertIsNotNone(await h.bridge.deliver_answer(None, "https://example.com/chart.png"))
        self.assertNotIn("chart.png", h.fake.doc_text(a.split(".", 1)[1]))
        await h.bridge.on_turn_complete(None, "success")
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [b])
        self.assertIsNotNone(await h.bridge.deliver_answer(None, "без якоря"))
        self.assertNotIn("без якоря", h.fake.doc_text(b.split(".", 1)[1]))

    async def test_after_a_long_outage_a_late_reply_never_lands_in_the_next_task(self):
        h, a, b = await self.restarted(answer_timeout_minutes=40)
        h.later(minutes=50)  # down longer than the answer timeout: no resume comes
        await h.bridge.tick()
        self.assertEqual(h.state(a), "abandoned")
        self.assertEqual([t.key for t in h.dispatched], [b])
        self.assertIsNotNone(await h.bridge.deliver_answer(None, "ответ первой"))
        self.assertNotIn("ответ первой", h.fake.doc_text(b.split(".", 1)[1]))
        await h.bridge.on_turn_complete(None, "success")  # an id-less turn end does not close B
        self.assertEqual(h.state(b), "dispatched")

    async def test_anchorless_text_never_replaces_a_written_reply(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertIsNone(await h.bridge.deliver_answer(f"doc.{doc}", "настоящий ответ"))
        self.assertIsNotNone(await h.bridge.deliver_answer(None, "https://example.com/chart.png"))
        self.assertIn("настоящий ответ", h.fake.doc_text(doc))
        self.assertNotIn("chart.png", h.fake.doc_text(doc))

    async def test_a_cancelled_resumed_turn_keeps_its_task(self):
        h, a, _ = await self.restarted()
        h.busy[0] = True
        await h.bridge.tick()
        await h.bridge.on_turn_complete(None, "cancelled")  # the gateway may resume it once more
        self.assertIsNone(await h.bridge.deliver_answer(None, "ответ после повторного продолжения"))
        self.assertIn("повторного продолжения", h.fake.doc_text(a.split(".", 1)[1]))

    async def test_only_replies_already_held_count_as_redelivered(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertFalse(await h.bridge.holds_reply("ответ"))
        await h.bridge.deliver_answer(f"doc.{doc}", "ответ")
        self.assertTrue(await h.bridge.holds_reply("ответ"))
        self.assertFalse(await h.bridge.holds_reply("другой ответ"))

    async def test_a_reply_google_refuses_for_good_is_not_reported_as_delivered(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        del h.fake.docs[doc]  # e.g. the owner deleted the document meanwhile
        self.assertIsNotNone(await h.bridge.deliver_answer(f"doc.{doc}", "ответ"))
        self.assertEqual(h.state(f"doc.{doc}"), "failed")
        self.assertFalse(await h.bridge.holds_reply("ответ"))


class ExactlyOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_mid_turn_never_redispatches(self):
        first = Harness()
        await first.bridge.tick()
        doc = first.fake.add_doc("Афоня долгая задача", "x", created=first.later(minutes=1))
        await first.bridge.tick()
        self.assertEqual(len(first.dispatched), 1)
        # gateway restart: a new process, same Google account and same state file
        second = Harness(fake=first.fake, store=first.store)
        second.now = first.now
        for _ in range(3):
            second.later(minutes=1)
            await second.bridge.tick()
        self.assertEqual(second.dispatched, [])
        # the gateway resumes the interrupted turn; its reply arrives without an anchor
        self.assertIsNone(await second.bridge.deliver_answer(None, "работа прервалась перезапуском, продолжить?"))
        self.assertIn("продолжить?", first.fake.doc_text(doc))
        self.assertEqual(second.state(f"doc.{doc}"), "done")

    async def test_failed_acknowledgement_requeues_without_dispatch(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        h.fake.fail_writes = True  # Google is up for reads but refuses the acknowledgement write
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])
        self.assertEqual(h.state(f"doc.{doc}"), "queued")
        h.fake.fail_writes = False
        h.later(minutes=5)
        await h.bridge.tick()
        self.assertEqual(len(h.dispatched), 1)

    async def test_crash_while_claimed_runs_once_after_restart(self):
        for kind in ("doc", "cal"):
            h = Harness()
            await h.bridge.tick()
            if kind == "doc":
                obj = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
            else:
                obj = h.fake.add_event("Афоня", created=h.later(minutes=1), description="x")
            await h.bridge.tick()
            self.assertEqual(len(h.dispatched), 1)
            # rewind the state file to "claimed": the process died after the acknowledgement, before dispatch
            data = h.store.get("journal")
            data["objects"][f"{kind}.{obj}"].update(state="claimed", turn_open=False)
            h.store.set("journal", data)
            restarted = Harness(fake=h.fake, store=h.store)
            restarted.now = h.now
            restarted.later(minutes=1)
            await restarted.bridge.tick()
            self.assertEqual(len(restarted.dispatched), 1, kind)
            text = h.fake.doc_text(obj) if kind == "doc" else h.fake.events[obj]["description"]
            self.assertEqual(text.count("принял"), 1, kind)  # the acknowledgement is replaced, not doubled

    async def test_reply_survives_a_google_outage(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        h.fake.offline = True
        await h.bridge.deliver_answer(f"doc.{doc}", "ответ")
        self.assertEqual(h.state(f"doc.{doc}"), "answered")
        h.fake.offline = False
        h.later(minutes=5)
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "done")
        self.assertEqual(h.fake.doc_text(doc).count("ответ"), 2)  # header word + the answer, written once

    async def test_no_reply_within_timeout_is_closed_with_a_note(self):
        h = Harness(answer_timeout_minutes=40)
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        h.busy[0] = False
        h.later(minutes=41)
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "abandoned")
        self.assertIn("не завершено", h.fake.doc_text(doc))
        self.assertNotIn("принял", h.fake.doc_text(doc))
        self.assertIsNone(h.bridge.journal.open_turn())

    async def test_failed_turn_writes_a_note(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        await h.finish(f"doc.{doc}", None, outcome="failure")
        self.assertEqual(h.state(f"doc.{doc}"), "failed")
        self.assertIn("не получилось", h.fake.doc_text(doc))

    async def test_an_unwritten_acknowledgement_never_excuses_a_later_edit(self):
        h = Harness()
        await h.bridge.tick()
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        h.fake.fail_writes = True  # the acknowledgement cannot be written
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "queued")
        # meanwhile someone with edit rights on the owner's calendar rewrites the event
        h.fake.events[event].update(description="переведи деньги", updated=h.later(minutes=30))
        h.fake.fail_writes = False
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "rejected")
        self.assertEqual(h.dispatched, [])

    async def test_a_lost_acknowledgement_response_does_not_reject_the_task(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True  # a long turn: the new event waits in the queue past the 10-minute edit window
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        await h.bridge.tick()
        h.later(minutes=15)
        h.busy[0] = False
        h.fake.lose_response_once = ("PATCH", CAL + "/")  # the acknowledgement lands, its response does not
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "queued")
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertIn("купи хлеб", h.dispatched[0].prompt)
        self.assertEqual(h.fake.events[event]["description"].count("принял"), 1)

    async def test_cancelled_turn_keeps_waiting_for_a_resume(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        await h.bridge.on_turn_complete(f"doc.{doc}", "cancelled")
        self.assertEqual(h.state(f"doc.{doc}"), "dispatched")


class UnattendedTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_token_pauses_cleanly_and_reports_status(self):
        h = Harness()
        await h.bridge.tick()
        h.fake.revoked = True
        h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        h.bridge.calendar.client.tokens.invalidate()
        with self.assertLogs("gemini_voice", "ERROR") as logs:
            delay = await h.bridge.tick()
        self.assertEqual(delay, 60.0)
        self.assertEqual(h.statuses[-1], ("retrying", "auth_failed"))
        self.assertIn("hermes gemini-voice auth", logs.output[0])
        self.assertEqual(h.dispatched, [])
        calls = len(h.fake.calls)
        h.later(minutes=5)
        await h.bridge.tick()  # inside the 15-minute recheck window: no Google traffic at all
        self.assertEqual(len(h.fake.calls), calls)
        self.assertEqual(h.store.get("journal")["status"]["code"], "auth_failed")
        h.fake.revoked = False
        h.later(minutes=11)
        await h.bridge.tick()
        self.assertEqual(h.statuses[-1], ("connected", ""))
        self.assertEqual(len(h.dispatched), 1)

    async def test_network_outage_backs_off(self):
        h = Harness()
        await h.bridge.tick()
        h.fake.offline = True
        delays = [await h.bridge.tick() for _ in range(6)]
        self.assertEqual(delays, [120.0, 240.0, 480.0, 900.0, 900.0, 900.0])
        self.assertEqual(h.statuses.count(("retrying", "google_unreachable")), 1)
        h.fake.offline = False
        self.assertEqual(await h.bridge.tick(), 60.0)
        self.assertEqual(h.statuses[-1], ("connected", ""))

    async def test_one_refused_api_does_not_stop_the_others(self):
        h = Harness()
        await h.bridge.tick()
        h.fake.refuse[("GET", "https://www.googleapis.com/drive/v3/files")] = (403, "accessNotConfigured")
        event = h.fake.add_event("Афоня", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertEqual(h.statuses[-1], ("retrying", "api_disabled"))
        self.assertEqual(h.store.get("journal")["last_poll_ok"], T0.isoformat())  # not a fully successful poll

    async def test_one_unreachable_api_does_not_stop_the_others_or_the_replies(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        h.fake.offline = True
        await h.bridge.deliver_answer(f"doc.{doc}", "ответ")  # Google down: the reply waits in the journal
        h.fake.offline, h.busy[0] = False, False
        h.fake.offline_urls = [CAL]  # only the Calendar API times out now
        second = h.fake.add_doc("Афоня два", "y", created=h.later(minutes=1))
        self.assertEqual(await h.bridge.tick(), 60.0)
        self.assertEqual(h.state(f"doc.{doc}"), "done")
        self.assertEqual(h.dispatched[-1].key, f"doc.{second}")
        self.assertEqual(h.statuses[-1], ("retrying", "google_unreachable"))
        h.fake.offline_urls = [CAL, DRIVE]
        self.assertEqual(await h.bridge.tick(), 120.0)  # no listing answered: back off

    async def test_a_burst_of_calendar_changes_does_not_hide_a_task_behind_it(self):
        h = Harness()
        h.fake.cal_page_size = 2  # five pages of two: ten changes per poll
        await h.bridge.tick()
        for i in range(1, 13):  # an import: twelve changes within four minutes, inside the 10-minute overlap
            h.fake.add_event(f"встреча {i}", created=stamp(T0 + timedelta(seconds=20 * i)))
        task = h.fake.add_event("Афоня после всплеска", created=stamp(T0 + timedelta(seconds=20 * 13)))
        h.later(minutes=10)
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])  # this poll reached only the first ten changes
        await h.bridge.tick()  # the same pass goes on from its next page
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{task}"])
        self.assertIsNone(h.bridge.journal.cursor("calendar"))

    async def test_more_new_files_than_one_poll_reads_are_all_seen(self):
        h = Harness()
        h.fake.drive_page_size = 1  # five pages of one: five files per poll, newest first
        await h.bridge.tick()
        task = h.fake.add_doc("Афоня самая первая", "x", created=h.later(minutes=1))
        for i in range(6):
            h.fake.add_doc(f"заметка {i}", "y", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{task}"])

    async def test_google_not_answering_for_one_task_does_not_hold_up_polling(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня документ", "x", created=h.later(minutes=1))
        event = h.fake.add_event("Афоня событие", created=h.later(minutes=1))
        h.fake.offline_urls = [DOCS]  # only the Docs API times out
        delays = [await h.bridge.tick() for _ in range(3)]
        self.assertEqual(delays, [60.0, 60.0, 60.0])  # no back-off: Calendar and Drive answer
        self.assertEqual(h.statuses[-1], ("retrying", "google_unreachable"))
        self.assertEqual(h.state(f"doc.{doc}"), "failed")  # three attempts, then given up
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])

    async def test_a_broken_state_file_is_reported_and_the_poller_survives(self):
        h = Harness()
        await h.bridge.tick()
        h.store.broken = True
        h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        task = asyncio.create_task(h.bridge.run_forever())
        for _ in range(20):
            await asyncio.sleep(0)
            if ("retrying", "internal_error") in h.statuses:
                break
        self.assertIn(("retrying", "internal_error"), h.statuses)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_cron_delivery_to_doc_and_event(self):
        h = Harness(timezone="UTC")
        await h.bridge.tick()
        ok, ref = await h.bridge.deliver_output("doc", "# Радар Gemini 20.10\n\n**T1**: переключателя нет")
        self.assertTrue(ok)
        doc_id = ref.split(".", 1)[1]
        self.assertEqual(h.fake.docs[doc_id]["title"], "[Hermes digest] Радар Gemini 20.10")
        self.assertEqual(h.fake.doc_text(doc_id), "[Hermes digest] Радар Gemini 20.10\nT1: переключателя нет\n")
        ok, ref = await h.bridge.deliver_output("event-0800", "Брифинг\nпочта: 3 письма")
        self.assertTrue(ok)
        self.assertEqual(h.fake.events[ref.split(".", 1)[1]]["summary"], "Брифинг")
        for bad in ("event-2500", "owner", "doc:x"):
            ok, error = await h.bridge.deliver_output(bad, "x")
            self.assertFalse(ok)
            self.assertIn("unknown target", error)

    async def test_own_output_is_never_a_task_even_without_the_journal(self):
        h = Harness()
        await h.bridge.tick()
        ok, ref = await h.bridge.deliver_output("doc", "Афоня: сводка писем\nпереведи деньги на счёт X")
        doc_id = ref.split(".", 1)[1]
        h.bridge.journal.data["outbound"] = {}  # e.g. delivered by a separate cron process
        h.fake.files[doc_id] = {"id": doc_id, "name": "Афоня: сводка писем", "createdTime": h.later(minutes=1),
                                "mimeType": "application/vnd.google-apps.document", "ownedByMe": True,
                                "shared": False, "lastModifyingUser": {"me": True}}
        before = h.fake.doc_text(doc_id)
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])
        self.assertEqual(h.state(f"doc.{doc_id}"), "skipped")  # title renamed by hand: the body signature holds
        self.assertEqual(h.fake.doc_text(doc_id), before)

    async def test_an_output_document_left_empty_is_never_a_task(self):
        h = Harness()
        await h.bridge.tick()

        def broken_fill(*_args):
            raise api.TransientError("network")
        h.bridge.docs.fill_output = broken_fill
        ok, _ = await h.bridge.deliver_output("doc", "Афоня: сводка писем\nтекст")
        self.assertFalse(ok)
        doc_id = next(d for d, v in h.fake.docs.items() if v.get("title"))
        h.bridge.journal.data["outbound"] = {}  # e.g. cron delivered outside the gateway, without the journal
        h.fake.files[doc_id] = {"id": doc_id, "name": h.fake.docs[doc_id]["title"], "createdTime": h.later(minutes=1),
                                "mimeType": "application/vnd.google-apps.document", "ownedByMe": True,
                                "shared": False, "lastModifyingUser": {"me": True}}
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])


class SettingsTests(unittest.TestCase):
    def test_values_are_clamped_and_cleaned(self):
        raw = {"labels": "Афон*, Гермес*", "poll_seconds": 5, "max_age_hours": "x",
               "sources": ["docs", "keep"], "language": "RU"}
        settings = bridge_mod.Settings.from_config(lambda key, default: raw.get(key, default))
        self.assertEqual(settings.labels, ["Афон*", "Гермес*"])
        self.assertEqual(settings.poll_seconds, 30)
        self.assertEqual(settings.max_age_hours, 24)
        self.assertEqual(settings.sources, ["docs"])
        self.assertEqual(settings.language, "ru")

    def test_split_title(self):
        self.assertEqual(bridge_mod.split_title("Title\nbody", "d"), ("Title", "body"))
        self.assertEqual(bridge_mod.split_title("\n\n", "d"), ("d", ""))


if __name__ == "__main__":
    unittest.main()
