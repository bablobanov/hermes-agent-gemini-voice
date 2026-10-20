"""Hermes Agent Gemini Voice: tasks dictated to the Gemini app on a phone reach Hermes through the owner's
own Google Calendar, Docs and Sheets; the reply is written into the same object for Gemini to read aloud.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from .auth_cli import ENV_CLIENT_ID, ENV_NAMES, default_io, handle, setup_parser
from .bridge import PLATFORM, TEXT_ONLY, Settings, Task, VoiceBridge, valid_target
from .journal import NullStore

logger = logging.getLogger("gemini_voice")

HOME_CHANNEL_ENV = "GEMINI_VOICE_HOME_CHANNEL"

PLATFORM_HINT = (
    "This conversation is one task from the owner's voice channel: the owner dictated it to the Gemini app "
    "on their phone, which saved it as a Google Calendar event, Doc or Sheet in the owner's own account. "
    "Every voice task is a conversation of its own, without the history of earlier ones. "
    "Your final reply is written into that same Google object and read aloud by Gemini when the owner "
    "asks, so reply in plain text without Markdown, briefly enough to listen to (about 150 words unless "
    "the task asks for more), in the language of the task. You cannot ask a clarifying question in the "
    "middle of a turn: make a reasonable assumption, or put the question in your final reply. Whenever "
    "your rules require the owner's confirmation, ask for it in the final reply; the owner answers with a "
    "new complete task, which starts a new conversation that does not see this one, so say briefly what "
    "the new task must contain besides the answer. Files, images and audio are not delivered "
    "here: put everything into the text. This chat receives nothing later on its own, neither scheduled "
    "jobs nor background-process notices: when the owner asks for something at a later time or on a "
    "schedule (a reminder, a daily digest), create the cron job with deliver set to gemini_voice:doc (a new "
    "Google Doc the owner can ask Gemini to read) or gemini_voice:event-HHMM (a calendar event at that local "
    "time, e.g. gemini_voice:event-0800), and say in your reply where the result will appear."
)

CLARIFY_BLOCK = (
    "clarify is not available on the Gemini voice channel: the owner cannot answer in the middle of a "
    "turn. Make a reasonable assumption, or ask your question in the final reply; the owner will answer "
    "with a new complete voice task, which starts a conversation of its own."
)


def _read_secret(name: str) -> Optional[str]:
    """The plugin's own credential by environment name, through Hermes's profile-scoped secret reader."""
    try:
        from agent.secret_scope import get_secret
        value = get_secret(name)
    except ImportError:
        value = os.environ.get(name)
    except Exception as exc:  # e.g. no secret scope while multiplexing: treat as absent, say why
        logger.warning("gemini-voice: cannot read %s: %s", name, type(exc).__name__)
        return None
    return str(value).strip() if value else None


def _is_connected(_config: Any = None) -> bool:
    """Configured once the Google client ID is in .env. The client secret and the grant stay Hermes's: a
    missing secret or sign-in shows up as the ``auth_failed`` status at the first poll."""
    return bool(_read_secret(ENV_CLIENT_ID))


def _env_enablement() -> Optional[Dict[str, Any]]:
    """A home channel only when the operator names one. Hermes sends its own notices (shutting down, back
    online, storage warnings) to every home channel, and here each would become a new Google object. Without
    GEMINI_VOICE_HOME_CHANNEL a bare ``deliver: gemini_voice`` has no target; gemini_voice:doc and
    gemini_voice:event-HHMM work either way."""
    if not _is_connected():
        return None
    home = (_read_secret(HOME_CHANNEL_ENV) or "").strip()
    if not home:
        return None
    if not valid_target(home):
        logger.warning("gemini-voice: %s=%r is not a delivery target (use doc or event-HHMM); using doc",
                       HOME_CHANNEL_ENV, home)
        home = "doc"
    return {"home_channel": {"chat_id": home, "name": "Google (new document)" if home == "doc" else f"Google {home}"}}


def _session_platform() -> str:
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env("HERMES_SESSION_PLATFORM", "") or "")
    except Exception:
        return ""


def _pre_tool_call(tool_name: str = "", **_: Any) -> Optional[Dict[str, str]]:
    """Only change to the agent's tools: ``clarify`` cannot be answered from the phone mid-turn."""
    if tool_name == "clarify" and _session_platform() == PLATFORM:
        return {"action": "block", "message": CLARIFY_BLOCK}
    return None


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    from .adapter import GeminiVoiceAdapter  # gateway imports stay inside Hermes

    def settings() -> Settings:
        return Settings.from_config(lambda key, default: ctx.get_config(key, default))

    def factory(config: Any) -> GeminiVoiceAdapter:
        return GeminiVoiceAdapter(config, settings=settings(), store=ctx.state, read_secret=_read_secret)

    async def standalone_send(pconfig: Any, chat_id: str, message: str, *, media_files: Any = None,
                              **_: Any) -> Dict[str, Any]:
        """Cron delivery when no gateway runs in this process. It never touches the gateway's journal
        (NullStore): output documents are recognised by their signature line, not by the journal. Text
        only, like the live adapter: with attachments nothing is written, so nothing is half-delivered."""
        from .google_api import SIGN_IN, GoogleClient
        if media_files:
            return {"error": f"gemini-voice: {TEXT_ONLY}"}
        if not _is_connected():
            return {"error": f"gemini-voice is not configured: set {ENV_CLIENT_ID} and run `{SIGN_IN}`"}

        async def _no_dispatch(_task: Task) -> None:
            raise RuntimeError("standalone delivery cannot start tasks")

        bridge = VoiceBridge(settings(), GoogleClient(), NullStore(),
                             _no_dispatch, lambda *_args: None)
        ok, ref = await bridge.deliver_output(str(chat_id), message)
        return {"success": True, "message_id": ref} if ok else {"error": ref}

    ctx.register_platform(
        name=PLATFORM, label="Gemini Voice (Google Calendar, Docs, Sheets)", adapter_factory=factory,
        check_fn=lambda: True, is_connected=_is_connected, required_env=list(ENV_NAMES),
        install_hint="Run `hermes auth add gemini-voice:google` once on the server", trusted_inbound=True,
        display_tier="minimal", platform_hint=PLATFORM_HINT, cron_deliver_env_var=HOME_CHANNEL_ENV,
        standalone_sender_fn=standalone_send, env_enablement_fn=_env_enablement, emoji="🎙",
        allow_update_command=False, max_message_length=0)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_cli_command(
        name="gemini-voice", help="Gemini voice channel: check, status",
        setup_fn=setup_parser, handler_fn=lambda args: handle(args, default_io(_read_secret), ctx.state),
        description="Verify the Google sign-in Hermes holds for the plugin and show the channel status.")
