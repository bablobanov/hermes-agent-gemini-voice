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
        rows = h.fake.sheets[sheet]
        self.assertEqual(rows[0], ["молоко", "хлеб"])
        self.assertTrue(rows[1][0].startswith("[Hermes] ответ"))  # the reply took the acknowledgement's place
        self.assertEqual(rows[2:], [["цапля принята"], []])  # and the acknowledgement's cell was emptied

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
        self.assertFalse(h.bridge.holds_reply("ответ"))
        await h.bridge.deliver_answer(f"doc.{doc}", "ответ")
        self.assertTrue(h.bridge.holds_reply("ответ"))
        self.assertFalse(h.bridge.holds_reply("другой ответ"))
        h.later(hours=25)  # the delivery ledger gives up re-sending after a day: so does the match
        self.assertFalse(h.bridge.holds_reply("ответ"))

    async def test_a_reply_google_refuses_for_good_is_not_reported_as_delivered(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        del h.fake.docs[doc]  # e.g. the owner deleted the document meanwhile
        self.assertIsNotNone(await h.bridge.deliver_answer(f"doc.{doc}", "ответ"))
        self.assertEqual(h.state(f"doc.{doc}"), "failed")
        self.assertFalse(h.bridge.holds_reply("ответ"))


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
        h.later(minutes=3)  # past the retry pause
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertIn("купи хлеб", h.dispatched[0].prompt)
        self.assertEqual(h.fake.events[event]["description"].count("принял"), 1)

    async def test_two_lost_acknowledgements_in_a_row_do_not_reject_the_task(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True  # a long turn: the new event waits in the queue past the 10-minute edit window
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        await h.bridge.tick()
        h.later(minutes=15)
        h.busy[0] = False
        h.fake.lose_response_once = ("PATCH", CAL + "/")  # the first acknowledgement lands, its response does not
        await h.bridge.tick()
        h.later(minutes=3)
        h.fake.fail_writes = True  # the second one does not land at all
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "queued")
        h.fake.fail_writes = False
        h.later(minutes=5)
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertIn("купи хлеб", h.dispatched[0].prompt)
        self.assertNotIn("принял", h.dispatched[0].prompt)
        self.assertEqual(h.fake.events[event]["description"].count("принял"), 1)

    async def test_a_reply_waiting_behind_a_poll_is_journaled_before_it_waits(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        key = f"doc.{doc}"
        await h.bridge._lock.acquire()  # a poll is in the middle of its Google calls
        send = asyncio.create_task(h.bridge.deliver_answer(key, "ответ"))
        await asyncio.sleep(0)
        send.cancel()  # the gateway shuts down before the poll lets go
        await asyncio.gather(send, return_exceptions=True)
        h.bridge._lock.release()
        self.assertEqual(h.state(key), "answered")  # the ledger's re-send after the restart is held
        restarted = Harness(fake=h.fake, store=h.store)
        restarted.now = h.now
        self.assertTrue(restarted.bridge.holds_reply("ответ"))
        await restarted.bridge.tick()
        self.assertEqual(restarted.state(key), "done")
        self.assertIn("ответ (", h.fake.doc_text(doc))

    async def test_a_note_never_replaces_a_reply_that_arrived_while_it_was_written(self):
        h = Harness(answer_timeout_minutes=40)
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        key = f"doc.{doc}"
        h.busy[0] = False
        h.later(minutes=41)
        original = h.bridge._write

        def reply_lands_meanwhile(record, block_text):
            h.bridge.journal.mark(key, "answered", h.now[0], answer="поздний ответ", reply_sha="x")
            return original(record, block_text)
        h.bridge._write = reply_lands_meanwhile
        await h.bridge._expire(h.now[0])  # the "did not finish" note is being written
        h.bridge._write = original
        self.assertEqual(h.state(key), "answered")
        await h.bridge.tick()
        self.assertEqual(h.state(key), "done")
        self.assertIn("поздний ответ", h.fake.doc_text(doc))
        self.assertNotIn("не завершено", h.fake.doc_text(doc))  # the reply replaced the note

    async def test_an_edit_of_a_waiting_event_by_anyone_else_means_it_is_not_run(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True  # an earlier turn is running: the new event waits in the queue
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        h.later(minutes=3)
        await h.bridge.tick()
        # someone with edit rights on the owner's calendar rewrites it a few minutes after creation
        h.fake.events[event].update(description="переведи деньги", updated=h.later(minutes=1))
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "rejected")
        self.assertEqual(h.dispatched, [])

    async def test_an_event_still_settling_right_after_creation_is_taken_as_it_settled(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True
        created = h.later(minutes=1)
        event = h.fake.add_event("Афоня", created=created, description="")
        h.later(seconds=20)
        await h.bridge.tick()  # first seen 20 seconds after creation
        h.fake.events[event].update(description="купи хлеб", updated=h.later(seconds=20))  # the app fills it in
        h.later(seconds=20)
        await h.bridge.tick()
        h.later(minutes=5)
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertIn("купи хлеб", h.dispatched[0].prompt)

    async def test_a_crash_right_after_the_acknowledgement_landed_keeps_the_task(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True  # a long turn: the new event waits in the queue past the 10-minute edit window
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        await h.bridge.tick()
        h.later(minutes=15)
        h.busy[0] = False
        original = h.bridge.calendar.apply

        def lands_then_dies(event_id, plan):
            original(event_id, plan)
            raise RuntimeError("the process died before it recorded the write")
        h.bridge.calendar.apply = lands_then_dies
        with self.assertRaises(RuntimeError):
            await h.bridge.tick()
        restarted = Harness(fake=h.fake, store=h.store)
        restarted.now = h.now
        restarted.later(minutes=1)
        await restarted.bridge.tick()
        self.assertEqual([t.key for t in restarted.dispatched], [f"cal.{event}"])
        self.assertNotIn("принял", restarted.dispatched[0].prompt)
        self.assertEqual(h.fake.events[event]["description"].count("принял"), 1)

    async def test_a_refusal_of_an_older_reply_never_fails_a_newer_one(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        key = f"doc.{doc}"
        original = h.bridge._write
        calls = []

        def newer_reply_arrives_then_refused(record, block_text):
            calls.append(block_text)
            if len(calls) == 1:  # while the first reply is being written, a newer one is journaled
                h.bridge.journal.mark(key, "answered", h.now[0], answer="новый ответ", reply_sha="b")
                raise api.ApiError("badRequest", "refused", 400)
            return original(record, block_text)
        h.bridge._write = newer_reply_arrives_then_refused
        self.assertIsNone(await h.bridge.deliver_answer(key, "старый ответ"))
        self.assertEqual(h.state(key), "answered")  # the newer reply is still owed, not failed
        await h.bridge.tick()
        self.assertEqual(h.state(key), "done")
        self.assertIn("новый ответ", h.fake.doc_text(doc))
        self.assertNotIn("не получилось", h.fake.doc_text(doc))

    async def test_a_note_google_did_not_take_shows_in_the_status_of_the_same_poll(self):
        h = Harness(answer_timeout_minutes=40)
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        h.busy[0] = False
        h.later(minutes=41)
        h.fake.offline_urls = [DOCS]
        await h.bridge.tick()  # the "did not finish" note cannot be written
        self.assertEqual(h.state(f"doc.{doc}"), "abandoned")
        self.assertEqual(h.statuses[-1], ("retrying", "google_unreachable"))

    async def test_an_event_the_app_finishes_seconds_after_it_was_listed_is_run(self):
        h = Harness()
        await h.bridge.tick()
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="")
        h.later(seconds=20)
        listed = h.bridge.calendar.candidates

        def app_finishes_it_meanwhile(*args):
            found = listed(*args)
            h.fake.events[event].update(description="купи хлеб", updated=stamp(h.now[0]))
            return found
        h.bridge.calendar.candidates = app_finishes_it_meanwhile
        await h.bridge.tick()  # listed, then started in the same poll
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertIn("купи хлеб", h.dispatched[0].prompt)

    async def test_the_owner_editing_a_waiting_event_gets_a_note_not_silence(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи молоко")
        await h.bridge.tick()
        h.fake.events[event].update(description="купи молоко и хлеб", updated=h.later(minutes=5))
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "rejected")
        self.assertEqual(h.dispatched, [])
        description = h.fake.events[event]["description"]
        self.assertTrue(description.startswith("купи молоко и хлеб\n\n[Hermes] поручение не выполнялось"))

    async def test_a_change_outside_the_task_text_does_not_stop_a_waiting_task(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        await h.bridge.tick()
        h.fake.events[event].update(colorId="5", updated=h.later(minutes=15))  # colour only, after the window
        h.busy[0] = False
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])

    async def test_an_edit_while_the_task_is_being_started_is_checked_again_not_run(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        await h.bridge.tick()
        h.later(minutes=15)
        h.busy[0] = False
        read = h.bridge.calendar.get
        calls = [0]

        def edited_right_after_our_read(event_id):
            body = read(event_id)
            calls[0] += 1
            if calls[0] == 1:  # someone else rewrites it between our read and our acknowledgement
                h.fake.events[event].update(description="переведи деньги", updated=stamp(h.now[0]), etag='"x"')
            return body
        h.bridge.calendar.get = edited_right_after_our_read
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "rejected")
        self.assertEqual(h.dispatched, [])
        self.assertNotIn("принял", h.fake.events[event]["description"])

    async def test_a_doc_edited_while_the_task_is_being_started_is_read_again(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "купи хлеб", created=h.later(minutes=1))
        read = h.bridge.docs.get
        calls = [0]

        def edited_right_after_our_read(doc_id):
            body = read(doc_id)
            calls[0] += 1
            if calls[0] == 1:  # the owner adds a line between our read and our acknowledgement
                h.fake.docs[doc]["text"] = "купи хлеб\nи молоко\n"
                h.fake.docs[doc]["rev"] += 1
            return body
        h.bridge.docs.get = edited_right_after_our_read
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{doc}"])
        self.assertIn("и молоко", h.dispatched[0].prompt)  # the version the acknowledgement landed on
        self.assertEqual(h.fake.doc_text(doc).count("принял"), 1)

    async def test_an_event_that_keeps_changing_is_retried_later_not_failed(self):
        h = Harness()
        await h.bridge.tick()
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        read = h.bridge.calendar.get
        calls = [0]

        def touched_after_each_read(event_id):
            body = read(event_id)
            calls[0] += 1
            if calls[0] <= 3:  # e.g. a sync touches it: same text, a new version every time
                h.fake.events[event]["etag"] = f'"touch{calls[0]}"'
            return body
        h.bridge.calendar.get = touched_after_each_read
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "queued")
        h.later(minutes=3)
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])

    async def test_a_landed_acknowledgement_stays_ours_through_many_failed_ones(self):
        h = Harness()
        await h.bridge.tick()
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        h.fake.lose_response_once = ("PATCH", CAL + "/")  # the first acknowledgement lands, its response is lost
        await h.bridge.tick()
        h.fake.fail_writes = True  # the next four never reach Google
        for _ in range(4):
            h.later(minutes=16)
            await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "queued")
        h.fake.fail_writes = False
        h.later(minutes=16)
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])
        self.assertEqual(h.fake.events[event]["description"].count("принял"), 1)

    async def test_no_note_lands_in_an_event_that_got_guests_meanwhile(self):
        h = Harness()
        await h.bridge.tick()
        h.busy[0] = True
        event = h.fake.add_event("Афоня", created=h.later(minutes=1), description="купи хлеб")
        await h.bridge.tick()
        h.fake.events[event].update(description="купи хлеб и вино", updated=h.later(minutes=5))
        h.busy[0] = False
        read = h.bridge.calendar.get

        def guests_added_right_after_our_read(event_id):
            body = read(event_id)
            h.fake.events[event].update(attendees=[{"self": True}, {"email": "x@example.com"}], etag='"g"')
            return body
        h.bridge.calendar.get = guests_added_right_after_our_read
        await h.bridge.tick()
        self.assertEqual(h.state(f"cal.{event}"), "rejected")
        self.assertNotIn("[Hermes]", h.fake.events[event]["description"])  # nothing written into a shared event

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
        await h.bridge.tick()  # the newest five, then the walk below them
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{task}"])

    async def test_google_not_answering_for_one_task_does_not_hold_up_polling(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня документ", "x", created=h.later(minutes=1))
        event = h.fake.add_event("Афоня событие", created=h.later(minutes=1))
        h.fake.offline_urls = [DOCS]  # only the Docs API times out
        delays = []
        for _ in range(3):
            delays.append(await h.bridge.tick())
            h.later(minutes=1)
        self.assertEqual(delays, [60.0, 60.0, 60.0])  # no back-off: Calendar and Drive answer
        self.assertEqual(h.statuses[-1], ("retrying", "google_unreachable"))
        self.assertEqual(h.state(f"doc.{doc}"), "queued")  # it waits for a retry, the event goes first
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{event}"])

    async def test_a_short_outage_of_one_api_delays_a_task_but_does_not_fail_it(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня документ", "x", created=h.later(minutes=1))
        h.fake.offline_urls = [DOCS]
        for _ in range(5):  # five minutes of polls while the Docs API is down
            await h.bridge.tick()
            h.later(minutes=1)
        self.assertEqual(h.state(f"doc.{doc}"), "queued")
        self.assertEqual(h.bridge.journal.get(f"doc.{doc}")["attempts"], 2)  # retried after pauses, not every poll
        h.fake.offline_urls = []
        h.later(minutes=5)
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{doc}"])
        self.assertEqual(h.statuses[-1], ("connected", ""))

    async def test_a_task_given_up_during_an_outage_gets_its_note_once_google_is_back(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня документ", "x", created=h.later(minutes=1))
        h.fake.offline_urls = [DOCS]
        for _ in range(60):  # an hour of polls while the Docs API is down
            await h.bridge.tick()
            h.later(minutes=1)
        self.assertEqual(h.state(f"doc.{doc}"), "failed")
        self.assertEqual(h.fake.doc_text(doc), "x\n")  # nothing could be written yet
        self.assertEqual(h.statuses[-1], ("retrying", "google_unreachable"))
        h.fake.offline_urls = []
        await h.bridge.tick()
        self.assertIn("не получилось", h.fake.doc_text(doc))  # the owner is not left with a silent document
        self.assertEqual(h.statuses[-1], ("connected", ""))
        self.assertEqual(h.bridge.journal.with_pending_note(), [])

    async def test_a_new_task_is_seen_at_once_however_many_files_the_window_holds(self):
        h = Harness()
        await h.bridge.tick()
        for i in range(1300):  # e.g. files shared with the owner today
            h.fake.add_doc(f"заметка {i}", "y", created=stamp(T0 + timedelta(seconds=i + 1)))
        h.later(hours=1)
        await h.bridge.tick()  # a walk over the whole window begins and takes several polls
        task = h.fake.add_doc("Афоня новая", "x", created=h.later(minutes=1))
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{task}"])

    async def test_a_walk_over_older_files_goes_on_and_ends(self):
        h = Harness()
        h.fake.drive_page_size = 1
        await h.bridge.tick()
        old_task = h.fake.add_doc("Афоня давняя", "x", created=h.later(minutes=1))
        for i in range(12):
            h.fake.add_doc(f"заметка {i}", "y", created=h.later(minutes=1))
        h.later(minutes=30)  # all of them older than the newest-files window: only the walk sees them
        for _ in range(3):
            await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{old_task}"])

    async def test_a_second_holding_more_files_than_a_poll_reads_does_not_hide_older_ones(self):
        h = Harness()
        await h.bridge.tick()
        old_task = h.fake.add_doc("Афоня давняя", "x", created=h.later(minutes=1))
        burst = h.later(minutes=1)
        for i in range(600):  # e.g. a bulk copy: all created within one second
            h.fake.add_doc(f"копия {i}", "y", created=burst)
        h.later(minutes=30)
        await h.bridge.tick()
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{old_task}"])

    async def test_a_second_holding_more_calendar_changes_than_a_poll_reads_hides_nothing_after_it(self):
        h = Harness()
        await h.bridge.tick()
        burst = h.later(minutes=1)
        for i in range(1300):  # e.g. an import: all changed within one second
            h.fake.add_event(f"импорт {i}", created=burst)
        task = h.fake.add_event("Афоня после импорта", created=h.later(minutes=1))
        h.later(minutes=1)
        for _ in range(3):
            await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"cal.{task}"])

    async def test_after_a_pause_files_created_meanwhile_count_as_new(self):
        h = Harness()
        await h.bridge.tick()
        for i in range(3000):  # a large window to walk
            h.fake.add_doc(f"заметка {i}", "y", created=stamp(T0 + timedelta(seconds=i + 1)))
        h.later(hours=2)
        await h.bridge.tick()
        task = h.fake.add_doc("Афоня перед паузой", "x", created=h.later(minutes=1))
        h.later(minutes=40)  # the gateway was down for a while
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{task}"])

    async def test_more_new_files_than_the_head_holds_never_hide_one_behind_an_old_walk(self):
        h = Harness()
        h.fake.drive_page_size = 2  # the head reads ten files per poll
        await h.bridge.tick()
        for i in range(50):
            h.fake.add_doc(f"заметка {i}", "y", created=stamp(T0 + timedelta(seconds=i + 1)))
        h.later(hours=1)
        await h.bridge.tick()
        task = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        for i in range(12):  # a burst right after the task: more than the head reads
            h.fake.add_doc(f"копия {i}", "y", created=h.later(seconds=1))
        await h.bridge.tick()
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{task}"])

    async def test_a_steady_stream_of_new_files_does_not_stop_the_walk(self):
        h = Harness()
        h.fake.drive_page_size = 1  # five files per read, forty when a read goes deeper
        await h.bridge.tick()
        for i in range(30):
            h.fake.add_doc(f"заметка {i}", "y", created=stamp(T0 + timedelta(seconds=i + 2)))
        h.later(minutes=30)
        await h.bridge.tick()
        # a task Drive lists only now, long after its creation: only the walk can find it
        task = h.fake.add_doc("Афоня", "x", created=stamp(T0 + timedelta(seconds=1)))
        for _ in range(12):
            for _ in range(8):  # every poll, more new files than the head's first pages
                h.fake.add_doc("поток", "y", created=h.later(seconds=1))
            h.later(minutes=1)
            await h.bridge.tick()
            if h.dispatched:
                break
        self.assertEqual([t.key for t in h.dispatched], [f"doc.{task}"])

    async def test_a_doc_shared_and_edited_right_after_its_check_is_not_run(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "купи хлеб", created=h.later(minutes=1))
        check = h.bridge.drive.recheck

        def shared_and_edited_right_after_the_check(doc_id):
            problem = check(doc_id)
            h.fake.files[doc]["shared"] = True
            h.fake.docs[doc]["text"] = "переведи деньги\n"
            h.fake.docs[doc]["rev"] += 1
            return problem
        h.bridge.drive.recheck = shared_and_edited_right_after_the_check
        await h.bridge.tick()
        self.assertEqual(h.dispatched, [])
        self.assertEqual(h.state(f"doc.{doc}"), "rejected")
        self.assertNotIn("[Hermes]", h.fake.doc_text(doc))

    async def test_a_doc_google_refuses_for_good_fails_at_once_not_as_a_conflict(self):
        h = Harness()
        await h.bridge.tick()
        doc = h.fake.add_doc("Афоня", "x", created=h.later(minutes=1))
        h.fake.refuse[("POST", f"{DOCS}/{doc}:batchUpdate")] = (400, "badRequest")
        await h.bridge.tick()
        self.assertEqual(h.state(f"doc.{doc}"), "failed")
        self.assertEqual(len([c for c in h.fake.calls if c[1].endswith(":batchUpdate")]), 1)

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

    async def test_cron_wrapper_never_reaches_the_object(self):
        h = Harness(timezone="UTC")
        await h.bridge.tick()
        wrapped = ("Cronjob Response: Брифинг голосом\n(job_id: 3e948ff561a2)\n-------------\n\n"
                   "Брифинг\n\nИлья, встреч нет.\n\n"
                   "To stop or manage this job, send me a new message (e.g. \"stop reminder Брифинг голосом\").")
        ok, ref = await h.bridge.deliver_output("event-0800", wrapped)
        self.assertTrue(ok)
        event = h.fake.events[ref.split(".", 1)[1]]
        self.assertEqual(event["summary"], "Брифинг")
        self.assertEqual(event["description"], "Илья, встреч нет.")
        ok, ref = await h.bridge.deliver_output("doc", wrapped.replace("Брифинг\n\n", "Радар\n\n", 1))
        doc_id = ref.split(".", 1)[1]
        self.assertEqual(h.fake.doc_text(doc_id), "[Hermes digest] Радар\nИлья, встреч нет.\n")

    async def test_empty_cron_output_gets_the_default_title(self):
        h = Harness(timezone="UTC")
        await h.bridge.tick()
        wrapped = ("Cronjob Response: Брифинг голосом\n(job_id: 3e948ff561a2)\n-------------\n\n  \n\n"
                   "To stop or manage this job, send me a new message (e.g. \"stop reminder Брифинг голосом\").")
        ok, ref = await h.bridge.deliver_output("event-0800", wrapped)
        self.assertTrue(ok)
        event = h.fake.events[ref.split(".", 1)[1]]
        self.assertTrue(event["summary"].startswith("Digest "), event["summary"])
        self.assertNotIn("job", event.get("description", ""))
        ok, ref = await h.bridge.deliver_output("doc", wrapped)
        doc_id = ref.split(".", 1)[1]
        self.assertTrue(h.fake.docs[doc_id]["title"].startswith("[Hermes digest] Digest "), h.fake.docs[doc_id]["title"])
        self.assertNotIn("Cronjob", h.fake.doc_text(doc_id))

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
