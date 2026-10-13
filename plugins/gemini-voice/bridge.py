"""Engine-independent core: poll Google, keep the journal, hand tasks over one at a time, write replies.

The Hermes adapter (``adapter.py``) wires this to the gateway; everything here is testable without
Hermes and without network access.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import textutil
from .google_api import ApiError, AuthError, GoogleClient, GoogleError, TransientError
from .journal import Journal, Store, from_iso
from .sources import Calendar, CalendarOutput, Docs, Drive, Sheets, Verdict

logger = logging.getLogger("gemini_voice")

PLATFORM = "gemini_voice"
OWNER_CHAT = "owner"
SOURCES = ("calendar", "docs", "sheets")
FOREIGN_REASONS = {"not_creator", "not_organizer", "has_attendees", "event_type",
                   "not_owned", "shared", "modified_by_other"}
TASK_TEXT_LIMIT = 8000
ANSWER_LIMITS = {"cal": 7000, "doc": 30000, "sheet": 30000}
KIND_NAMES = {"cal": "Google Calendar event", "doc": "Google Doc", "sheet": "Google Sheet"}
AUTH_RECHECK = timedelta(minutes=15)
CALENDAR_SKEW = timedelta(minutes=10)
TURN_GRACE = timedelta(minutes=2)
MAX_BACKOFF = 900.0
_SLOT = re.compile(r"^event-([01]\d|2[0-3])([0-5]\d)$")


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
                 run_blocking: Optional[Callable[..., Awaitable[Any]]] = None):
        self.settings = settings
        self.journal = Journal(store)
        self.calendar, self.drive = Calendar(client), Drive(client)
        self.docs, self.sheets, self.calendar_output = Docs(client), Sheets(client), CalendarOutput(client)
        self._dispatch, self._publish = dispatch, publish_status
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._run = run_blocking or asyncio.to_thread
        self._patterns = textutil.compile_labels(settings.labels)
        self._text = textutil.strings(settings.language)
        self._zone, self._zone_name = resolve_zone(settings.timezone)
        self._lock = asyncio.Lock()
        self._auth_failed_at: Optional[datetime] = None
        self._backoff = 0.0
        self._status: Optional[Tuple[str, str]] = None
        self._logged_rejects: set = set()

    # ------------------------------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        self.recover(self._clock())
        while True:
            try:
                delay = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gemini-voice: poll cycle failed")
                delay = float(self.settings.poll_seconds * 2)
            await asyncio.sleep(delay)

    def recover(self, now: datetime) -> None:
        """After a restart: claimed (never dispatched) tasks go back to the queue; dispatched ones stay."""
        for key in self.journal.with_state("claimed"):
            self.journal.mark(key, "queued", now)
            logger.info("gemini-voice: %s was claimed but not dispatched before the restart; requeued", key)

    async def tick(self) -> float:
        """One poll cycle; returns seconds until the next one."""
        now = self._clock()
        if self._auth_failed_at and now - self._auth_failed_at < AUTH_RECHECK:
            return float(self.settings.poll_seconds)
        try:
            async with self._lock:
                await self._poll(now)
                self._on_success()
                await self._advance(now)
        except AuthError as exc:
            return self._on_auth_error(exc, now)
        except TransientError as exc:
            return self._on_transient(exc)
        except ApiError as exc:
            return self._on_api_error(exc)
        return float(self.settings.poll_seconds)

    # ---------------------------------------------------------------------------------- polling
    async def _poll(self, now: datetime) -> None:
        since = self.journal.since(now)
        not_before = max(since, now - timedelta(hours=self.settings.max_age_hours))
        verdicts: List[Verdict] = []
        if "calendar" in self.settings.sources:
            updated_min = max(not_before, self.journal.calendar_updated_min(not_before) - CALENDAR_SKEW)
            verdicts += await self._run(self.calendar.candidates, self._patterns, not_before, updated_min)
        kinds = [k for k, s in (("doc", "docs"), ("sheet", "sheets")) if s in self.settings.sources]
        if kinds:
            verdicts += await self._run(self.drive.candidates, self._patterns, not_before, kinds,
                                        self.journal.outbound_ids())
        for verdict in verdicts:
            self._consider(verdict, now)
        self.journal.poll_ok(now, now)

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
                self._logged_rejects.add(obj.key)
                logger.info("gemini-voice: ignored %s (%s): not created by the account owner",
                            obj.key, verdict.reason)

    # --------------------------------------------------------------------------------- advancing
    async def _advance(self, now: datetime) -> None:
        await self._retry_answers(now)
        await self._expire(now)
        if not self._turn_busy(now):
            key = self.journal.next_queued()
            if key:
                await self._start(key, now)
        self.journal.prune(now)

    async def _retry_answers(self, now: datetime) -> None:
        for key in self.journal.with_state("answered"):
            await self._write_answer(key, now)

    async def _expire(self, now: datetime) -> None:
        limit = timedelta(minutes=self.settings.answer_timeout_minutes)
        for key in self.journal.with_state("dispatched"):
            record = self.journal.get(key) or {}
            started = _parse(record.get("dispatched_at")) or now
            if now - started > limit:
                logger.warning("gemini-voice: no reply for %s within %s; closing it", key, limit)
                await self._close_with_note(key, "abandoned", self._text["abandoned"], now)
                self.journal.close_turn(key)

    def _turn_busy(self, now: datetime) -> bool:
        key = self.journal.open_turn()
        record = self.journal.get(key)
        if record is None:
            return False
        if record.get("state") == "dispatched":
            return True
        finished = _parse(record.get(f"{record.get('state')}_at")) or now
        if now - finished > TURN_GRACE:
            self.journal.close_turn(key)  # the gateway never reported the end of the turn
            return False
        return True

    async def _start(self, key: str, now: datetime) -> None:
        record = self.journal.get(key) or {}
        day = now.astimezone(self._zone).strftime("%Y-%m-%d")
        if self.journal.count_today(day) >= self.settings.max_tasks_per_day:
            note = self._text["daily_cap"].format(limit=self.settings.max_tasks_per_day)
            await self._close_with_note(key, "skipped", note, now)
            return
        record = self.journal.mark(key, "claimed", now, attempts=int(record.get("attempts") or 0) + 1)
        prompt = await self._claim(key, record, now)
        if prompt is None:
            return
        self.journal.count_task(day)
        self.journal.mark(key, "dispatched", now, turn_open=True)
        logger.info("gemini-voice: dispatched %s", key)
        try:
            await self._dispatch(Task(key, record["kind"], record.get("title") or "", prompt))
        except Exception as exc:
            logger.exception("gemini-voice: gateway refused %s", key)
            await self._close_with_note(key, "failed", self._text["failed"].format(reason=type(exc).__name__), now)
            self.journal.close_turn(key)

    async def _claim(self, key: str, record: Dict[str, Any], now: datetime) -> Optional[str]:
        """Read the task text and write the acknowledgement; None when the task cannot start now."""
        try:
            text = await self._run(self._read, record)
            ack = textutil.block(self._text["ack"].format(time=self._local(now)))
            await self._run(self._write, record, ack)
        except ApiError as exc:
            logger.warning("gemini-voice: cannot start %s: %s", key, exc.code)
            self.journal.mark(key, "failed", now, reason=exc.code)
            return None
        except (AuthError, TransientError):
            state = "failed" if int(record.get("attempts") or 0) >= 3 else "queued"
            self.journal.mark(key, state, now)
            raise
        return self._prompt(record, text)

    def _prompt(self, record: Dict[str, Any], text: str) -> str:
        created = _parse(record.get("created"))
        when = f" · created {self._local(created)}" if created else ""
        header = f"[Voice task from the owner via the Gemini app · {KIND_NAMES.get(record['kind'], 'Google object')}{when}]"
        body = textutil.truncate(text.strip(), TASK_TEXT_LIMIT, "[task text shortened]")
        return f"{header}\n{body}"

    # ------------------------------------------------------------------------------- Google I/O
    def _read(self, record: Dict[str, Any]) -> str:
        kind, obj_id, title = record["kind"], record["id"], record.get("title") or ""
        if kind == "cal":
            return self.calendar.read(obj_id)
        if kind == "doc":
            return self.docs.read(obj_id, title)
        return self.sheets.read(obj_id, title)

    def _write(self, record: Dict[str, Any], block_text: str) -> int:
        kind, obj_id = record["kind"], record["id"]
        writer = {"cal": self.calendar.write, "doc": self.docs.write, "sheet": self.sheets.write}[kind]
        status = writer(obj_id, block_text)
        logger.info("gemini-voice: wrote to %s.%s HTTP %s", kind, obj_id, status)
        return status

    async def _close_with_note(self, key: str, state: str, note: str, now: datetime) -> None:
        record = self.journal.get(key) or {}
        try:
            await self._run(self._write, record, textutil.block(note))
        except GoogleError as exc:
            logger.warning("gemini-voice: could not write the %s note to %s: %s", state, key, exc.code)
        self.journal.mark(key, state, now)

    async def _write_answer(self, key: str, now: datetime) -> None:
        record = self.journal.get(key) or {}
        limit = ANSWER_LIMITS.get(record.get("kind", ""), 30000)
        body = textutil.truncate(record.get("answer") or "", limit, self._text["truncated"])
        header = self._text["reply"].format(time=self._local(now))
        try:
            status = await self._run(self._write, record, textutil.block(header, body))
        except ApiError as exc:
            logger.warning("gemini-voice: reply to %s refused by Google: %s", key, exc.code)
            self.journal.mark(key, "failed", now, reason=exc.code)
            return
        except (AuthError, TransientError) as exc:
            logger.warning("gemini-voice: reply to %s not written yet (%s); will retry", key, exc.code)
            return
        self.journal.mark(key, "done", now, http=status)

    # -------------------------------------------------------------------------- gateway callbacks
    async def deliver_answer(self, reply_to: Optional[str], content: str) -> bool:
        """The final reply of a voice turn: journal first, then into the task object."""
        async with self._lock:
            now = self._clock()
            key = self._route(reply_to)
            if key is None:
                logger.warning("gemini-voice: a reply arrived with no open task; dropped")
                return False
            self.journal.mark(key, "answered", now, answer=textutil.plain_text(content))
            await self._write_answer(key, now)
            return True

    def _route(self, reply_to: Optional[str]) -> Optional[str]:
        for key in (reply_to, self.journal.open_turn()):
            record = self.journal.get(key)
            if record is not None and record.get("state") in ("dispatched", "answered"):
                return key
        return None

    async def on_turn_complete(self, message_id: Optional[str], outcome: str, streamed: str = "") -> None:
        """End of a gateway turn: close tasks that ended without a reply; free the queue."""
        async with self._lock:
            now = self._clock()
            key = message_id if self.journal.get(message_id) else self.journal.open_turn()
            record = self.journal.get(key)
            if record is None or outcome == "cancelled":
                return  # a cancelled turn may be resumed by the gateway; the answer timeout still applies
            if record.get("state") == "dispatched":
                if outcome == "success" and streamed.strip():
                    self.journal.mark(key, "answered", now, answer=textutil.plain_text(streamed))
                    await self._write_answer(key, now)
                elif outcome == "success":
                    await self._close_with_note(key, "done", self._text["done_silent"].format(time=self._local(now)), now)
                else:
                    await self._close_with_note(key, "failed", self._text["failed"].format(reason="turn error"), now)
            self.journal.close_turn(key)

    async def deliver_output(self, target: str, content: str) -> Tuple[bool, str]:
        """Cron delivery: ``doc`` creates a document, ``event-HHMM`` creates or updates a slot event."""
        now = self._clock()
        title, body = split_title(textutil.plain_text(content), f"Hermes {self._local(now)}")
        slot = _SLOT.match(target or "")
        try:
            if target in ("doc", ""):
                doc_id, status = await self._run(self.docs.create, title, body)
                self.journal.add_outbound(doc_id, now)
                logger.info("gemini-voice: delivered to new doc.%s HTTP %s", doc_id, status)
                return True, f"doc.{doc_id}"
            if slot:
                event_id, status = await self._run(self.calendar_output.upsert, slot.group(1) + slot.group(2),
                                                   title, body, self._zone, self._zone_name, now)
                logger.info("gemini-voice: delivered to cal.%s HTTP %s", event_id, status)
                return True, f"cal.{event_id}"
        except GoogleError as exc:
            logger.warning("gemini-voice: delivery to %s failed: %s", target, exc.code)
            return False, exc.code
        return False, f"unknown target {target!r}: use 'doc' or 'event-HHMM'"

    # ------------------------------------------------------------------------------------ status
    def _set_status(self, state: str, code: str, message: str) -> None:
        if self._status == (state, code):
            return
        self._status = (state, code)
        self.journal.set_status(state, code, message, self._clock())
        try:
            self._publish(state, code, message)
        except Exception:
            logger.warning("gemini-voice: could not publish platform status %s/%s", state, code, exc_info=True)

    def _on_success(self) -> None:
        if self._auth_failed_at is not None or self._backoff:
            logger.info("gemini-voice: Google reachable and authorized again")
        self._auth_failed_at, self._backoff = None, 0.0
        self._set_status("connected", "", "")

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

    def _on_api_error(self, exc: ApiError) -> float:
        message = (f"Google refused a polling request ({exc.code}). Check that the Calendar, Drive, Docs and "
                   "Sheets APIs are enabled in the OAuth client's Cloud project.")
        logger.warning("gemini-voice: %s", message)
        self._set_status("retrying", exc.code, message)
        return float(self.settings.poll_seconds * 5)

    def _local(self, moment: Optional[datetime]) -> str:
        return textutil.time_label((moment or self._clock()).astimezone(self._zone))


_parse = from_iso
