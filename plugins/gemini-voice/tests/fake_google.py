"""In-memory stand-in for the Google endpoints the plugin calls, used as the HTTP transport in tests.

It enforces the contracts that matter to the plugin: ``If-Match`` etags on event patches, Docs
``requiredRevisionId``, Sheets ``valueInputOption``, OAuth refresh errors and 401 handling.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE = "https://www.googleapis.com/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
ALL_SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
])

Reply = Tuple[int, Dict[str, str], bytes]


def _json(status: int, body: Any) -> Reply:
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode("utf-8")


def _error(status: int, reason: str = "", message: str = "error") -> Reply:
    return _json(status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else []}})


class FakeGoogle:
    def __init__(self) -> None:
        self.events: Dict[str, Dict[str, Any]] = {}
        self.files: Dict[str, Dict[str, Any]] = {}
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.sheets: Dict[str, List[List[str]]] = {}
        self.calls: List[Tuple[str, str, Dict[str, str], Any]] = []
        self.revoked = False
        self.offline = False
        self.fail_writes = False
        self.reject_access_once = False
        self.token_scope = ALL_SCOPES
        self.refreshes = 0
        self.sheet_writes: List[Dict[str, Any]] = []
        self._seq = 0
        self._etag = 0

    # ------------------------------------------------------------------ fixtures
    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def add_event(self, summary: str, *, created: str, self_creator: bool = True, self_organizer: bool = True,
                  description: str = "", attendees: Optional[List[dict]] = None, event_type: str = "default",
                  status: str = "confirmed", private: Optional[Dict[str, str]] = None) -> str:
        event_id = self._next("ev")
        self._etag += 1
        event = {"id": event_id, "summary": summary, "created": created, "updated": created, "status": status,
                 "eventType": event_type, "description": description, "etag": f'"{self._etag}"',
                 "creator": {"self": self_creator}, "organizer": {"self": self_organizer},
                 "start": {"dateTime": "2026-10-14T10:00:00+05:00"}}
        if attendees:
            event["attendees"] = attendees
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
        if base == TOKEN_URL:
            return self._token(payload or {})
        if headers.get("Authorization") != "Bearer at-1":
            return _error(401, "authError")
        if self.reject_access_once:
            self.reject_access_once = False
            return _error(401, "authError")
        if self.fail_writes and method != "GET":
            return _error(503, "backendError")
        return self._route(method, base, params, payload, headers)

    def _token(self, form: Dict[str, str]) -> Reply:
        self.refreshes += 1
        if self.revoked:
            return _json(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        return _json(200, {"access_token": "at-1", "expires_in": 3600, "scope": self.token_scope})

    def _route(self, method: str, base: str, params: Dict[str, str], payload: Any, headers: Dict[str, str]) -> Reply:
        routes: List[Tuple[str, str, Callable[..., Reply]]] = [
            ("GET", CAL, self._cal_list), ("POST", CAL, self._cal_insert),
            ("GET", CAL + "/", self._cal_get), ("PATCH", CAL + "/", self._cal_patch),
            ("GET", DRIVE, self._drive_list), ("POST", DOCS, self._doc_create),
            ("GET", DOCS + "/", self._doc_get), ("POST", DOCS + "/", self._doc_batch),
            ("GET", SHEETS + "/", self._sheet_get), ("POST", SHEETS + "/", self._sheet_clear),
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
        return _json(200, {"items": items})

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
        return _json(200, event)

    def _cal_insert(self, _tail: str, _p: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
        event_id = self._next("ev")
        self._etag += 1
        event = dict(payload or {}, id=event_id, etag=f'"{self._etag}"', creator={"self": True},
                     organizer={"self": True}, created="2026-10-14T00:00:00Z", status="confirmed")
        self.events[event_id] = event
        return _json(200, event)

    # ------------------------------------------------------------------ drive
    def _drive_list(self, _tail: str, _p: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        return _json(200, {"files": list(self.files.values())})

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
            return _json(200, {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet 1", "index": 0}}]})
        return _json(200, {"values": [r for r in self.sheets[sheet_id]]})

    def _sheet_clear(self, tail: str, _p: Dict[str, str], _payload: Any, _h: Dict[str, str]) -> Reply:
        sheet_id, _, rest = tail.partition("/values/")
        start = self._start_row(rest.rsplit(":clear", 1)[0])
        self.sheets[sheet_id] = self.sheets[sheet_id][:start - 1]
        return _json(200, {})

    def _sheet_update(self, tail: str, params: Dict[str, str], payload: Any, _h: Dict[str, str]) -> Reply:
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
