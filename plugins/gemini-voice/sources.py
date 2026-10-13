"""Google Calendar, Docs and Sheets: find owner-created task objects, read their text, write replies.

Ownership is decided only from fields the Google API returns for the authorized account. Anything
created by someone else (an invitation, a shared file, an event another person put on a shared
calendar) is rejected here and never reaches Hermes, whatever its title says.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import textutil
from .google_api import ApiError, GoogleClient

CAL_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
OUTPUT_PROPERTY = "gemini_voice"
MAX_PAGES = 5
SHEET_READ_RANGE = "A1:Z500"
SHEET_CELL_LIMIT = 1000
SHEET_MAX_ROWS = 300
_HTML_HINT = re.compile(r"<(br|p|div|b|i|u|a|ul|ol|li|span)\b|</\w+>", re.IGNORECASE)
_HTML_BREAK = re.compile(r"<\s*br\s*/?\s*>|</\s*(p|div|li)\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_TRAILING_BREAKS = re.compile(r"(\s*<\s*br\s*/?\s*>\s*)+$", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceObject:
    """A candidate task object: ``kind`` is ``cal``, ``doc`` or ``sheet``."""
    kind: str
    id: str
    title: str
    created: datetime

    @property
    def key(self) -> str:
        return f"{self.kind}.{self.id}"


@dataclass(frozen=True)
class Verdict:
    obj: VoiceObject
    accepted: bool
    reason: str = ""


def parse_time(value: Any) -> Optional[datetime]:
    """RFC 3339 timestamp from Google as an aware UTC datetime; None when absent or malformed."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paged(client: GoogleClient, url: str, params: Dict[str, Any], items_key: str) -> List[dict]:
    items: List[dict] = []
    page_params = dict(params)
    for _ in range(MAX_PAGES):
        body = client.request("GET", url, params=page_params).body
        items.extend(x for x in body.get(items_key) or [] if isinstance(x, dict))
        token = body.get("nextPageToken")
        if not token:
            break
        page_params["pageToken"] = token
    return items


def html_to_text(value: str) -> str:
    """Calendar descriptions may be HTML; turn them into plain lines."""
    if not _HTML_HINT.search(value or ""):
        return value or ""
    text = _HTML_BREAK.sub("\n", value)
    return html.unescape(_HTML_TAG.sub("", text))


# ---------------------------------------------------------------------------------------- Calendar

def check_event(event: dict, patterns: Sequence[re.Pattern], not_before: datetime) -> Verdict:
    """Accept only a plain, live event the owner created and organizes alone, with a label in its title."""
    obj = VoiceObject("cal", str(event.get("id") or ""), str(event.get("summary") or ""),
                      parse_time(event.get("created")) or datetime.min.replace(tzinfo=timezone.utc))
    private = ((event.get("extendedProperties") or {}).get("private") or {})
    checks = (
        (OUTPUT_PROPERTY in private, "plugin_output"),
        (event.get("status") == "cancelled", "cancelled"),
        ((event.get("eventType") or "default") != "default", "event_type"),
        (not (event.get("creator") or {}).get("self"), "not_creator"),
        (not (event.get("organizer") or {}).get("self"), "not_organizer"),
        (any(not a.get("self") for a in event.get("attendees") or [] if isinstance(a, dict)), "has_attendees"),
        (obj.created < not_before, "too_old"),
        (not textutil.has_label(obj.title, patterns), "no_label"),
        (textutil.has_tag(event.get("description")), "already_handled"),
    )
    for failed, reason in checks:
        if failed:
            return Verdict(obj, False, reason)
    return Verdict(obj, bool(obj.id), "" if obj.id else "no_id")


