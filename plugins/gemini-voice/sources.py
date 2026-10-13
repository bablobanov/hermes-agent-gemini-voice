"""Google Calendar, Docs and Sheets: find owner-created task objects, read their text, write replies.

Ownership is decided only from fields the Google API returns for the authorized account. Anything
created by someone else (an invitation, a shared file, an event another person put on a shared
calendar) is rejected here and never reaches Hermes, whatever its title says.

Writes never search for markers. Every write returns a *locator*: the exact text it wrote and where (the
end of an event description, the start index in a Doc, the row in a Sheet). The next write replaces exactly
that, if it is still there unchanged at that place; otherwise it appends. Nothing the owner wrote is ever
deleted or overwritten.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import textutil
from .google_api import ApiError, GoogleClient, Response

CAL_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
OUTPUT_PROPERTY = "gemini_voice"
MAX_PAGES = 5
DEEP_PAGES = 40  # one poll reads this far when a single second holds more changes than MAX_PAGES
EDIT_WINDOW = timedelta(minutes=10)
# A change this soon after creation is taken as the Gemini app finishing the event; a later one by anyone but
# the plugin means the task is not run (Google does not say who edited an event).
SEEN_GRACE = timedelta(minutes=2)
SHEET_READ_ROWS = 500
SHEET_READ_RANGE = f"A1:Z{SHEET_READ_ROWS}"
SHEET_CELL_LIMIT = 1000
SHEET_MAX_ROWS = 300
# Only real formatting tags count as HTML; any other text in angle brackets is the owner's text.
_TAGS = (r"(?:a|b|i|u|s|br|p|div|span|ul|ol|li|strong|em|strike|del|ins|font|h[1-6]|hr|img|pre|code|"
         r"blockquote|sub|sup|table|thead|tbody|tr|td|th)")
_HTML_HINT = re.compile(rf"<{_TAGS}\b[^<>]*>|</{_TAGS}\s*>", re.IGNORECASE)
_HTML_BREAK = re.compile(r"<\s*br\s*/?\s*>|</\s*(p|div|li)\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(rf"</?{_TAGS}\b[^<>]*>", re.IGNORECASE)
_ROW = re.compile(r"^\$?[A-Z]+\$?(\d+)")
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

Locator = Dict[str, Any]


@dataclass(frozen=True)
class VoiceObject:
    """A candidate task object: ``kind`` is ``cal``, ``doc`` or ``sheet``."""
    kind: str
    id: str
    title: str
    created: datetime
    fingerprint: str = ""  # events: what the task is read from, as first seen (see Calendar.recheck)

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


def _drive_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _paged(client: GoogleClient, url: str, params: Dict[str, Any], items_key: str,
           max_pages: int = MAX_PAGES) -> Tuple[List[dict], bool]:
    """Up to ``max_pages`` pages of one listing, and whether more remain. Page tokens are used only within
    this call; a later poll goes on from the last item read (its time), never from a stored token."""
    items: List[dict] = []
    page_params, seen = dict(params), set()
    for _ in range(max_pages):
        body = client.request("GET", url, params=page_params).body
        items.extend(x for x in body.get(items_key) or [] if isinstance(x, dict))
        token = str(body.get("nextPageToken") or "")
        if not token:
            return items, False
        if token in seen:  # a token going round in circles: stop here, the next poll goes on by time
            return items, True
        seen.add(token)
        page_params["pageToken"] = token
    return items, True


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
                      parse_time(event.get("created")) or _EPOCH, event_fingerprint(event))
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

    def candidates(self, patterns: Sequence[re.Pattern], not_before: datetime, updated_min: datetime,
                   pages: int = MAX_PAGES) -> Tuple[List[Verdict], Optional[datetime], bool]:
        """Verdicts on events changed since ``updated_min`` (whole seconds; in update order), the latest
        change read, and whether ``pages`` cut the listing (the next read goes on from that change)."""
        # singleEvents=false: a recurring series is one item (and rejected), never hundreds of instances.
        params = {"updatedMin": rfc3339(updated_min), "singleEvents": "false", "showDeleted": "false",
                  "maxResults": 250, "orderBy": "updated"}
        items, cut = _paged(self.client, CAL_EVENTS, params, "items", pages)
        stamps = [t for t in (parse_time(e.get("updated")) for e in items) if t is not None]
        return [check_event(e, patterns, not_before) for e in items], (max(stamps) if stamps else None), cut

    def get(self, event_id: str) -> dict:
        return self.client.request("GET", f"{CAL_EVENTS}/{_quote(event_id)}").body

    def recheck(self, event_id: str, own_updates: Sequence[Optional[str]] = (),
                own_fingerprints: Sequence[str] = (), seen: str = "") -> str:
        """Ownership again, on a fresh copy of the event (see ``judge_event``)."""
        return judge_event(self.get(event_id), own_updates, own_fingerprints, seen)

    def read(self, event_id: str, previous: Optional[Locator] = None) -> str:
        return event_text(self.get(event_id), previous)

    def plan(self, event_id: str, block_text: str, previous: Optional[Locator]) -> Dict[str, Any]:
        return plan_event_write(self.get(event_id), block_text, previous)

    def write(self, event_id: str, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        """Replace our previous block if it is still the exact end of the description, else append."""
        for _ in range(3):
            try:
                return self.apply(event_id, self.plan(event_id, block_text, previous))
            except ApiError as exc:
                if exc.status != 412:
                    raise
        raise ApiError("conflict", "event kept changing while writing", 412)

    def apply(self, event_id: str, plan: Dict[str, Any]) -> Tuple[int, Locator]:
        """Send a planned write, only onto the version it was planned on (HTTP 412 otherwise). The locator
        keeps the ``updated`` stamp of this write: the proof that the last change is ours."""
        resp = self.client.request(
            "PATCH", f"{CAL_EVENTS}/{_quote(event_id)}", params={"sendUpdates": "none"},
            body={"description": plan["description"]}, headers={"If-Match": plan["etag"]})
        return resp.status, {"suffix": plan["suffix"], "updated": str((resp.body or {}).get("updated") or "")}


def judge_event(event: dict, own_updates: Sequence[Optional[str]] = (), own_fingerprints: Sequence[str] = (),
                seen: str = "") -> str:
    """Ownership again, right before the task starts. Google does not say who edited an event, so what the
    task is read from must be what the plugin first saw (``seen``; other fields such as colour or reminders
    may change), what one of its own writes made it (its ``updated`` stamp, or the planned result when the
    response was lost), or a change made within ``SEEN_GRACE`` of creation (the Gemini app finishing it).
    Anything else means the task is not run. Without ``seen`` the time rule of ``event_problem`` decides."""
    problem = event_problem(event)
    if problem not in ("", "edited_after_creation"):
        return problem
    updated, fingerprint = parse_time(event.get("updated")), event_fingerprint(event)
    stamped = updated is not None and any(parse_time(stamp) == updated for stamp in own_updates if stamp)
    if stamped or fingerprint in set(own_fingerprints) or (seen and fingerprint == seen):
        return ""
    if not seen:
        return problem
    created = parse_time(event.get("created"))
    settling = not problem and created is not None and updated is not None and updated - created <= SEEN_GRACE
    return "" if settling else "edited_after_creation"


def event_text(event: dict, previous: Optional[Locator] = None) -> str:
    """The task text of an event: title, start and description, without our own block at its end."""
    start = event.get("start") or {}
    when = start.get("dateTime") or start.get("date") or ""
    description = event.get("description") or ""
    own = next((s for s in own_suffixes(previous) if description.endswith(s)), None)
    description = textutil.remove_suffix_block(description, own)
    parts = [str(event.get("summary") or ""), f"When: {when}" if when else "", html_to_text(description).strip()]
    return "\n".join(p for p in parts if p)


def plan_event_write(event: dict, block_text: str, previous: Optional[Locator]) -> Dict[str, Any]:
    """What a write onto this copy of the event will send, and the suffix and fingerprint of the event as it
    will leave it: kept by the caller before the write, so a write whose response (or process) is lost stays
    recognisable."""
    description, suffix = merge_description(event.get("description") or "", block_text, own_suffixes(previous))
    return {"etag": str(event.get("etag") or "*"), "description": description, "suffix": suffix,
            "fingerprint": event_fingerprint(dict(event, description=description))}


def event_fingerprint(event: dict) -> str:
    """Digest of everything a task is read from: title, start and the description as the task text shows it
    (markup turned into lines; line breaks and indentation kept, since they can change what a task means; only
    line endings and trailing spaces evened out)."""
    lines = html_to_text(str(event.get("description") or "")).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(line.rstrip() for line in lines).strip()
    fields = [str(event.get("summary") or ""), event.get("start") or {}, text]
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def own_suffixes(previous: Optional[Locator]) -> List[str]:
    """Every suffix our last write may have left: the one recorded, and those of earlier writes whose
    response was lost (any of them may be the one that landed)."""
    prev = previous or {}
    return [str(s) for s in [prev.get("suffix"), *(prev.get("suffixes") or [])] if s]


def merge_description(existing: str, block_text: str, previous_suffixes: Sequence[str]) -> Tuple[str, str]:
    """``(new description, suffix written)``. Our previous suffix is replaced only when it is still the
    exact end of the description: the only text that can be mistaken for it is a verbatim copy of our own
    block at the very end, so nothing of the owner's is lost. HTML descriptions keep each block line on its
    own text line."""
    own = next((s for s in previous_suffixes if s and existing.endswith(s)), "")
    base = existing[:-len(own)] if own else existing
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
    is_output = obj.id in outbound or textutil.is_output(obj.title)  # the output channel signs titles too
    reason = _first_failure(((is_output, "plugin_output"),)) or file_problem(item) or _first_failure((
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
                   outbound: Sequence[str], before: Optional[datetime] = None, pages: int = MAX_PAGES,
                   after: Optional[datetime] = None) -> Tuple[List[Verdict], Optional[datetime], bool]:
        """Verdicts on files created after ``not_before`` (and after ``after``, and before the second after
        ``before``), newest first, the creation time of the oldest file read, and whether more remain (the
        next read goes on from it)."""
        mimes = [m for m, k in ((DOC_MIME, "doc"), (SHEET_MIME, "sheet")) if k in kinds]
        if not mimes:
            return [], None, False
        mime_q = " or ".join(f"mimeType='{m}'" for m in mimes)
        bounds = f"createdTime > '{_drive_time(max(not_before, after or not_before))}'"
        if before is not None:  # whole seconds in the query: one second later, so no file of that second is skipped
            bounds += f" and createdTime < '{_drive_time(before + timedelta(seconds=1))}'"
        # Files shared with the owner are listed too, so that a labelled foreign file is rejected (and
        # logged) by check_file rather than silently invisible.
        params = {"q": f"({mime_q}) and trashed=false and {bounds}",
                  "fields": f"nextPageToken,files({self.FIELDS})", "orderBy": "createdTime desc",
                  "pageSize": 100, "spaces": "drive", "corpora": "user"}
        files, cut = _paged(self.client, DRIVE_FILES, params, "files", pages)
        stamps = [t for t in (parse_time(f.get("createdTime")) for f in files) if t is not None]
        return [check_file(f, patterns, not_before, outbound) for f in files], (min(stamps) if stamps else None), cut

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


def _all_text(content: Sequence[dict], skip: Optional[Tuple[int, int]] = None) -> str:
    """Every paragraph's text, tables and tables of contents included, in document order; top-level
    paragraphs starting inside ``skip`` (``[start, end)``, our own block) are left out."""
    parts: List[str] = []
    for element in content or []:
        if "paragraph" in element:
            if skip and skip[0] <= int(element.get("startIndex") or 0) < skip[1]:
                continue
            parts.append("".join((r.get("textRun") or {}).get("content", "")
                                 for r in element["paragraph"].get("elements") or []))
        elif "table" in element:
            for row in element["table"].get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    parts.append(_all_text(cell.get("content") or []))
        elif "tableOfContents" in element:
            parts.append(_all_text(element["tableOfContents"].get("content") or []))
    return "".join(parts)


def find_block(paragraphs: List[Tuple[int, int, str]], block_text: str, start: int) -> Optional[Tuple[int, int]]:
    """``(start, end)`` of our block when it is still where we wrote it: adjacent top-level paragraphs that
    begin exactly at ``start`` and whose text is exactly ``block_text``; ``end`` excludes the last newline.
    A copy of the same text anywhere else, or a table between the lines, is never taken for it."""
    lines = [line + "\n" for line in block_text.split("\n")]
    first = next((i for i, p in enumerate(paragraphs) if p[0] == start), None)
    if first is None:
        return None
    window = paragraphs[first:first + len(lines)]
    adjacent = all(a[1] == b[0] for a, b in zip(window, window[1:]))
    if len(window) == len(lines) and adjacent and [p[2] for p in window] == lines:
        return start, window[-1][1] - 1
    return None


def _own_block(content: Sequence[dict], previous: Optional[Locator]) -> Optional[Tuple[int, int]]:
    prev = previous or {}
    if not prev.get("text") or not isinstance(prev.get("start"), int):
        return None
    return find_block(_paragraphs(content), str(prev["text"]), prev["start"])


class Docs:
    """Task text from the document; the reply replaces our acknowledgement paragraph(s) exactly."""

    def __init__(self, client: GoogleClient):
        self.client = client

    def get(self, doc_id: str) -> dict:
        return self.client.request("GET", f"{DOCS}/{_quote(doc_id)}").body

    @staticmethod
    def body_text(doc: dict, previous: Optional[Locator] = None) -> str:
        """The document text without our own block, if it is still where we wrote it (no title)."""
        content = (doc.get("body") or {}).get("content") or []
        own = _own_block(content, previous)
        return _all_text(content, (own[0], own[1] + 1) if own else None).strip()

    def read_body(self, doc_id: str, previous: Optional[Locator] = None) -> str:
        return self.body_text(self.get(doc_id), previous)

    def read(self, doc_id: str, title: str, previous: Optional[Locator] = None) -> str:
        return f"{title}\n{self.read_body(doc_id, previous)}".strip()

    def write(self, doc_id: str, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        for attempt in (1, 2):
            try:
                return self.write_on(doc_id, self.get(doc_id), block_text, previous)
            except ApiError as exc:
                if exc.status != 400 or attempt == 2:
                    raise  # 400 = the document changed between read and write: once more, fresh
        raise ApiError("conflict", "document kept changing while writing")  # pragma: no cover

    def write_on(self, doc_id: str, doc: dict, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        """Write onto exactly this revision of the document (HTTP 400 if it changed since)."""
        requests, start = self._plan(doc, block_text, previous)
        return self._batch(doc_id, requests, doc.get("revisionId")), {"text": block_text, "start": start}

    @staticmethod
    def _plan(doc: dict, block_text: str, previous: Optional[Locator]) -> Tuple[List[dict], int]:
        """The batchUpdate requests and the index where ``block_text`` will start (Docs indexes)."""
        content = (doc.get("body") or {}).get("content") or []
        found = _own_block(content, previous)
        if found:
            start, end = found
            requests: List[dict] = []
            if end > start:
                requests.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}})
            requests.append({"insertText": {"location": {"index": start}, "text": block_text}})
            return requests, start
        # Appended before the body's final newline; after a separating newline when there is text above.
        has_text = any(text.strip() for _, _, text in _paragraphs(content))
        body_end = int((content[-1] if content else {}).get("endIndex") or 2)
        separator = "\n" if has_text else ""
        return [{"insertText": {"endOfSegmentLocation": {}, "text": separator + block_text}}], \
            body_end - 1 + len(separator)

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


def sheet_lines(block_text: str, shortened: str = "(reply shortened)") -> List[str]:
    """Block text as cell-sized lines (long paragraphs wrapped); past ``SHEET_MAX_ROWS`` the last row says
    the reply was shortened."""
    lines: List[str] = []
    for paragraph in block_text.split("\n"):
        while len(paragraph) > SHEET_CELL_LIMIT:
            cut = paragraph.rfind(" ", 0, SHEET_CELL_LIMIT)
            cut = cut if cut > SHEET_CELL_LIMIT // 2 else SHEET_CELL_LIMIT
            lines.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        lines.append(paragraph)
    return lines if len(lines) <= SHEET_MAX_ROWS else lines[:SHEET_MAX_ROWS - 1] + [shortened]


def first_row(updated_range: str) -> Optional[int]:
    """First row of an A1 range such as ``'Sheet 1'!A12:A14``; the sheet name itself may contain ``!``."""
    match = _ROW.match((updated_range or "").rsplit("!", 1)[-1])
    return int(match.group(1)) if match else None


class Sheets:
    """Task text from the first sheet; replies go into column A. Appends insert rows, never overwrite."""

    def __init__(self, client: GoogleClient, shortened: str = "(reply shortened)"):
        self.client = client
        self.shortened = shortened

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
        """The first sheet as text lines; only our own cell is left out, and only while it still holds
        exactly our text. Data beyond the range read is not visible here, so the bounds are always said."""
        name, _ = self._first(sheet_id)
        rows = self._values(sheet_id, f"{quote_sheet(name)}!{SHEET_READ_RANGE}")
        prev = previous or {}
        own_row, own = int(prev.get("row") or 0), str(prev.get("text") or "")
        lines = []
        for number, row in enumerate(rows, 1):
            cells = row[1:] if number == own_row and own and row[:1] == [own] else row
            line = ", ".join(c for c in cells if c.strip())
            if line:
                lines.append(line)
        return "\n".join([title, *lines, f"[read: the first sheet, rows 1-{SHEET_READ_ROWS}, columns A-Z]"])

    def write(self, sheet_id: str, block_text: str, previous: Optional[Locator]) -> Tuple[int, Locator]:
        name, grid_id = self._first(sheet_id)
        lines = sheet_lines(block_text, self.shortened)
        prev = previous or {}
        row, own = int(prev.get("row") or 0), str(prev.get("text") or "")
        if row and own and self._values(sheet_id, f"{quote_sheet(name)}!A{row}") == [[own]]:
            return self._replace(sheet_id, grid_id, row, own, lines), \
                ({"row": row, "text": lines[0]} if len(lines) == 1 else {})
        return self._append(sheet_id, name, lines)

    def _replace(self, sheet_id: str, grid_id: int, row: int, own: str, lines: List[str]) -> int:
        """Our one-row block at ``row`` gives way to ``lines`` in one batchUpdate (all or nothing): the lines go
        into new rows inserted in its place, and our old cell (now right below them) is emptied through
        findReplace, which matches only while the cell holds exactly our text. The server compares and
        clears in one step, so an edit of the owner is never written over; whatever it finds, the block is
        complete, and nothing is left for a second write."""
        n = len(lines)
        cell = {"sheetId": grid_id, "startRowIndex": row - 1 + n, "endRowIndex": row + n,
                "startColumnIndex": 0, "endColumnIndex": 1}
        clear = {"findReplace": {"find": own, "replacement": "", "range": cell, "matchCase": True,
                                 "matchEntireCell": True, "searchByRegex": False, "includeFormulas": False}}
        return self._batch(sheet_id, [*self._insert_rows(grid_id, row - 1, lines), clear]).status

    @staticmethod
    def _insert_rows(grid_id: int, at_index: int, lines: List[str]) -> List[dict]:
        """Requests that put ``lines`` into new rows inserted at 0-based ``at_index`` (rows from there move
        down), as plain strings never parsed as formulas; nothing existing is written over."""
        return [{"insertDimension": {"range": {"sheetId": grid_id, "dimension": "ROWS", "startIndex": at_index,
                                               "endIndex": at_index + len(lines)}, "inheritFromBefore": False}},
                {"updateCells": {"start": {"sheetId": grid_id, "rowIndex": at_index, "columnIndex": 0},
                                 "rows": [{"values": [{"userEnteredValue": {"stringValue": line}}]} for line in lines],
                                 "fields": "userEnteredValue"}}]

    def _batch(self, sheet_id: str, requests: List[dict]) -> Response:
        return self.client.request("POST", f"{SHEETS}/{_quote(sheet_id)}:batchUpdate", body={"requests": requests})

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
