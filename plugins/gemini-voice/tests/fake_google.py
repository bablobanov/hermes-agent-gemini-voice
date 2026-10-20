"""In-memory stand-in for the Google endpoints the plugin calls, used as the HTTP transport in tests.

It stands where Hermes's plugin OAuth route stands in production: the plugin must never send its own
``Authorization`` header, and ``refusal`` plays a refusal of the route (no sign-in, a revoked one) that
happens before anything is sent. It enforces the contracts that matter to the plugin: ``If-Match`` etags on
event patches, Docs ``requiredRevisionId``, Sheets ``valueInputOption``.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE = "https://www.googleapis.com/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

Reply = Tuple[int, Dict[str, str], bytes]


def _json(status: int, body: Any) -> Reply:
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8")


def _when(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _error(status: int, reason: str = "", message: str = "error") -> Reply:
    return _json(status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else []}})


class FakeGoogle:
    def __init__(self) -> None:
        self.events: Dict[str, Dict[str, Any]] = {}
        self.files: Dict[str, Dict[str, Any]] = {}
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.sheets: Dict[str, List[List[str]]] = {}
        self.calls: List[Tuple[str, str, Dict[str, str], Any]] = []
        self.refusal: Optional[Exception] = None  # raised before anything is sent, as Hermes's route does
        self.error_message = "error"  # error.message of refused requests (Google's own text)
        self.offline = False
        self.fail_writes = False
        self.fail_cell_writes = False  # writing cell values fails (a values PUT, or a batchUpdate that writes cells)
        self.before_sheet_batch: Optional[Callable[[str], None]] = None
        self.lose_response_once: Optional[Tuple[str, str]] = None  # (method, URL prefix)
        self.offline_urls: List[str] = []  # URL prefixes that time out, e.g. one Google API down
        self.cal_page_size = 250
        self.drive_page_size = 100
        self.refuse: Dict[Tuple[str, str], Tuple[int, str]] = {}  # (method, url) -> (status, reason)
        self.sheet_writes: List[Dict[str, Any]] = []
        self._seq = 0
        self._etag = 0

    # ------------------------------------------------------------------ fixtures
    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def add_event(self, summary: str, *, created: str, self_creator: bool = True, self_organizer: bool = True,
                  description: str = "", attendees: Optional[List[dict]] = None, event_type: str = "default",
                  status: str = "confirmed", private: Optional[Dict[str, str]] = None,
                  updated: Optional[str] = None, recurrence: Optional[List[str]] = None) -> str:
        event_id = self._next("ev")
        self._etag += 1
        event = {"id": event_id, "summary": summary, "created": created, "updated": updated or created, "status": status,
                 "eventType": event_type, "description": description, "etag": f'"{self._etag}"',
                 "creator": {"self": self_creator}, "organizer": {"self": self_organizer},
                 "start": {"dateTime": "2026-10-14T10:00:00+05:00"}}
        if attendees:
            event["attendees"] = attendees
        if recurrence:
            event["recurrence"] = list(recurrence)
        if private:
            event["extendedProperties"] = {"private": dict(private)}
        self.events[event_id] = event
        return event_id

    def add_doc(self, name: str, text: str, *, created: str, owned: bool = True, shared: bool = False,
                modified_by_me: bool = True) -> str:
        doc_id = self._next("doc")
        self.files[doc_id] = {"id": doc_id, "name": name, "mimeType": "application/vnd.google-apps.document",
                              "createdTime": created, "ownedByMe": owned, "shared": shared,
                              "lastModifyingUser": {"me": modified_by_me}}
        self.docs[doc_id] = {"text": (text if text.endswith("\n") else text + "\n"), "rev": 1}
        return doc_id

    def add_sheet(self, name: str, rows: List[List[str]], *, created: str, owned: bool = True) -> str:
        sheet_id = self._next("sh")
        self.files[sheet_id] = {"id": sheet_id, "name": name, "mimeType": "application/vnd.google-apps.spreadsheet",
                                "createdTime": created, "ownedByMe": owned, "shared": False,
                                "lastModifyingUser": {"me": True}}
        self.sheets[sheet_id] = [list(r) for r in rows]
        return sheet_id

    def doc_text(self, doc_id: str) -> str:
        return self.docs[doc_id]["text"]

    # ------------------------------------------------------------------ transport
    def __call__(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Reply:
        if any(name.lower() == "authorization" for name in headers):
            raise AssertionError("the plugin sent its own Authorization header; Hermes attaches the token")
        if self.refusal is not None:
            raise self.refusal
        if self.offline:
            raise OSError("network down")
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        payload: Any = None
        if body:
            raw = body.decode("utf-8")
            payload = json.loads(raw) if raw.startswith(("{", "[")) else dict(urllib.parse.parse_qsl(raw))
        self.calls.append((method, base, params, payload))
        if any(base.startswith(prefix) for prefix in self.offline_urls):
            raise OSError("timed out")
        if self.fail_writes and method != "GET":
            return _error(503, "backendError")
        if (method, base) in self.refuse:
            status, reason = self.refuse[(method, base)]
            return _error(status, reason, self.error_message)
        reply = self._route(method, base, params, payload, headers)
        if self.lose_response_once and self.lose_response_once[0] == method \
                and base.startswith(self.lose_response_once[1]):
            self.lose_response_once = None
            raise OSError("timed out")  # Google applied the request; its answer never arrived
        return reply

    def _route(self, method: str, base: str, params: Dict[str, str], payload: Any, headers: Dict[str, str]) -> Reply:
        routes: List[Tuple[str, str, Callable[..., Reply]]] = [
            ("GET", CAL, self._cal_list), ("POST", CAL, self._cal_insert),
            ("GET", CAL + "/", self._cal_get), ("PATCH", CAL + "/", self._cal_patch),
            ("GET", DRIVE, self._drive_list), ("GET", DRIVE + "/", self._drive_get),
            ("POST", DOCS, self._doc_create),
            ("GET", DOCS + "/", self._doc_get), ("POST", DOCS + "/", self._doc_batch),
            ("GET", SHEETS + "/", self._sheet_get), ("POST", SHEETS + "/", self._sheet_post),
            ("PUT", SHEETS + "/", self._sheet_update),
        ]
        for verb, prefix, handler in routes:
            exact = not prefix.endswith("/")
            if verb == method and ((exact and base == prefix) or (not exact and base.startswith(prefix))):
                return handler(base[len(prefix):] if not exact else "", params, payload, headers)
        return _error(404, "notFound")

    # ------------------------------------------------------------------ calendar
    def _cal_list(self, _tail: str, params: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        items = list(self.events.values())
        prop = params.get("privateExtendedProperty")
        if prop:
            key, _, value = prop.partition("=")
            items = [e for e in items if ((e.get("extendedProperties") or {}).get("private") or {}).get(key) == value]
        if params.get("updatedMin"):
            items = [e for e in items if _when(e.get("updated")) >= _when(params["updatedMin"])]
        if params.get("orderBy") == "updated":
            items.sort(key=lambda e: _when(e.get("updated")))
        start = int(params.get("pageToken") or 0)
        size = min(int(params.get("maxResults") or 250), self.cal_page_size)
        page = {"items": items[start:start + size]}
        if start + size < len(items):
            page["nextPageToken"] = str(start + size)
        return _json(200, page)

    def _cal_get(self, tail: str, _p: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        event = self.events.get(urllib.parse.unquote(tail))
        return _json(200, event) if event else _error(404, "notFound")

    def _cal_patch(self, tail: str, _p: Dict[str, str], payload: Any, headers: Dict[str, str]) -> Reply:
        event = self.events.get(urllib.parse.unquote(tail))
        if event is None:
            return _error(404, "notFound")
        match = headers.get("If-Match")
        if match not in (None, "*", event["etag"]):
            return _error(412, "conditionNotMet")
        event.update(payload or {})
        self._etag += 1
        event["etag"] = f'"{self._etag}"'
        event["updated"] = f"2099-01-01T00:00:{self._etag % 60:02d}.000Z"  # Google moves `updated` on every change
        return _json(200, event)

    def _cal_insert(self, _tail: str, _p: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
        event_id = self._next("ev")
        self._etag += 1
        event = dict(payload or {}, id=event_id, etag=f'"{self._etag}"', creator={"self": True},
                     organizer={"self": True}, created="2026-10-14T00:00:00Z", status="confirmed")
        self.events[event_id] = event
        return _json(200, event)

    # ------------------------------------------------------------------ drive
    def _drive_list(self, _tail: str, params: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        items = list(self.files.values())
        after = re.search(r"createdTime > '([^']+)'", params.get("q", ""))
        if after:
            items = [f for f in items if _when(f["createdTime"]) > _when(after.group(1) + "+00:00")]
        before = re.search(r"createdTime < '([^']+)'", params.get("q", ""))
        if before:
            items = [f for f in items if _when(f["createdTime"]) < _when(before.group(1) + "+00:00")]
        items.sort(key=lambda f: _when(f["createdTime"]), reverse=True)
        start = int(params.get("pageToken") or 0)
        size = min(int(params.get("pageSize") or 100), self.drive_page_size)
        page = {"files": items[start:start + size]}
        if start + size < len(items):
            page["nextPageToken"] = str(start + size)
        return _json(200, page)

    def _drive_get(self, tail: str, _p: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        item = self.files.get(urllib.parse.unquote(tail))
        return _json(200, item) if item else _error(404, "notFound")

    # ------------------------------------------------------------------ docs
    def _doc_body(self, doc_id: str) -> Dict[str, Any]:
        text = self.docs[doc_id]["text"]
        content: List[Dict[str, Any]] = [{"endIndex": 1, "sectionBreak": {}}]
        index = 1
        for line in text.splitlines(keepends=True):
            content.append({"startIndex": index, "endIndex": index + len(line),
                            "paragraph": {"elements": [{"textRun": {"content": line}}]}})
            index += len(line)
        return {"documentId": doc_id, "revisionId": f"r{self.docs[doc_id]['rev']}", "body": {"content": content}}

    def _doc_get(self, tail: str, _p: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        doc_id = urllib.parse.unquote(tail)
        return _json(200, self._doc_body(doc_id)) if doc_id in self.docs else _error(404, "notFound")

    def _doc_create(self, _tail: str, _p: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
        doc_id = self._next("doc")
        self.docs[doc_id] = {"text": "\n", "rev": 1, "title": (payload or {}).get("title")}
        return _json(200, {"documentId": doc_id})

    def _doc_batch(self, tail: str, _p: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
        doc_id = urllib.parse.unquote(tail).split(":")[0]
        doc = self.docs.get(doc_id)
        if doc is None:
            return _error(404, "notFound")
        required = ((payload or {}).get("writeControl") or {}).get("requiredRevisionId")
        if required and required != f"r{doc['rev']}":
            return _error(400, "failedPrecondition", "revision mismatch")
        for request in (payload or {}).get("requests") or []:
            doc["text"] = self._apply(doc["text"], request)
        doc["rev"] += 1
        return _json(200, {"documentId": doc_id})

    @staticmethod
    def _apply(text: str, request: Dict[str, Any]) -> str:
        if "deleteContentRange" in request:
            rng = request["deleteContentRange"]["range"]
            return text[:rng["startIndex"] - 1] + text[rng["endIndex"] - 1:]
        insert = request["insertText"]
        position = len(text) if "endOfSegmentLocation" in insert else insert["location"]["index"]
        return text[:position - 1] + insert["text"] + text[position - 1:]

    # ------------------------------------------------------------------ sheets
    @staticmethod
    def _start_row(rng: str) -> int:
        cell = urllib.parse.unquote(rng).split("!")[-1].split(":")[0]
        return int("".join(ch for ch in cell if ch.isdigit()) or "1")

    def _sheet_get(self, tail: str, params: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        sheet_id, _, rest = tail.partition("/")
        if sheet_id not in self.sheets:
            return _error(404, "notFound")
        if not rest:
            return _json(200, {"sheets": [{"properties": {"sheetId": 7, "title": "Sheet 1", "index": 0}}]})
        rng = urllib.parse.unquote(rest.split("values/", 1)[1]).split("!")[-1]
        rows = self.sheets[sheet_id]
        if ":" in rng:  # the plugin reads A1:Z500
            return _json(200, {"values": [r for r in rows[:500]]})
        row = self._start_row(rng)
        cell = rows[row - 1][:1] if row - 1 < len(rows) and rows[row - 1] else []
        return _json(200, {"values": [cell]} if cell else {})

    def _sheet_request(self, rows: List[List[str]], request: Dict[str, Any]) -> Dict[str, Any]:
        if "insertDimension" in request:
            rng = request["insertDimension"]["range"]
            assert rng["sheetId"] == 7 and rng["dimension"] == "ROWS", request
            for _ in range(rng["endIndex"] - rng["startIndex"]):
                rows.insert(rng["startIndex"], [])
            return {}
        if "findReplace" in request:  # whole-cell, case-sensitive, literal: a compare-and-swap on one cell
            swap = request["findReplace"]
            rng, index = swap["range"], swap["range"]["startRowIndex"]
            assert rng["sheetId"] == 7 and rng["endRowIndex"] == index + 1 and swap["matchEntireCell"], request
            assert swap["matchCase"] and not swap["searchByRegex"], request
            if index < len(rows) and rows[index][:1] == [swap["find"]]:
                rows[index][0] = swap["replacement"]
                if not any(rows[index]):
                    rows[index] = []  # an emptied cell: the row reads as empty, like in Sheets
                self.sheet_writes.append({"valueInputOption": "RAW", "body": {"values": [[swap["replacement"]]]}})
                return {"findReplace": {"occurrencesChanged": 1, "valuesChanged": 1}}
            return {"findReplace": {}}
        update = request["updateCells"]
        assert update["start"]["sheetId"] == 7 and update["fields"] == "userEnteredValue", request
        values = [[c["userEnteredValue"]["stringValue"] for c in r["values"]] for r in update["rows"]]
        self.sheet_writes.append({"valueInputOption": "RAW", "body": {"values": values}})
        for offset, row in enumerate(values):
            index = update["start"]["rowIndex"] + offset
            while len(rows) <= index:
                rows.append([])
            rows[index] = list(row)
        return {}

    def _sheet_post(self, tail: str, params: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
        if tail.endswith(":batchUpdate"):
            sheet_id = urllib.parse.unquote(tail).split(":")[0]
            requests = (payload or {}).get("requests") or []
            if self.before_sheet_batch:
                self.before_sheet_batch(sheet_id)  # e.g. the owner edits a cell right before our write lands
            if self.fail_cell_writes and any("updateCells" in r or "findReplace" in r for r in requests):
                return _error(503, "backendError")  # batchUpdate is all or nothing: nothing applied
            rows = self.sheets[sheet_id]
            return _json(200, {"replies": [self._sheet_request(rows, r) for r in requests]})
        sheet_id, _, rest = tail.partition("/values/")
        assert rest.endswith(":append"), tail
        self.sheet_writes.append({"valueInputOption": params.get("valueInputOption"),
                                  "insertDataOption": params.get("insertDataOption"), "body": payload})
        rows = self.sheets[sheet_id]
        while rows and not rows[-1]:
            rows.pop()
        start = len(rows) + 1
        rows.extend(list(r) for r in (payload or {}).get("values") or [])
        end = len(rows)
        return _json(200, {"updates": {"updatedRange": f"'Sheet 1'!A{start}:A{end}"}})

    def _sheet_update(self, tail: str, params: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
        if self.fail_cell_writes:
            return _error(503, "backendError")
        sheet_id, _, rest = tail.partition("/values/")
        self.sheet_writes.append({"valueInputOption": params.get("valueInputOption"), "body": payload})
        rows = self.sheets[sheet_id]
        start = self._start_row(rest)
        while len(rows) < start - 1:
            rows.append([])
        for offset, row in enumerate((payload or {}).get("values") or []):
            index = start - 1 + offset
            if index < len(rows):
                rows[index] = list(row)
            else:
                rows.append(list(row))
        return _json(200, {"updatedRange": urllib.parse.unquote(rest)})
