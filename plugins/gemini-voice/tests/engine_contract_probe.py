"""Contract probe run in a child process against a real Hermes checkout (see test_engine_contract.py).

argv: <engine root> <plugin dir>. HERMES_HOME must point to an empty temporary directory.
Prints one JSON object: {check name: [ok, detail]}.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


def install(plugin_dir: Path, home: Path) -> None:
    shutil.copytree(plugin_dir, home / "plugins" / "gemini-voice",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - gemini-voice\n", encoding="utf-8")


class StubBridge:
    def __init__(self):
        self.answers, self.outputs, self.completed = [], [], []

    async def deliver_answer(self, reply_to, content):
        self.answers.append((reply_to, content))
        return True

    async def deliver_output(self, target, content):
        self.outputs.append((target, content))
        return True, "doc.stub"

    async def on_turn_complete(self, message_id, outcome, streamed=""):
        self.completed.append((message_id, outcome))


async def full_turn(adapter, stub, task_cls) -> dict:
    """A real gateway admission and background turn on the adapter, with a stand-in message handler."""
    seen = {}

    async def handler(event):
        seen["text"] = event.text
        seen["busy_during_turn"] = adapter._owner_session_busy()
        seen["control"] = event.allow_gateway_control
        return "final **answer**"

    adapter.set_message_handler(handler)
    await adapter._dispatch(task_cls("doc.turn1", "doc", "t", "the task text"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if stub.completed:
            break
    await asyncio.sleep(0.05)
    seen["busy_after_turn"] = adapter._owner_session_busy()
    return seen


def probe() -> dict:
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, get_pre_tool_call_directive
    from gateway.platform_registry import platform_registry
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, _reply_anchor_for_event
    from gateway.platforms.event import MessageEvent
    from gateway.session import build_session_key
    from gateway.delivery import DeliveryTarget
    from gateway.session_context import clear_session_vars, set_session_vars

    discover_plugins()
    results: dict = {}
    entry = platform_registry.get("gemini_voice")
    results["platform registered"] = [entry is not None, repr(getattr(entry, "label", None))]
    results["trusted inbound"] = [bool(getattr(entry, "trusted_inbound", False)), ""]
    results["display tier minimal"] = [getattr(entry, "display_tier", "") == "minimal", getattr(entry, "display_tier", "")]
    results["cron home env"] = [getattr(entry, "cron_deliver_env_var", "") == "GEMINI_VOICE_HOME_CHANNEL", ""]
    results["platform enum"] = [Platform("gemini_voice").value == "gemini_voice", ""]
    manager = get_plugin_manager()
    results["cli command"] = ["gemini-voice" in manager._cli_commands, ""]

    adapter = entry.adapter_factory(PlatformConfig(enabled=True))
    results["adapter class"] = [isinstance(adapter, BasePlatformAdapter), type(adapter).__name__]
    source = adapter.build_source(chat_id="owner", chat_type="dm", user_id="owner", message_id="doc.x")
    key = build_session_key(source)
    results["session key"] = [key == "agent:main:gemini_voice:dm:owner", key]
    event = MessageEvent(text="t", source=source, message_id="doc.x", allow_gateway_control=False)
    results["reply anchor"] = [_reply_anchor_for_event(event) == "doc.x", str(_reply_anchor_for_event(event))]
    target = DeliveryTarget.parse("gemini_voice:event-0800")
    results["delivery target"] = [target.platform.value == "gemini_voice" and target.chat_id == "event-0800", ""]

    stub = StubBridge()
    adapter._bridge = stub
    final = asyncio.run(adapter.send("owner", "final", reply_to="doc.x", metadata={"notify": True}))
    progress = asyncio.run(adapter.send("owner", "progress", metadata={"_interim_send": True}))
    after_progress = (len(stub.answers), len(stub.outputs))
    approval = asyncio.run(adapter.send("owner", "approve?", metadata={"is_approval_prompt": True}))
    output = asyncio.run(adapter.send("event-0800", "digest", metadata={"notify": True, "job_id": "j"}))
    to_owner = asyncio.run(adapter.send("owner", "digest", metadata={"notify": True, "job_id": "j"}))
    from gateway.delivery_ledger import RECOVERED_MARKER
    asyncio.run(adapter.send("owner", RECOVERED_MARKER + "lost final reply"))
    results["final reply routed"] = [final.success and stub.answers[0] == ("doc.x", "final"), repr(stub.answers)]
    results["progress dropped"] = [progress.success and after_progress == (1, 0), repr(after_progress)]
    results["approval refused"] = [approval.success is False, str(approval.error)]
    results["cron output routed"] = [output.success and stub.outputs == [("event-0800", "digest")], repr(stub.outputs)]
    results["cron to voice chat refused"] = [to_owner.success is False, str(to_owner.error)]
    results["recovered reply routed"] = [stub.answers[-1] == (None, "lost final reply"), repr(stub.answers[-1])]
    results["interactive resume"] = [type(adapter).interactive_resume is True, ""]

    from importlib import import_module
    bridge_module = import_module(type(adapter).__module__.rsplit(".", 1)[0] + ".bridge")
    turn_stub = StubBridge()
    adapter._bridge = turn_stub
    seen = asyncio.run(full_turn(adapter, turn_stub, bridge_module.Task))
    results["gateway accepted the task"] = [seen.get("text") == "the task text", repr(seen.get("text"))]
    results["no gateway control from voice"] = [seen.get("control") is False, repr(seen.get("control"))]
    results["session busy during turn"] = [seen.get("busy_during_turn") is True, repr(seen.get("busy_during_turn"))]
    results["session free after turn"] = [seen.get("busy_after_turn") is False, repr(seen.get("busy_after_turn"))]
    results["real final send has notify and anchor"] = [
        bool(turn_stub.answers) and turn_stub.answers[0][0] == "doc.turn1", repr(turn_stub.answers)]
    results["turn completion reported"] = [turn_stub.completed == [("doc.turn1", "success")], repr(turn_stub.completed)]

    tokens = set_session_vars(platform="gemini_voice")
    try:
        voice = get_pre_tool_call_directive("clarify", {})
        other_tool = get_pre_tool_call_directive("web_search", {})
    finally:
        clear_session_vars(tokens)
    tokens = set_session_vars(platform="telegram")
    try:
        telegram = get_pre_tool_call_directive("clarify", {})
    finally:
        clear_session_vars(tokens)
    results["clarify blocked on voice"] = [voice[0] == "block", str(voice[0])]
    results["other tools untouched"] = [other_tool[0] is None, str(other_tool[0])]
    results["clarify allowed elsewhere"] = [telegram[0] is None, str(telegram[0])]
    return results


def main() -> int:
    engine_root, plugin_dir = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(engine_root))
    install(plugin_dir, Path(os.environ["HERMES_HOME"]))
    print(json.dumps(probe(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
