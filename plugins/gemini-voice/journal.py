"""Durable per-object journal: every task object passes through these states at most once.

    queued -> claimed -> dispatched -> answered -> done
                 |            |-> abandoned (no answer within the timeout)
                 |            |-> failed (the turn errored)
                 |-> failed (could not read or acknowledge it)
    queued -> skipped (daily limit)

``dispatched`` is written to disk *before* the task is handed to the gateway, and a dispatched task is
never handed over again. A restart therefore cannot run a task twice: the gateway either finishes the
interrupted turn itself, or the task is closed as ``abandoned`` with a note in the object.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol

STORE_KEY = "journal"
VERSION = 1
FINAL_STATES = {"done", "abandoned", "failed", "skipped"}
KEEP_DAYS = 30


class Store(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


def iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def from_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class Journal:
    """In-memory view of the journal, saved to the store after every change."""

    def __init__(self, store: Store):
        self._store = store
        data = store.get(STORE_KEY, None)
        self.data: Dict[str, Any] = data if isinstance(data, dict) and data.get("version") == VERSION else {}
        self.data.setdefault("version", VERSION)
        for key, empty in (("objects", {}), ("outbound", {}), ("daily", {}), ("status", {})):
            if not isinstance(self.data.get(key), type(empty)):
                self.data[key] = empty

    def save(self) -> None:
        self._store.set(STORE_KEY, self.data)

    # -- watermarks -------------------------------------------------------------------------------
    def since(self, now: datetime) -> datetime:
        """When the plugin first ran for this profile; objects created earlier are never tasks."""
        value = from_iso(self.data.get("since"))
        if value is None:
            value = now
            self.data["since"] = iso(now)
            self.save()
        return value

    def calendar_updated_min(self, default: datetime) -> datetime:
        return from_iso(self.data.get("cal_updated_min")) or default

    def poll_ok(self, now: datetime, calendar_mark: datetime) -> None:
        self.data["last_poll_ok"] = iso(now)
        self.data["cal_updated_min"] = iso(calendar_mark)
        self.save()

    # -- objects ----------------------------------------------------------------------------------
    @property
    def objects(self) -> Dict[str, Dict[str, Any]]:
        return self.data["objects"]

    def get(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.objects.get(key) if key else None

    def queue(self, key: str, kind: str, obj_id: str, title: str, created: datetime, now: datetime) -> None:
        self.objects[key] = {"kind": kind, "id": obj_id, "title": title[:200], "created": iso(created),
                             "state": "queued", "queued_at": iso(now), "attempts": 0}
        self.save()

    def mark(self, key: str, state: str, now: datetime, **fields: Any) -> Dict[str, Any]:
        record = self.objects[key]
        record.update(fields)
        record["state"] = state
        record[f"{state}_at"] = iso(now)
        if state in FINAL_STATES:
            record.pop("answer", None)
        self.save()
        return record

    def next_queued(self) -> Optional[str]:
        queued = [(r.get("created") or "", k) for k, r in self.objects.items() if r.get("state") == "queued"]
        return min(queued)[1] if queued else None

    def with_state(self, state: str) -> List[str]:
        return [k for k, r in self.objects.items() if r.get("state") == state]

    def open_turn(self) -> Optional[str]:
        """The task whose gateway turn may still be running (at most one at a time)."""
        open_keys = [k for k, r in self.objects.items() if r.get("turn_open")]
        return open_keys[0] if open_keys else None

    def close_turn(self, key: str) -> None:
        record = self.objects.get(key)
        if record is not None and record.get("turn_open"):
            record["turn_open"] = False
            self.save()

    # -- limits, outbound, housekeeping -----------------------------------------------------------
    def count_today(self, day: str) -> int:
        daily = self.data["daily"]
        return int(daily.get("count") or 0) if daily.get("date") == day else 0

    def count_task(self, day: str) -> None:
        self.data["daily"] = {"date": day, "count": self.count_today(day) + 1}
        self.save()

    def outbound_ids(self) -> List[str]:
        return list(self.data["outbound"])

    def add_outbound(self, obj_id: str, now: datetime) -> None:
        self.data["outbound"][obj_id] = iso(now)
        self.save()

    def set_status(self, state: str, code: str, message: str, now: datetime) -> None:
        self.data["status"] = {"state": state, "code": code, "message": message, "at": iso(now)}
        self.save()

    def prune(self, now: datetime) -> int:
        """Forget final records and outbound ids older than ``KEEP_DAYS``; returns how many went."""
        horizon = now - timedelta(days=KEEP_DAYS)
        stale = [k for k, r in self.objects.items()
                 if r.get("state") in FINAL_STATES and (from_iso(r.get("queued_at")) or now) < horizon]
        for key in stale:
            del self.objects[key]
        old_out = [k for k, v in self.data["outbound"].items() if (from_iso(v) or now) < horizon]
        for key in old_out:
            del self.data["outbound"][key]
        if stale or old_out:
            self.save()
        return len(stale) + len(old_out)