class Calendar:
    """Owner's primary calendar: task events in, reply in the event description."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def candidates(self, patterns: Sequence[re.Pattern], not_before: datetime,
                   updated_min: datetime) -> List[Verdict]:
        params = {"updatedMin": rfc3339(updated_min), "singleEvents": "true", "showDeleted": "false",
                  "maxResults": 100, "orderBy": "updated"}
        return [check_event(e, patterns, not_before) for e in _paged(self.client, CAL_EVENTS, params, "items")]

    def _get(self, event_id: str) -> dict:
        return self.client.request("GET", f"{CAL_EVENTS}/{urllib.parse.quote(event_id, safe='')}").body

    def read(self, event_id: str) -> str:
        event = self._get(event_id)
        start = (event.get("start") or {})
        when = start.get("dateTime") or start.get("date") or ""
        description = html_to_text(textutil.cut_block(event.get("description")))
        parts = [str(event.get("summary") or ""), f"When: {when}" if when else "", description.strip()]
        return "\n".join(p for p in parts if p)

    def write(self, event_id: str, block_text: str) -> int:
        """Replace our trailing block in the description (etag-guarded; retried on a concurrent edit)."""
        for _ in range(3):
            event = self._get(event_id)
            description = merge_description(event.get("description") or "", block_text)
            headers = {"If-Match": str(event.get("etag") or "*")}
            try:
                return self.client.request(
                    "PATCH", f"{CAL_EVENTS}/{urllib.parse.quote(event_id, safe='')}",
                    params={"sendUpdates": "none"}, body={"description": description}, headers=headers).status
            except ApiError as exc:
                if exc.status != 412:
                    raise
        raise ApiError("conflict", "event kept changing while writing", 412)


def merge_description(existing: str, block_text: str) -> str:
    """Owner's description plus our block. In HTML descriptions every block line still starts on its own
    text line (``<br>`` then a newline), so the marker stays at a line start and the block is replaced,
    not stacked, on the next write."""
    base = textutil.cut_block(existing)
    if _HTML_HINT.search(base):
        base = _TRAILING_BREAKS.sub("", base)
        rendered = "<br>\n".join(html.escape(line, quote=False) for line in block_text.split("\n"))
        return f"{base}<br><br>\n{rendered}" if base else rendered
    return f"{base}\n\n{block_text}" if base else block_text


# ------------------------------------------------------------------------------------------- Drive

def check_file(item: dict, patterns: Sequence[re.Pattern], not_before: datetime,
               outbound: Sequence[str]) -> Verdict:
    """Accept only an unshared Doc or Sheet the owner owns and last modified, with a label in its name."""
    kind = {DOC_MIME: "doc", SHEET_MIME: "sheet"}.get(str(item.get("mimeType")), "")
    obj = VoiceObject(kind or "file", str(item.get("id") or ""), str(item.get("name") or ""),
                      parse_time(item.get("createdTime")) or datetime.min.replace(tzinfo=timezone.utc))
    checks = (
        (not kind, "mime"),
        (obj.id in outbound, "plugin_output"),
        (item.get("ownedByMe") is not True, "not_owned"),
        (item.get("shared") is not False, "shared"),
        (not (item.get("lastModifyingUser") or {}).get("me"), "modified_by_other"),
        (obj.created < not_before, "too_old"),
        (not textutil.has_label(obj.title, patterns), "no_label"),
    )
    for failed, reason in checks:
        if failed:
            return Verdict(obj, False, reason)
    return Verdict(obj, bool(obj.id), "" if obj.id else "no_id")


class Drive:
    """Metadata-only listing of recently created Docs and Sheets owned by the account."""

    FIELDS = ("nextPageToken,files(id,name,mimeType,createdTime,ownedByMe,shared,"
              "lastModifyingUser(me),owners(me))")

    def __init__(self, client: GoogleClient):
        self.client = client

    def candidates(self, patterns: Sequence[re.Pattern], not_before: datetime, kinds: Sequence[str],
                   outbound: Sequence[str]) -> List[Verdict]:
        mimes = [m for m, k in ((DOC_MIME, "doc"), (SHEET_MIME, "sheet")) if k in kinds]
        if not mimes:
            return []
        mime_q = " or ".join(f"mimeType='{m}'" for m in mimes)
        created = not_before.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        params = {"q": f"({mime_q}) and 'me' in owners and trashed=false and createdTime > '{created}'",
                  "fields": self.FIELDS, "orderBy": "createdTime", "pageSize": 100,
                  "spaces": "drive", "corpora": "user"}
        files = _paged(self.client, DRIVE_FILES, params, "files")
        return [check_file(f, patterns, not_before, outbound) for f in files]


# -------------------------------------------------------------------------------------------- Docs

def _paragraphs(content: Sequence[dict]) -> List[Tuple[int, str]]:
    """``(startIndex, text)`` for every paragraph, tables and tables of contents included."""
    found: List[Tuple[int, str]] = []
    for element in content or []:
        if "paragraph" in element:
            runs = element["paragraph"].get("elements") or []
            text = "".join((r.get("textRun") or {}).get("content", "") for r in runs)
            found.append((int(element.get("startIndex") or 0), text))
        elif "table" in element:
            for row in element["table"].get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    found.extend(_paragraphs(cell.get("content") or []))
        elif "tableOfContents" in element:
            found.extend(_paragraphs(element["tableOfContents"].get("content") or []))
    return found


class Docs:
    """Task text from the document body; the reply replaces our block at the end of the body."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def _get(self, doc_id: str) -> dict:
        return self.client.request("GET", f"{DOCS}/{urllib.parse.quote(doc_id, safe='')}").body

    def read(self, doc_id: str, title: str) -> str:
        paragraphs = _paragraphs((self._get(doc_id).get("body") or {}).get("content") or [])
        body = textutil.cut_block("".join(text for _, text in paragraphs))
        return f"{title}\n{body.strip()}".strip()

    def write(self, doc_id: str, block_text: str) -> int:
        doc = self._get(doc_id)
        try:
            return self._batch(doc_id, self._requests(doc, block_text), doc.get("revisionId"))
        except ApiError as exc:
            if exc.status != 400:
                raise
        doc = self._get(doc_id)  # the document changed between read and write: once more, fresh
        return self._batch(doc_id, self._requests(doc, block_text), doc.get("revisionId"))

    @staticmethod
    def _requests(doc: dict, block_text: str) -> List[dict]:
        content = (doc.get("body") or {}).get("content") or []
        end = int(content[-1].get("endIndex") or 1) if content else 1
        tagged = [start for start, text in _paragraphs(content) if textutil.is_marker_line(text)]
        if tagged and tagged[-1] < end - 1:
            start = tagged[-1]
            return [{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end - 1}}},
                    {"insertText": {"location": {"index": start}, "text": block_text}}]
        has_text = any(text.strip() for _, text in _paragraphs(content))
        text = ("\n" + block_text) if has_text else block_text
        return [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]

    def _batch(self, doc_id: str, requests: List[dict], revision: Optional[str]) -> int:
        body: Dict[str, Any] = {"requests": requests}
        if revision:
            body["writeControl"] = {"requiredRevisionId": revision}
        url = f"{DOCS}/{urllib.parse.quote(doc_id, safe='')}:batchUpdate"
        return self.client.request("POST", url, body=body).status

    def create(self, title: str, text: str) -> Tuple[str, int]:
        """New document owned by the account; returns ``(document id, HTTP status of the text write)``."""
        doc_id = str(self.client.request("POST", DOCS, body={"title": title}).body.get("documentId") or "")
        if not doc_id:
            raise ApiError("create_failed", "no documentId in response")
        if not text.strip():
            return doc_id, 200
        body = {"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}
        url = f"{DOCS}/{urllib.parse.quote(doc_id, safe='')}:batchUpdate"
        return doc_id, self.client.request("POST", url, body=body).status


# ------------------------------------------------------------------------------------------ Sheets

def quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def last_marker_row(rows: List[List[str]]) -> Optional[int]:
    """Index of the row where our block starts (column A holds the marker line), or None."""
    marked = [i for i, row in enumerate(rows) if row and textutil.is_marker_line(row[0])]
    return marked[-1] if marked else None


def sheet_lines(block_text: str) -> List[str]:
    """Block text as cell-sized lines (long paragraphs wrapped), capped at ``SHEET_MAX_ROWS``."""
    lines: List[str] = []
    for paragraph in block_text.split("\n"):
        while len(paragraph) > SHEET_CELL_LIMIT:
            cut = paragraph.rfind(" ", 0, SHEET_CELL_LIMIT)
            cut = cut if cut > SHEET_CELL_LIMIT // 2 else SHEET_CELL_LIMIT
            lines.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        lines.append(paragraph)
    return lines[:SHEET_MAX_ROWS]


class Sheets:
    """Task text from the first sheet; the reply goes into column A below the owner's rows."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def _first_title(self, sheet_id: str) -> str:
        url = f"{SHEETS}/{urllib.parse.quote(sheet_id, safe='')}"
        body = self.client.request("GET", url, params={"fields": "sheets.properties(sheetId,title,index)"}).body
        sheets = [s.get("properties") or {} for s in body.get("sheets") or []]
        sheets.sort(key=lambda p: int(p.get("index") or 0))
        if not sheets or not sheets[0].get("title"):
            raise ApiError("no_sheet", "spreadsheet has no sheets")
        return str(sheets[0]["title"])

    def _rows(self, sheet_id: str, title: str) -> List[List[str]]:
        rng = urllib.parse.quote(f"{quote_sheet(title)}!{SHEET_READ_RANGE}", safe="")
        url = f"{SHEETS}/{urllib.parse.quote(sheet_id, safe='')}/values/{rng}"
        body = self.client.request("GET", url, params={"majorDimension": "ROWS"}).body
        return [[str(c) for c in row] for row in body.get("values") or []]

    def read(self, sheet_id: str, title: str) -> str:
        rows = self._rows(sheet_id, self._first_title(sheet_id))
        mine = last_marker_row(rows)
        lines = [", ".join(c for c in row if c.strip()) for row in rows[:mine]]
        return f"{title}\n" + "\n".join(line for line in lines if line)

    def write(self, sheet_id: str, block_text: str) -> int:
        """Clear our previous block (if any), then write the new one from that row, as RAW strings."""
        title = self._first_title(sheet_id)
        rows = self._rows(sheet_id, title)
        tagged = last_marker_row(rows)
        start = (tagged if tagged is not None else len(rows)) + 1
        base = f"{SHEETS}/{urllib.parse.quote(sheet_id, safe='')}/values"
        if tagged is not None:
            old = urllib.parse.quote(f"{quote_sheet(title)}!A{start}:Z{max(start, len(rows))}", safe="")
            self.client.request("POST", f"{base}/{old}:clear", body={})
        lines = sheet_lines(block_text)
        target = f"{quote_sheet(title)}!A{start}:A{start + len(lines) - 1}"
        body = {"range": target, "majorDimension": "ROWS", "values": [[line] for line in lines]}
        url = f"{base}/{urllib.parse.quote(target, safe='')}"
        return self.client.request("PUT", url, params={"valueInputOption": "RAW"}, body=body).status


# ------------------------------------------------------------------------------------ Output events

def slot_start(slot: str, zone: Any, now: datetime) -> datetime:
    """Today's ``HHMM`` slot in ``zone``; tomorrow's once today's slot is more than 6 hours past."""
    hour, minute = int(slot[:2]), int(slot[2:])
    local_now = now.astimezone(zone)
    start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start + timedelta(days=1) if local_now > start + timedelta(hours=6) else start


class CalendarOutput:
    """Digest events: one event per slot and day, updated in place when re-delivered."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def upsert(self, slot: str, title: str, text: str, zone: Any, zone_name: str, now: datetime) -> Tuple[str, int]:
        start = slot_start(slot, zone, now)
        marker = f"{slot}_{start.strftime('%Y%m%d')}"
        found = self.client.request("GET", CAL_EVENTS, params={
            "privateExtendedProperty": f"{OUTPUT_PROPERTY}={marker}", "singleEvents": "true",
            "showDeleted": "false", "maxResults": 5}).body.get("items") or []
        body = {"summary": title, "description": text}
        if found:
            event_id = str(found[0].get("id"))
            url = f"{CAL_EVENTS}/{urllib.parse.quote(event_id, safe='')}"
            return event_id, self.client.request("PATCH", url, params={"sendUpdates": "none"}, body=body).status
        body.update({
            "start": {"dateTime": start.isoformat(), "timeZone": zone_name},
            "end": {"dateTime": (start + timedelta(minutes=15)).isoformat(), "timeZone": zone_name},
            "transparency": "transparent",
            "extendedProperties": {"private": {OUTPUT_PROPERTY: marker}},
        })
        resp = self.client.request("POST", CAL_EVENTS, params={"sendUpdates": "none"}, body=body)
        return str(resp.body.get("id") or ""), resp.status
