"""Engine-independent core: poll Google, keep the journal, hand tasks over one at a time, write replies.

The Hermes adapter (``adapter.py``) wires this to the gateway; everything here is testable without
Hermes and without network access.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import textutil
from .google_api import ApiError, AuthError, GoogleClient, GoogleError, TransientError
from .journal import Journal, Store, from_iso, iso
from .sources import Calendar, CalendarOutput, Docs, Drive, Locator, Sheets, Verdict

logger = logging.getLogger("gemini_voice")

PLATFORM = "gemini_voice"
OWNER_CHAT = "owner"
SOURCES = ("calendar", "docs", "sheets")
FOREIGN_REASONS = {"not_creator", "not_organizer", "has_attendees", "event_type", "recurring",
                   "edited_after_creation", "not_owned", "shared", "modified_by_other"}
TASK_TEXT_LIMIT = 8000
ANSWER_LIMITS = {"cal": 7000, "doc": 30000, "sheet": 30000}
KIND_NAMES = {"cal": "Google Calendar event", "doc": "Google Doc", "sheet": "Google Sheet"}
AUTH_RECHECK = timedelta(minutes=15)
CALENDAR_SKEW = timedelta(minutes=10)
MAX_BACKOFF = 900.0
MAX_LOGGED_REJECTS = 1000
CLAIM_ATTEMPTS = 3
_SLOT = re.compile(r"^event-([01]\d|2[0-3])([0-5]\d)$")


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _string_list(value: Any, default: List[str]) -> List[str]:
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return list(default)
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    return cleaned or list(default)


@dataclass
class Settings:
    labels: List[str] = field(default_factory=lambda: ["Hermes"])
    language: str = "en"
    timezone: str = "UTC"
    poll_seconds: int = 60
    max_age_hours: int = 24
    answer_timeout_minutes: int = 40
    max_tasks_per_day: int = 30
    sources: List[str] = field(default_factory=lambda: list(SOURCES))

    @classmethod
    def from_config(cls, get: Callable[[str, Any], Any]) -> "Settings":
        """Settings from ``plugins.entries.gemini-voice.settings`` with every value clamped to sane bounds."""
        default = cls()
        return cls(
            labels=_string_list(get("labels", default.labels), default.labels),
            language=str(get("language", default.language) or "en").strip().lower(),
            timezone=str(get("timezone", default.timezone) or "UTC").strip(),
            poll_seconds=_clamp(get("poll_seconds", 60), 30, 3600, 60),
            max_age_hours=_clamp(get("max_age_hours", 24), 1, 168, 24),
            answer_timeout_minutes=_clamp(get("answer_timeout_minutes", 40), 5, 720, 40),
            max_tasks_per_day=_clamp(get("max_tasks_per_day", 30), 1, 1000, 30),
            sources=[s for s in _string_list(get("sources", default.sources), default.sources) if s in SOURCES]
            or list(SOURCES),
        )


def resolve_zone(name: str) -> Tuple[Any, str]:
    """``(tzinfo, IANA name)``; an unknown zone falls back to UTC with a warning."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        logger.warning("gemini-voice: unknown timezone %r, using UTC", name)
        return timezone.utc, "UTC"


def split_title(text: str, default: str) -> Tuple[str, str]:
    """First non-empty line as the title (max 100 chars), the rest as the body."""
    lines = text.strip().split("\n")
    title = lines[0].strip(" #*-:") if lines and lines[0].strip() else ""
    if not title:
        return default, text.strip()
    return title[:100], "\n".join(lines[1:]).strip()


@dataclass(frozen=True)
class Task:
    key: str
    kind: str
    title: str
    prompt: str


Dispatch = Callable[[Task], Awaitable[None]]
PublishStatus = Callable[[str, str, str], None]  # (platform_state, error_code, message)


