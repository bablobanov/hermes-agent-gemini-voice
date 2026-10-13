"""Google Calendar, Docs and Sheets: find owner-created task objects, read their text, write replies.

Ownership is decided only from fields the Google API returns for the authorized account. Anything
created by someone else (an invitation, a shared file, an event another person put on a shared
calendar) is rejected here and never reaches Hermes, whatever its title says.

Writes never search for markers. Every write returns a *locator*: the exact text (and, for Sheets, the
row) it wrote. The next write replaces exactly that, if it is still there unchanged; otherwise it
appends. Nothing the owner wrote is ever deleted or overwritten.
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
EDIT_WINDOW = timedelta(minutes=10)
SHEET_READ_RANGE = "A1:Z500"
SHEET_CELL_LIMIT = 1000
SHEET_MAX_ROWS = 300
_HTML_HINT = re.compile(r"<(br|p|div|b|i|u|a|ul|ol|li|span)\b|</\w+>", re.IGNORECASE)
_HTML_BREAK = re.compile(r"<\s*br\s*/?\s*>|</\s*(p|div|li)\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(r"!\$?[A-Z]+\$?(\d+)")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

Locator = Dict[str, Any]


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
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _paged(client: GoogleClient, url: str, params: Dict[str, Any], items_key: str) -> Tuple[List[dict], bool]:
    """All items, and whether the listing was complete (False when ``MAX_PAGES`` cut it short)."""
    items: List[dict] = []
    page_params = dict(params)
    for _ in range(MAX_PAGES):
        body = client.request("GET", url, params=page_params).body
        items.extend(x for x in body.get(items_key) or [] if isinstance(x, dict))
        token = body.get("nextPageToken")
        if not token:
            return items, True
        page_params["pageToken"] = token
    return items, False


def html_to_text(value: str) -> str:
    """Calendar descriptions may be HTML; turn them into plain lines."""
    if not _HTML_HINT.search(value or ""):
        return value or ""
    return html.unescape(_HTML_TAG.sub("", _HTML_BREAK.sub("\n", value)))


def _first_failure(checks: Sequence[Tuple[bool, str]]) -> str:
    return next((reason for failed, reason in checks if failed), "")


# ---------------------------------------------------------------------------------------- Calendar

def event_problem(event: dict) -> str:
    """Why an event is not a plain one-off event the owner alone created and organizes ("" if it is)."""
    created, updated = parse_time(event.get("created")), parse_time(event.get("updated"))
    private = ((event.get("extendedProperties") or {}).get("private") or {})
    return _first_failure((
        (OUTPUT_PROPERTY in private, "plugin_output"),
        (event.get("status") == "cancelled", "cancelled"),
        ((event.get("eventType") or "default") != "default", "event_type"),
        (bool(event.get("recurrence") or event.get("recurringEventId")), "recurring"),
        (not (event.get("creator") or {}).get("self"), "not_creator"),
        (not (event.get("organizer") or {}).get("self"), "not_organizer"),
        (any(not a.get("self") for a in event.get("attendees") or [] if isinstance(a, dict)), "has_attendees"),
        # A delegate with "make changes to events" could rewrite a fresh owner event; such an edit moves
        # ``updated`` well past ``created``, while Gemini's own creation does not.
        (bool(created and updated and updated - created > EDIT_WINDOW), "edited_after_creation"),
    ))


def check_event(event: dict, patterns: Sequence[re.Pattern], not_before: datetime) -> Verdict:
    """Accept only an owner-only one-off event, created after ``not_before``, with a label in its title."""
    obj = VoiceObject("cal", str(event.get("id") or ""), str(event.get("summary") or ""),
                      parse_time(event.get("created")) or _EPOCH)
    reason = event_problem(event) or _first_failure((
        (obj.created < not_before, "too_old"),
        (not textutil.has_label(obj.title, patterns), "no_label"),
        (not obj.id, "no_id"),
    ))
    return Verdict(obj, not reason, reason)


class Calendar:
    """Owner's primary calendar: task events in, reply in the event description."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def candidates(self, patterns: Sequence[re.Pattern], not_before: datetime,
                   updated_min: datetime) -> Tuple[List[Verdict], bool]:
        # singleEvents=false: a recurring series is one item (and rejected), never hundreds of instances.
        params = {"updatedMin": rfc3339(updated_min), "singleEvents": "false", "showDeleted": "false",
                  "maxResults": 250, "orderBy": "updated"}
        items, complete = _paged(self.client, CAL_EVENTS, params, "items")
        return [check_event(e, patterns, not_before) for e in items], complete

    def _get(self, event_id: str) -> dict:
        return self.client.request("GET", f"{CAL_EVENTS}/{_quote(event_id)}").body

    def recheck(self, event_id: str) -> str:
        return event_problem(self._get(event_id))

    def read(self, event_id: str, previous: Optional[Locator] = None) -> str:
        event = self._get(event_id)
        start = event.get("start") or {}
        when = start.get("dateTime") or start.get("date") or ""
        own = (previous or {}).get("suffix")
        description = textutil.remove_suffix_block(event.get("description") or "", own)
        parts = [str(event.get("summary") or ""), f"When: {when}" if when else "",
                 html_to_text(description).strip()]
        return "\n".join(p for p in parts if p)

    def write(self, event_id: str, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        """Replace our previous block if it is still the exact end of the description, else append."""
        for _ in range(3):
            event = self._get(event_id)
            description, suffix = merge_description(event.get("description") or "", block_text,
                                                    (previous or {}).get("suffix"))
            try:
                status = self.client.request(
                    "PATCH", f"{CAL_EVENTS}/{_quote(event_id)}", params={"sendUpdates": "none"},
                    body={"description": description}, headers={"If-Match": str(event.get("etag") or "*")}).status
                return status, {"suffix": suffix}
            except ApiError as exc:
                if exc.status != 412:
                    raise
        raise ApiError("conflict", "event kept changing while writing", 412)


def merge_description(existing: str, block_text: str, previous_suffix: Optional[str]) -> Tuple[str, str]:
    """``(new description, suffix written)``. Our previous suffix is replaced only when it is still the
    exact end of the description. HTML descriptions keep each block line on its own text line."""
    base = existing[:-len(previous_suffix)] if previous_suffix and existing.endswith(previous_suffix) else existing
    if _HTML_HINT.search(base):
        rendered = "<br>\n".join(html.escape(line, quote=False) for line in block_text.split("\n"))
        suffix = ("<br><br>\n" if base.strip() else "") + rendered
    else:
        suffix = ("\n\n" if base.strip() else "") + block_text
    return base + suffix, suffix


# ------------------------------------------------------------------------------------------- Drive

def file_problem(item: dict) -> str:
    """Why a Drive file is not an unshared Doc or Sheet the owner owns and last modified ("" if it is)."""
    return _first_failure((
        (item.get("mimeType") not in (DOC_MIME, SHEET_MIME), "mime"),
        (item.get("trashed") is True, "trashed"),
        (item.get("ownedByMe") is not True, "not_owned"),
        (item.get("shared") is not False, "shared"),
        (not (item.get("lastModifyingUser") or {}).get("me"), "modified_by_other"),
    ))


def check_file(item: dict, patterns: Sequence[re.Pattern], not_before: datetime,
               outbound: Sequence[str]) -> Verdict:
    kind = {DOC_MIME: "doc", SHEET_MIME: "sheet"}.get(str(item.get("mimeType")), "file")
    obj = VoiceObject(kind, str(item.get("id") or ""), str(item.get("name") or ""),
                      parse_time(item.get("createdTime")) or _EPOCH)
    reason = _first_failure(((obj.id in outbound, "plugin_output"),)) or file_problem(item) or _first_failure((
        (obj.created < not_before, "too_old"),
        (not textutil.has_label(obj.title, patterns), "no_label"),
        (not obj.id, "no_id"),
    ))
    return Verdict(obj, not reason, reason)


class Drive:
    """Metadata-only listing of recently created Docs and Sheets the account can see."""

    FIELDS = "id,name,mimeType,createdTime,trashed,ownedByMe,shared,lastModifyingUser(me)"

    def __init__(self, client: GoogleClient):
        self.client = client

    def candidates(self, patterns: Sequence[re.Pattern], not_before: datetime, kinds: Sequence[str],
                   outbound: Sequence[str]) -> Tuple[List[Verdict], bool]:
        mimes = [m for m, k in ((DOC_MIME, "doc"), (SHEET_MIME, "sheet")) if k in kinds]
        if not mimes:
            return [], True
        mime_q = " or ".join(f"mimeType='{m}'" for m in mimes)
        created = not_before.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        # Files shared with the owner are listed too, so that a labelled foreign file is rejected (and
        # logged) by check_file rather than silently invisible.
        params = {"q": f"({mime_q}) and trashed=false and createdTime > '{created}'",
                  "fields": f"nextPageToken,files({self.FIELDS})", "orderBy": "createdTime desc",
                  "pageSize": 100, "spaces": "drive", "corpora": "user"}
        files, complete = _paged(self.client, DRIVE_FILES, params, "files")
        return [check_file(f, patterns, not_before, outbound) for f in files], complete

    def recheck(self, file_id: str) -> str:
        item = self.client.request("GET", f"{DRIVE_FILES}/{_quote(file_id)}", params={"fields": self.FIELDS}).body
        return file_problem(item)


# -------------------------------------------------------------------------------------------- Docs

def _paragraphs(content: Sequence[dict]) -> List[Tuple[int, int, str]]:
    """``(startIndex, endIndex, text)`` of top-level body paragraphs (Docs indexes, UTF-16 units)."""
    found = []
    for element in content or []:
        if "paragraph" in element:
            runs = element["paragraph"].get("elements") or []
            text = "".join((r.get("textRun") or {}).get("content", "") for r in runs)
            found.append((int(element.get("startIndex") or 0), int(element.get("endIndex") or 0), text))
    return found


def _all_text(content: Sequence[dict]) -> str:
    """Every paragraph's text, tables and tables of contents included, in document order."""
    parts: List[str] = []
    for element in content or []:
        if "paragraph" in element:
            parts.append("".join((r.get("textRun") or {}).get("content", "")
                                 for r in element["paragraph"].get("elements") or []))
        elif "table" in element:
            for row in element["table"].get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    parts.append(_all_text(cell.get("content") or []))
        elif "tableOfContents" in element:
            parts.append(_all_text(element["tableOfContents"].get("content") or []))
    return "".join(parts)


def find_block(paragraphs: List[Tuple[int, int, str]], block_text: str) -> Optional[Tuple[int, int]]:
    """``(start, end)`` of the LAST run of consecutive paragraphs whose text is exactly ``block_text``;
    ``end`` excludes the final paragraph's newline."""
    lines = [line + "\n" for line in block_text.split("\n")]
    for i in range(len(paragraphs) - len(lines), -1, -1):
        window = paragraphs[i:i + len(lines)]
        if [p[2] for p in window] == lines:
            return window[0][0], window[-1][1] - 1
    return None


class Docs:
    """Task text from the document; the reply replaces our acknowledgement paragraph(s) exactly."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def _get(self, doc_id: str) -> dict:
        return self.client.request("GET", f"{DOCS}/{_quote(doc_id)}").body

    def read_body(self, doc_id: str, previous: Optional[Locator] = None) -> str:
        """The document text without our own last block (the title is not part of it)."""
        text = _all_text((self._get(doc_id).get("body") or {}).get("content") or [])
        own = (previous or {}).get("text")
        return textutil.remove_suffix_block(text, own + "\n" if own else None).strip()

    def read(self, doc_id: str, title: str, previous: Optional[Locator] = None) -> str:
        return f"{title}\n{self.read_body(doc_id, previous)}".strip()

    def write(self, doc_id: str, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        for attempt in (1, 2):
            doc = self._get(doc_id)
            requests = self._requests(doc, block_text, (previous or {}).get("text"))
            try:
                return self._batch(doc_id, requests, doc.get("revisionId")), {"text": block_text}
            except ApiError as exc:
                if exc.status != 400 or attempt == 2:
                    raise  # 400 = the document changed between read and write: once more, fresh
        raise ApiError("conflict", "document kept changing while writing")  # pragma: no cover

    @staticmethod
    def _requests(doc: dict, block_text: str, previous_text: Optional[str]) -> List[dict]:
        content = (doc.get("body") or {}).get("content") or []
        paragraphs = _paragraphs(content)
        found = find_block(paragraphs, previous_text) if previous_text else None
        if found:
            start, end = found
            requests: List[dict] = []
            if end > start:
                requests.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
            requests.append({"insertText": {"location": {"index": start}, "text": block_text}})
            return requests
        has_text = any(text.strip() for _, _, text in paragraphs)
        return [{"insertText": {"endOfSegmentLocation": {}, "text": ("\n" if has_text else "") + block_text}}]

    def _batch(self, doc_id: str, requests: List[dict], revision: Optional[str]) -> int:
        body: Dict[str, Any] = {"requests": requests}
        if revision:
            body["writeControl"] = {"requiredRevisionId": revision}
        return self.client.request("POST", f"{DOCS}/{_quote(doc_id)}:batchUpdate", body=body).status

    def create_blank(self, title: str) -> str:
        doc_id = str(self.client.request("POST", DOCS, body={"title": title}).body.get("documentId") or "")
        if not doc_id:
            raise ApiError("create_failed", "no documentId in response")
        return doc_id

    def fill_output(self, doc_id: str, title: str, text: str) -> int:
        """Write an output document: the signature line first, so it can never be read back as a task."""
        body = f"{textutil.OUTPUT_MARKER} {title}\n{text.strip()}".strip()
        request = {"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]}
        return self.client.request("POST", f"{DOCS}/{_quote(doc_id)}:batchUpdate", body=request).status


# ------------------------------------------------------------------------------------------ Sheets

def quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


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


def first_row(updated_range: str) -> Optional[int]:
    match = _ROW.search(updated_range or "")
    return int(match.group(1)) if match else None


class Sheets:
    """Task text from the first sheet; replies go into column A. Appends insert rows, never overwrite."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def _first(self, sheet_id: str) -> Tuple[str, int]:
        body = self.client.request("GET", f"{SHEETS}/{_quote(sheet_id)}",
                                   params={"fields": "sheets.properties(sheetId,title,index)"}).body
        sheets = sorted((s.get("properties") or {} for s in body.get("sheets") or []),
                        key=lambda p: int(p.get("index") or 0))
        if not sheets or not sheets[0].get("title"):
            raise ApiError("no_sheet", "spreadsheet has no sheets")
        return str(sheets[0]["title"]), int(sheets[0].get("sheetId") or 0)

    def _values(self, sheet_id: str, a1: str) -> List[List[str]]:
        url = f"{SHEETS}/{_quote(sheet_id)}/values/{_quote(a1)}"
        body = self.client.request("GET", url, params={"majorDimension": "ROWS"}).body
        return [[str(c) for c in row] for row in body.get("values") or []]

    def read(self, sheet_id: str, title: str, previous: Optional[Locator] = None) -> str:
        name, _ = self._first(sheet_id)
        rows = self._values(sheet_id, f"{quote_sheet(name)}!{SHEET_READ_RANGE}")
        own_row = int((previous or {}).get("row") or 0)
        lines = [", ".join(c for c in row if c.strip()) for i, row in enumerate(rows, 1) if i != own_row]
        return f"{title}\n" + "\n".join(line for line in lines if line)

    def write(self, sheet_id: str, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        name, grid_id = self._first(sheet_id)
        lines = sheet_lines(block_text)
        row = int((previous or {}).get("row") or 0)
        if row and self._values(sheet_id, f"{quote_sheet(name)}!A{row}") == [[(previous or {}).get("text", "")]]:
            status = self._replace(sheet_id, name, grid_id, row, lines)
            return status, ({"row": row, "text": lines[0]} if len(lines) == 1 else {})
        return self._append(sheet_id, name, lines)

    def _replace(self, sheet_id: str, name: str, grid_id: int, row: int, lines: List[str]) -> int:
        """Our one-row block at ``row`` becomes ``lines``: new rows are inserted below it first."""
        if len(lines) > 1:
            insert = {"insertDimension": {"range": {"sheetId": grid_id, "dimension": "ROWS",
                                                    "startIndex": row, "endIndex": row + len(lines) - 1},
                                          "inheritFromBefore": True}}
            self.client.request("POST", f"{SHEETS}/{_quote(sheet_id)}:batchUpdate", body={"requests": [insert]})
        target = f"{quote_sheet(name)}!A{row}:A{row + len(lines) - 1}"
        body = {"range": target, "majorDimension": "ROWS", "values": [[line] for line in lines]}
        return self.client.request("PUT", f"{SHEETS}/{_quote(sheet_id)}/values/{_quote(target)}",
                                   params={"valueInputOption": "RAW"}, body=body).status

    def _append(self, sheet_id: str, name: str, lines: List[str]) -> Tuple[int, Locator]:
        target = f"{quote_sheet(name)}!A1"
        resp = self.client.request(
            "POST", f"{SHEETS}/{_quote(sheet_id)}/values/{_quote(target)}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            body={"majorDimension": "ROWS", "values": [[line] for line in lines]})
        row = first_row(str(((resp.body or {}).get("updates") or {}).get("updatedRange") or ""))
        # A multi-line block is never replaced later (only one-row acknowledgements are): record it as such.
        return resp.status, ({"row": row, "text": lines[0]} if row and len(lines) == 1 else {})


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
        body: Dict[str, Any] = {"summary": title, "description": text}
        if found:
            event_id = str(found[0].get("id"))
            return event_id, self.client.request("PATCH", f"{CAL_EVENTS}/{_quote(event_id)}",
                                                 params={"sendUpdates": "none"}, body=body).status
        body.update({
            "start": {"dateTime": start.isoformat(), "timeZone": zone_name},
            "end": {"dateTime": (start + timedelta(minutes=15)).isoformat(), "timeZone": zone_name},
            "transparency": "transparent",
            "extendedProperties": {"private": {OUTPUT_PROPERTY: marker}},
        })
        resp = self.client.request("POST", CAL_EVENTS, params={"sendUpdates": "none"}, body=body)
        return str(resp.body.get("id") or ""), resp.status
