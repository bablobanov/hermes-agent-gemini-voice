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
APPROVAL_UNSUPPORTED = ("approval prompts cannot be answered on the Gemini voice channel, so commands that "
                        "need a person's approval are refused here; repeat them in a messaging app")


def _message_id() -> str:
    return f"gv-{int(time.time() * 1000)}"


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
    interactive_resume = False  # after a restart, finish the interrupted task instead of asking nobody

    def __init__(self, config: PlatformConfig, *, settings: Settings, store: Any,
                 read_secret: Callable[[str], Optional[str]]):
        super().__init__(config, Platform(PLATFORM))
        self._settings, self._store, self._read_secret = settings, store, read_secret
        self._bridge: Optional[VoiceBridge] = None
        self._poller: Optional["asyncio.Task[Any]"] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        credentials = [self._read_secret(name) or "" for name in ENV_NAMES]
        if not all(credentials):
            self._set_fatal_error("not_configured", "Google access is not configured: run `hermes gemini-voice "
                                  "auth` on the server, then restart the gateway.", retryable=False)
            return False
        client = GoogleClient(TokenSource(*credentials))
        self._bridge = VoiceBridge(self._settings, client, self._store, self._dispatch, self._publish)
        self._poller = asyncio.create_task(self._bridge.run_forever(), name="gemini-voice-poller")
        self._poller.add_done_callback(_log_poller_end)
        self._mark_connected()
        logger.info("gemini-voice: connected; polling every %ss for %s", self._settings.poll_seconds,
                    ", ".join(self._settings.sources))
        return True

    async def disconnect(self) -> None:
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await poller
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        meta = metadata or {}
        if meta.get("is_approval_prompt"):
            return SendResult(success=False, error=APPROVAL_UNSUPPORTED)
        if self._bridge is None:
            return SendResult(success=False, error="gemini-voice is not connected", retryable=True)
        if str(chat_id) == OWNER_CHAT:
            if meta.get("notify"):  # only the final reply of a turn goes into the task object
                await self._bridge.deliver_answer(reply_to, content)
            return SendResult(success=True, message_id=_message_id())
        ok, ref = await self._bridge.deliver_output(str(chat_id), content)
        return SendResult(success=ok, message_id=ref if ok else None, error=None if ok else ref)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        name = "Gemini voice" if str(chat_id) == OWNER_CHAT else f"Google {chat_id}"
        return {"name": name, "type": "dm"}

    def format_message(self, content: str) -> str:
        return textutil.plain_text(content)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        return None

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        if self._bridge is None:
            return
        streamed = getattr(event, "_streamed_final_response", "")
        await self._bridge.on_turn_complete(getattr(event, "message_id", None), str(outcome.value),
                                            streamed if isinstance(streamed, str) else "")

    async def _dispatch(self, task: Task) -> None:
        source = self.build_source(chat_id=OWNER_CHAT, chat_name="Gemini voice", chat_type="dm",
                                   user_id=OWNER_CHAT, user_name=OWNER_NAME, message_id=task.key)
        event = MessageEvent(text=task.prompt, message_type=MessageType.TEXT, user_id=OWNER_CHAT,
                             user_name=OWNER_NAME, source=source, message_id=task.key,
                             metadata={"gemini_voice_object": task.key}, allow_gateway_control=False)
        await self.handle_message(event)

    def _publish(self, state: str, code: str, message: str) -> None:
        if state == "connected":
            self._mark_connected()
            return
        self._write_runtime_status_safe(f"gemini_voice_{code or state}", platform_state=state,
                                        error_code=code or None, error_message=message or None)
