"""Hermes gateway adapter: the Gemini voice channel as a messaging platform. Wiring only; see bridge.py."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any, Callable, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from . import textutil
from .auth_cli import ENV_NAMES
from .bridge import OWNER_CHAT, PLATFORM, Settings, Task, VoiceBridge
from .google_api import GoogleClient, TokenSource

logger = logging.getLogger("gemini_voice")

OWNER_NAME = "Owner (Gemini voice)"
# Every redelivery line of the gateway's delivery ledger starts like this (gateway/delivery_ledger.py).
LEDGER_MARKER = "♻️ Recovered reply"
# Prefix of this adapter's refusals: final, never retried, never re-sent as plain text.
REFUSED = "gemini-voice: "
NOT_A_TARGET = ("the voice chat only takes the final reply of a voice task; deliver to gemini_voice:doc "
                "(a new document) or gemini_voice:event-HHMM (an event at that local time)")
NO_ATTACHMENTS = "files, images and audio are not delivered on the voice channel; put the content into the text"


def _message_id() -> str:
    return f"gv-{int(time.time() * 1000)}"


def _refuse(reason: str) -> SendResult:
    return SendResult(success=False, error=REFUSED + reason, retryable=False)


def _without_ledger_marker(content: str) -> str:
    """``content`` without the delivery ledger's redelivery line, if it starts with one."""
    if content.startswith(LEDGER_MARKER) and "\n\n" in content:
        return content.split("\n\n", 1)[1]
    return content