class VoiceBridge:
    """Poller, journal and reply writer for one Google account and one Hermes profile."""

    def __init__(self, settings: Settings, client: GoogleClient, store: Store, dispatch: Dispatch,
                 publish_status: PublishStatus, *, clock: Optional[Callable[[], datetime]] = None,
                 run_blocking: Optional[Callable[..., Awaitable[Any]]] = None,
                 session_busy: Callable[[], bool] = lambda: False):
        self.settings = settings
        self.journal = Journal(store)
        self._text = textutil.strings(settings.language)
        self.calendar, self.drive = Calendar(client), Drive(client)
        self.docs, self.calendar_output = Docs(client), CalendarOutput(client)
        self.sheets = Sheets(client, shortened=self._text["truncated"])
        self._dispatch, self._publish, self._session_busy = dispatch, publish_status, session_busy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._run = run_blocking or asyncio.to_thread
        self._patterns = textutil.compile_labels(settings.labels)
        self._zone, self._zone_name = resolve_zone(settings.timezone)
        self._lock = asyncio.Lock()
        self._recovered = False
        # The task whose gateway turn was open when this process started: the only one a reply without an
        # anchor (a turn the gateway resumed, or the delivery ledger re-sending a cut-off reply) can go to.
        self._inherited: Optional[str] = None
        self._auth_failed_at: Optional[datetime] = None
        self._backoff = 0.0
        self._status: Optional[Tuple[str, str]] = None
        self._logged_rejects: set = set()

    # ------------------------------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        while True:
            try:
                delay = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # e.g. the state file cannot be written: say so in the status, keep going
                logger.exception("gemini-voice: poll cycle failed")
                self._set_status("retrying", "internal_error",
                                 f"poll cycle failed ({type(exc).__name__}); see the gateway log")
                delay = float(self.settings.poll_seconds * 2)
            await asyncio.sleep(delay)

    def recover(self, now: datetime) -> None:
        """After a restart: claimed (never dispatched) tasks go back to the queue; dispatched ones stay, and
        the one whose turn was open is inherited (the gateway may resume that turn without an anchor)."""
        for key in self.journal.with_state("claimed"):
            self.journal.mark(key, "queued", now)
            logger.info("gemini-voice: %s was claimed but not dispatched before the restart; requeued", key)
        key = self.journal.open_turn()
        record = self.journal.get(key)
        self._inherited = key if record is not None and record.get("state") == "dispatched" else None
        if self._inherited:
            logger.info("gemini-voice: %s was running before the restart; waiting for the resumed turn", key)

    def _ensure_recovered(self, now: datetime) -> None:
        if not self._recovered:
            self.recover(now)
            self._recovered = True

    async def tick(self) -> float:
        """One poll cycle; returns seconds until the next one."""
        now = self._clock()
        if self._auth_failed_at and now - self._auth_failed_at < AUTH_RECHECK:
            return float(self.settings.poll_seconds)
        stalled: List[GoogleError] = []
        try:
            async with self._lock:
                self._ensure_recovered(now)
                problems, listings = await self._poll(now)
                stalled = await self._advance(now)  # replies and the queue move on even when a listing failed
        except AuthError as exc:
            return self._on_auth_error(exc, now)
        except TransientError as exc:
            return self._on_transient(exc)
        except ApiError as exc:
            problems, listings = [exc], 0
        unreachable = [p for p in problems if isinstance(p, TransientError)]
        if unreachable and len(unreachable) == listings:  # Google answered none of the listings
            return self._on_transient(unreachable[0])
        self._on_success(problems + stalled)  # a reply or a task held up by Google is not "connected"
        return float(self.settings.poll_seconds)

    # ---------------------------------------------------------------------------------- polling
    async def _poll(self, now: datetime) -> Tuple[List[GoogleError], int]:
        """List candidates from every source; a source Google refuses or does not answer does not stop the
        others (an authorization error concerns them all and propagates). Returns the problems and how
        many listings were requested."""
        since = self.journal.since(now)
        not_before = max(since, now - timedelta(hours=self.settings.max_age_hours))
        calendar_mark = self.journal.calendar_updated_min(not_before)
        verdicts: List[Verdict] = []
        problems: List[GoogleError] = []
        listings = 0
        if "calendar" in self.settings.sources:
            listings += 1
            try:
                found, calendar_mark = await self._list_calendar(not_before, calendar_mark, now)
                verdicts += found
            except (ApiError, TransientError) as exc:
                problems.append(exc)
        kinds = [k for k, s in (("doc", "docs"), ("sheet", "sheets")) if s in self.settings.sources]
        if kinds:
            listings += 1
            try:
                verdicts += await self._list_drive(not_before, kinds)
            except (ApiError, TransientError) as exc:
                problems.append(exc)
        for verdict in verdicts:
            self._consider(verdict, now)
        self.journal.poll_ok(now if not problems else None, calendar_mark)
        return problems, listings

    async def _list_calendar(self, not_before: datetime, mark: datetime,
                             now: datetime) -> Tuple[List[Verdict], datetime]:
        """This poll's share of a calendar pass, and the watermark. A pass longer than one poll goes on next
        time from its page token, so a burst of changes never hides the events behind it for good; the
        watermark moves (to the pass's start) only once the whole pass has been read."""
        pass_ = self.journal.cursor("calendar") or {"min": iso(max(not_before, mark - CALENDAR_SKEW)),
                                                    "started": iso(now)}
        try:
            found, token = await self._run(self.calendar.candidates, self._patterns, not_before,
                                           from_iso(pass_["min"]) or not_before, pass_.get("token"))
        except ApiError:
            self.journal.set_cursor("calendar", None)  # e.g. an expired page token: the pass starts again
            raise
        self.journal.set_cursor("calendar", dict(pass_, token=token) if token else None)
        return found, mark if token else (from_iso(pass_["started"]) or now)

    async def _list_drive(self, not_before: datetime, kinds: List[str]) -> List[Verdict]:
        """This poll's share of a Drive pass (newest first); a longer pass goes on from its page token."""
        pass_ = self.journal.cursor("drive") or {"min": iso(not_before)}
        try:
            found, token = await self._run(self.drive.candidates, self._patterns, not_before, kinds,
                                           self.journal.outbound_ids(), from_iso(pass_["min"]) or not_before,
                                           pass_.get("token"))
        except ApiError:
            self.journal.set_cursor("drive", None)
            raise
        self.journal.set_cursor("drive", dict(pass_, token=token) if token else None)
        return found

    def _consider(self, verdict: Verdict, now: datetime) -> None:
        obj = verdict.obj
        if self.journal.get(obj.key) is not None:
            return
        if verdict.accepted:
            self.journal.queue(obj.key, obj.kind, obj.id, obj.title, obj.created, now)
            logger.info("gemini-voice: queued %s", obj.key)
            return
        if verdict.reason in FOREIGN_REASONS and textutil.has_label(obj.title, self._patterns):
            if obj.key not in self._logged_rejects:
                if len(self._logged_rejects) >= MAX_LOGGED_REJECTS:
                    self._logged_rejects.clear()
                self._logged_rejects.add(obj.key)
                logger.info("gemini-voice: ignored %s (%s): not a task from the account owner",
                            obj.key, verdict.reason)

    # --------------------------------------------------------------------------------- advancing
    async def _advance(self, now: datetime) -> List[GoogleError]:
        """Write waiting replies, close overdue tasks, start the next one. Google not answering for one
        object does not stop the rest; those problems come back for the status. A broken token propagates."""
        stalled: List[GoogleError] = []
        for key in self.journal.with_state("answered"):
            try:
                await self._write_answer(key, now, raise_unavailable=True)
            except TransientError as exc:
                stalled.append(exc)
        await self._expire(now)
        if not self._turn_busy(now):
            key = self.journal.next_queued()
            if key:
                try:
                    await self._start(key, now)
                except TransientError as exc:  # the task waits for the next poll (see _claim)
                    stalled.append(exc)
        self.journal.prune(now)
        return stalled

    def _timeout(self) -> timedelta:
        return timedelta(minutes=self.settings.answer_timeout_minutes)

    async def _expire(self, now: datetime) -> None:
        if self._session_busy():
            return  # a turn is still running; the gateway's own turn timeout bounds it
        for key in self.journal.with_state("dispatched"):
            started = from_iso((self.journal.get(key) or {}).get("dispatched_at")) or now
            if now - started > self._timeout():
                logger.warning("gemini-voice: no reply for %s within %s; closing it", key, self._timeout())
                await self._close_with_note(key, "abandoned", self._text["abandoned"], now)
                self.journal.close_turn(key)

    def _turn_busy(self, now: datetime) -> bool:
        """True while the gateway may still be running the last task's turn: no new task meanwhile."""
        if self._session_busy():
            return True
        key = self.journal.open_turn()
        record = self.journal.get(key)
        if record is None:
            return False
        started = from_iso(record.get("dispatched_at")) or now
        if record.get("state") == "dispatched" and now - started <= self._timeout():
            return True  # admitted but not visible yet, or about to be resumed after a restart
        self.journal.close_turn(key)
        return False

    async def _start(self, key: str, now: datetime) -> None:
        record = self.journal.get(key) or {}
        created = from_iso(record.get("created"))
        if created and now - created > timedelta(hours=self.settings.max_age_hours):
            await self._close_with_note(key, "skipped", self._text["stale"], now)
            return
        day = now.astimezone(self._zone).strftime("%Y-%m-%d")
        if self.journal.count_today(day) >= self.settings.max_tasks_per_day:
            await self._close_with_note(key, "skipped", self._text["daily_cap"].format(
                limit=self.settings.max_tasks_per_day), now)
            return
        record = self.journal.mark(key, "claimed", now, attempts=int(record.get("attempts") or 0) + 1)
        prompt = await self._claim(key, record, now)
        if prompt is None:
            return
        self.journal.count_task(day)
        self._inherited = None  # a newer turn opens: a reply without an anchor can no longer be placed
        self.journal.mark(key, "dispatched", now, turn_open=True)
        logger.info("gemini-voice: dispatched %s", key)
        try:
            await self._dispatch(Task(key, record["kind"], record.get("title") or "", prompt))
        except Exception as exc:
            logger.exception("gemini-voice: gateway did not take %s", key)
            await self._close_with_note(key, "failed", self._text["failed"].format(reason=type(exc).__name__), now)
            self.journal.close_turn(key)

    async def _claim(self, key: str, record: Dict[str, Any], now: datetime) -> Optional[str]:
        """Re-check ownership, read the task, write the acknowledgement; None when it must not start."""
        intent: Dict[str, Any] = {}  # what an event will look like after our acknowledgement, before we write it
        try:
            problem = await self._run(self._recheck, record)
            if problem:
                logger.info("gemini-voice: ignored %s (%s): changed before it started", key, problem)
                self.journal.mark(key, "rejected", now, reason=problem)
                return None
            text, is_output = await self._run(self._read, record)
            if is_output:
                logger.info("gemini-voice: %s is an output of this plugin; not a task", key)
                self.journal.mark(key, "skipped", now, reason="plugin_output")
                return None
            ack = textutil.block(self._text["ack"].format(time=self._local(now)))
            _, locator = await self._run(self._write, record, ack, intent)
        except ApiError as exc:
            logger.warning("gemini-voice: cannot start %s: %s", key, exc.code)
            self.journal.mark(key, "failed", now, reason=exc.code)
            return None
        except (AuthError, TransientError) as exc:
            await self._unclaim(key, record, intent, exc, now)
            raise
        self.journal.mark(key, "claimed", now, locator=locator)
        return self._prompt(record, text)

    async def _unclaim(self, key: str, record: Dict[str, Any], intent: Dict[str, Any], exc: GoogleError,
                       now: datetime) -> None:
        """Google did not answer while the task was being claimed: it goes back to the queue (a broken token
        is not the task's fault and does not count as an attempt), or fails with a note after the last
        attempt. An acknowledgement whose response was lost may still have landed: what we meant to write is
        kept, so the ownership re-check can recognise it (see Calendar.recheck)."""
        if intent.get("fingerprint"):
            self.journal.mark(key, "claimed", now, locator={"suffix": intent["suffix"]},
                              ack_fingerprint=intent["fingerprint"])
        attempts = int(record.get("attempts") or 0)
        if isinstance(exc, AuthError):
            self.journal.mark(key, "queued", now, attempts=max(0, attempts - 1))
        elif attempts >= CLAIM_ATTEMPTS:
            logger.warning("gemini-voice: %s could not be started (%s); giving up", key, exc.code)
            await self._close_with_note(key, "failed", self._text["failed"].format(reason=exc.code), now)
        else:
            self.journal.mark(key, "queued", now)

    def _prompt(self, record: Dict[str, Any], text: str) -> str:
        created = from_iso(record.get("created"))
        when = f" · created {self._local(created)}" if created else ""
        kind = KIND_NAMES.get(record["kind"], "Google object")
        body = textutil.truncate(text.strip(), TASK_TEXT_LIMIT, "[task text shortened]")
        return f"[Voice task from the owner via the Gemini app · {kind}{when}]\n{body}"

    # ------------------------------------------------------------------------------- Google I/O
    def _recheck(self, record: Dict[str, Any]) -> str:
        if record["kind"] == "cal":
            # Our own acknowledgement moves ``updated``: it is forgiven only while the event's last change is
            # exactly the one our recorded write produced, or while the event is exactly what that write was
            # to make it (its response lost); never on the mere intention to write.
            return self.calendar.recheck(record["id"], (record.get("locator") or {}).get("updated"),
                                         record.get("ack_fingerprint"))
        return self.drive.recheck(record["id"])

    def _read(self, record: Dict[str, Any]) -> Tuple[str, bool]:
        """``(task text, whether the object is an output of this plugin)``."""
        kind, obj_id, title, locator = record["kind"], record["id"], record.get("title") or "", record.get("locator")
        if kind == "cal":
            return self.calendar.read(obj_id, locator), False
        if kind == "doc":
            body = self.docs.read_body(obj_id, locator)
            return f"{title}\n{body}".strip(), textutil.is_output(body)
        return self.sheets.read(obj_id, title, locator), False

    def _write(self, record: Dict[str, Any], block_text: str,
               intent: Optional[Dict[str, Any]] = None) -> Tuple[int, Locator]:
        kind, obj_id = record["kind"], record["id"]
        if kind == "cal":
            status, locator = self.calendar.write(obj_id, block_text, record.get("locator"), intent)
        else:
            writer = {"doc": self.docs.write, "sheet": self.sheets.write}[kind]
            status, locator = writer(obj_id, block_text, record.get("locator"))
        logger.info("gemini-voice: wrote to %s.%s HTTP %s", kind, obj_id, status)
        return status, locator

    async def _close_with_note(self, key: str, state: str, note: str, now: datetime) -> None:
        record = self.journal.get(key) or {}
        locator = record.get("locator")
        try:
            _, locator = await self._run(self._write, record, textutil.block(note))
        except GoogleError as exc:
            logger.warning("gemini-voice: could not write the %s note to %s: %s", state, key, exc.code)
        self.journal.mark(key, state, now, locator=locator)

    async def _write_answer(self, key: str, now: datetime, *, raise_unavailable: bool = False) -> bool:
        """Write the journaled answer into its object. False when Google refused it for good (the object
        gets a failure note if it can); during an outage it stays ``answered`` for the next poll, and the
        outage propagates when ``raise_unavailable``."""
        record = self.journal.get(key) or {}
        limit = ANSWER_LIMITS.get(record.get("kind", ""), 30000)
        body = textutil.truncate(record.get("answer") or "", limit, self._text["truncated"])
        header = self._text["reply"].format(time=self._local(now))
        try:
            status, locator = await self._run(self._write, record, textutil.block(header, body))
        except ApiError as exc:
            logger.warning("gemini-voice: reply to %s refused by Google: %s", key, exc.code)
            await self._close_with_note(key, "failed", self._text["failed"].format(reason=exc.code), now)
            return False
        except (AuthError, TransientError) as exc:
            logger.warning("gemini-voice: reply to %s not written yet (%s); will retry", key, exc.code)
            if raise_unavailable:
                raise
            return True
        self.journal.mark(key, "done", now, http=status, locator=locator)
        return True

    # -------------------------------------------------------------------------- gateway callbacks
    async def deliver_answer(self, reply_to: Optional[str], content: str) -> Optional[str]:
        """The final reply of a voice turn: journal first, then into the task object. None when it is in
        the journal (written, or written on a later poll after an outage); otherwise why it was refused."""
        async with self._lock:
            now = self._clock()
            self._ensure_recovered(now)
            key = self._route(reply_to)
            if key is None:
                logger.warning("gemini-voice: a reply for %s has no task to go to; refused", reply_to or "no anchor")
                return "no voice task takes this reply"
            self.journal.mark(key, "answered", now, answer=textutil.plain_text(content), reply_sha=_digest(content))
            if not await self._write_answer(key, now):
                return "Google refused the reply for its object; see the gemini-voice log"
            return None

    async def holds_reply(self, content: str) -> bool:
        """Whether ``content`` is a final reply this plugin already holds (in the journal or written): the
        gateway's delivery ledger re-sends a reply whose acknowledgement it lost, without its anchor."""
        digest = _digest(content)
        async with self._lock:
            return any(r.get("reply_sha") == digest and r.get("state") in ("answered", "done")
                       for r in self.journal.objects.values())

    def _route(self, reply_to: Optional[str]) -> Optional[str]:
        """The anchor decides. A reply without one goes only to the inherited task, while that task has no
        reply yet and no newer task has started; anything else could land in the wrong object."""
        if reply_to:
            return reply_to if self.journal.get(reply_to) is not None else None
        record = self.journal.get(self._inherited)
        return self._inherited if record is not None and record.get("state") in ("dispatched", "abandoned") else None

    async def on_turn_complete(self, message_id: Optional[str], outcome: str, streamed: str = "") -> None:
        """End of a gateway turn: close a task that ended without a reply; free the queue. A turn without
        a message id is the gateway resuming the inherited task's turn."""
        async with self._lock:
            now = self._clock()
            self._ensure_recovered(now)
            key = message_id or self._inherited
            record = self.journal.get(key)
            if record is None or outcome == "cancelled":
                return  # a cancelled turn may be resumed by the gateway; the answer timeout still applies
            if not message_id:
                self._inherited = None  # the resumed turn is over
            if record.get("state") == "dispatched":
                if outcome == "success" and streamed.strip():
                    self.journal.mark(key, "answered", now, answer=textutil.plain_text(streamed))
                    await self._write_answer(key, now)
                elif outcome == "success":
                    note = self._text["done_silent"].format(time=self._local(now))
                    await self._close_with_note(key, "done", note, now)
                else:
                    await self._close_with_note(key, "failed", self._text["failed"].format(reason="turn error"), now)
            self.journal.close_turn(key)

    async def deliver_output(self, target: str, content: str) -> Tuple[bool, str]:
        """Cron delivery: ``doc`` creates a document, ``event-HHMM`` creates or updates a slot event."""
        slot = _SLOT.match(target or "")
        if target not in ("doc", "") and not slot:
            return False, f"unknown target {target!r}: use 'doc' or 'event-HHMM'"
        async with self._lock:
            now = self._clock()
            title, body = split_title(textutil.plain_text(content), f"Digest {self._local(now)}")
            try:
                if slot:
                    event_id, status = await self._run(self.calendar_output.upsert, slot.group(1) + slot.group(2),
                                                       title, body, self._zone, self._zone_name, now)
                    logger.info("gemini-voice: delivered to cal.%s HTTP %s", event_id, status)
                    return True, f"cal.{event_id}"
                # The signature is in the title too: if filling fails, even an empty output document is
                # never read back as a task, with or without the journal (cron outside the gateway).
                doc_id = await self._run(self.docs.create_blank, f"{textutil.OUTPUT_MARKER} {title}")
                self.journal.add_outbound(doc_id, now)  # recorded before any text exists in it
                status = await self._run(self.docs.fill_output, doc_id, title, body)
                logger.info("gemini-voice: delivered to new doc.%s HTTP %s", doc_id, status)
                return True, f"doc.{doc_id}"
            except GoogleError as exc:
                logger.warning("gemini-voice: delivery to %s failed: %s", target, exc.code)
                return False, exc.code

    # ------------------------------------------------------------------------------------ status
    def _set_status(self, state: str, code: str, message: str) -> None:
        if self._status == (state, code):
            return
        self._status = (state, code)
        try:  # the gateway status comes first; a failing state file must not hide it
            self._publish(state, code, message)
        except Exception:
            logger.warning("gemini-voice: could not publish platform status %s/%s", state, code, exc_info=True)
        try:
            self.journal.set_status(state, code, message, self._clock())
        except Exception:
            logger.warning("gemini-voice: could not save the status to the state file", exc_info=True)

    def _on_success(self, problems: List[GoogleError]) -> None:
        if self._auth_failed_at is not None or self._backoff:
            logger.info("gemini-voice: Google reachable and authorized again")
        self._auth_failed_at, self._backoff = None, 0.0
        if not problems:
            self._set_status("connected", "", "")
            return
        code = problems[0].code
        if isinstance(problems[0], TransientError):
            code, message = "google_unreachable", (f"Google did not answer a polling request ({problems[0].code}); "
                                                   "the other sources keep working.")
        elif code == "api_disabled":
            message = "A Google API is disabled: enable Calendar, Drive, Docs and Sheets in the OAuth client's project."
        else:
            message = f"Google refused a polling request ({code}); the other sources keep working."
        if self._status != ("retrying", code):
            logger.warning("gemini-voice: %s", message)
        self._set_status("retrying", code, message)

    def _on_auth_error(self, exc: AuthError, now: datetime) -> float:
        message = (f"Google authorization failed ({exc.code}); polling paused. Run `hermes gemini-voice auth` "
                   "on the server, then restart the gateway.")
        if self._auth_failed_at is None:
            logger.error("gemini-voice: %s", message)
        self._auth_failed_at = now
        self._set_status("retrying", "auth_failed", message)
        return float(self.settings.poll_seconds)

    def _on_transient(self, exc: TransientError) -> float:
        first = not self._backoff
        doubled = self._backoff * 2 if self._backoff else float(self.settings.poll_seconds) * 2
        self._backoff = min(MAX_BACKOFF, doubled)
        if first:
            logger.warning("gemini-voice: Google unreachable (%s); retrying with backoff", exc.code)
        self._set_status("retrying", "google_unreachable", f"Google API unreachable ({exc.code}); retrying.")
        return self._backoff

    def _local(self, moment: Optional[datetime]) -> str:
        return textutil.time_label((moment or self._clock()).astimezone(self._zone))