def _log_poller_end(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("gemini-voice: poller stopped: %r", exc)


class GeminiVoiceAdapter(BasePlatformAdapter):
    """Inbound: owner-created Google objects from the poller. Outbound: replies and cron digests to Google."""

    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = 100_000
    splits_long_messages = True  # one final reply per turn; length is handled per Google object
    supports_async_delivery = False  # a late detached message could land in the wrong task object
    # After a restart the gateway resumes an interrupted turn by reporting it and asking what next
    # (the owner answers with a new voice task), instead of silently redoing steps already done.
    interactive_resume = True

    def __init__(self, config: PlatformConfig, *, settings: Settings, store: Any,
                 read_secret: Callable[[str], Optional[str]]):
        super().__init__(config, Platform(PLATFORM))
        self._settings, self._store, self._read_secret = settings, store, read_secret
        self._bridge: Optional[VoiceBridge] = None
        self._poller: Optional["asyncio.Task[Any]"] = None
        self._owner_key: Optional[str] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._poller is not None and not self._poller.done():
            return True  # one poller (and one journal writer) per adapter
        credentials = [self._read_secret(name) or "" for name in ENV_NAMES]
        if not all(credentials):
            self._set_fatal_error("not_configured", "Google access is not configured: run `hermes gemini-voice "
                                  "auth` on the server, then restart the gateway.", retryable=False)
            return False
        client = GoogleClient(TokenSource(*credentials))
        self._bridge = VoiceBridge(self._settings, client, self._store, self._dispatch, self._publish,
                                   session_busy=self._owner_session_busy)
        self._poller = asyncio.create_task(self._bridge.run_forever(), name="gemini-voice-poller")
        self._poller.add_done_callback(_log_poller_end)
        self._mark_connected()
        logger.info("gemini-voice: connected; polling every %ss for %s", self._settings.poll_seconds,
                    ", ".join(self._settings.sources))
        return True

    async def disconnect(self) -> None:
        poller, self._poller, self._bridge = self._poller, None, None
        if poller is not None:
            poller.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await poller
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Success means the text is in the journal and will be written into a Google object. Anything the
        voice chat cannot take is refused for good (see ``_send_retry_is_final``), never reported as sent.

        Only the gateway's own turn delivery marks a final reply (``notify``). The delivery ledger's
        redeliveries carry no such mark and look exactly like ``send_message`` from another chat (the
        "Recovered reply" marker is plain text anyone can write), so they never choose an object: one that
        repeats a reply this plugin already holds is acknowledged, so the ledger stops; any other is
        refused. A reply lost in the instant between the turn and this adapter ends as the task's "did not
        finish" note."""
        meta = metadata or {}
        bridge = self._bridge
        if bridge is None:
            # Between a disconnect and the next connect. Final on purpose: retries would end in the engine's
            # "delivery failed" notice, sent with the same anchor and written as the task's reply.
            return _refuse("gemini-voice is not connected")
        if str(chat_id) != OWNER_CHAT:
            ok, ref = await bridge.deliver_output(str(chat_id), content)
            return SendResult(success=ok, message_id=ref if ok else None, error=None if ok else ref)
        if meta.get("job_id"):
            return _refuse(NOT_A_TARGET)
        if meta.get("is_approval_prompt"):
            # Nobody can press Approve by voice: the engine waits approvals.timeout, then refuses.
            return _refuse("approval prompts cannot be answered on the voice channel")
        if meta.get("_interim_send"):
            return SendResult(success=True, message_id=_message_id())  # mid-turn status: not shown here by design
        if not meta.get("notify"):  # a ledger redelivery, send_message from another chat, a turn-error notice
            if await bridge.holds_reply(_without_ledger_marker(content)):
                return SendResult(success=True, message_id=_message_id())  # already in the journal: nothing to do
            return _refuse(NOT_A_TARGET)
        # The final reply of a turn; without an anchor only for the turn resumed after a restart.
        problem = await bridge.deliver_answer(reply_to, content)
        return _refuse(problem) if problem else SendResult(success=True, message_id=_message_id())

    def _send_retry_is_final(self, result: SendResult) -> bool:
        """Our refusals are final: a retry or the plain-text fallback would only repeat them."""
        return str(getattr(result, "error", "") or "").startswith(REFUSED)

    # Attachments have no place in a Google object read aloud. The engine's defaults would send their URL
    # or a failure notice as plain text without an anchor; refuse them here instead.
    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return _refuse(NO_ATTACHMENTS)

    async def send_animation(self, chat_id: str, animation_url: str, caption: Optional[str] = None,
                             reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return _refuse(NO_ATTACHMENTS)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs: Any) -> SendResult:
        return _refuse(NO_ATTACHMENTS)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> SendResult:
        return _refuse(NO_ATTACHMENTS)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        return _refuse(NO_ATTACHMENTS)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs: Any) -> SendResult:
        return _refuse(NO_ATTACHMENTS)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        name = "Gemini voice" if str(chat_id) == OWNER_CHAT else f"Google {chat_id}"
        return {"name": name, "type": "dm"}

    def format_message(self, content: str) -> str:
        return textutil.plain_text(content)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        return None

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        bridge = self._bridge
        if bridge is None:
            return
        streamed = getattr(event, "_streamed_final_response", "")
        await bridge.on_turn_complete(getattr(event, "message_id", None), str(outcome.value),
                                      streamed if isinstance(streamed, str) else "")

    def _owner_source(self, message_id: Optional[str] = None) -> Any:
        return self.build_source(chat_id=OWNER_CHAT, chat_name="Gemini voice", chat_type="dm",
                                 user_id=OWNER_CHAT, user_name=OWNER_NAME, message_id=message_id)

    def _owner_session_busy(self) -> bool:
        """Whether a new task must wait: the gateway is still resuming turns cut off by a restart (a task
        handed over now would only be parked and its turn reported empty), or a turn in the voice chat is
        running. A guard whose turn has already ended is not busy: handing the next task over through
        ``handle_message`` is what makes the gateway heal such a guard."""
        if getattr(getattr(self, "gateway_runner", None), "_startup_restore_in_progress", False) is True:
            return True
        if self._owner_key is None:
            self._owner_key = self._source_session_key(self._owner_source())
        if self._owner_key not in getattr(self, "_active_sessions", {}):
            return False
        stale = getattr(self, "_session_task_is_stale", None)
        return not (callable(stale) and stale(self._owner_key))

    async def _dispatch(self, task: Task) -> None:
        source = self._owner_source(task.key)
        event = MessageEvent(text=task.prompt, message_type=MessageType.TEXT, user_id=OWNER_CHAT,
                             user_name=OWNER_NAME, source=source, message_id=task.key,
                             metadata={"gemini_voice_object": task.key}, allow_gateway_control=False)
        await self.handle_message(event)
        if getattr(event, "_gateway_accepted", True) is not True:
            raise RuntimeError("the gateway did not accept the task")

    def _publish(self, state: str, code: str, message: str) -> None:
        if state == "connected":
            self._mark_connected()
            return
        self._write_runtime_status_safe(f"gemini_voice_{code or state}", platform_state=state,
                                        error_code=code or None, error_message=message or None)
