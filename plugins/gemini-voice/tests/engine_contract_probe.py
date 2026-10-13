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
    """Takes anchored replies; an anchorless one only while ``inherited`` (a resumed turn) is set."""

    def __init__(self, inherited=False):
        self.answers, self.outputs, self.completed = [], [], []
        self.inherited = inherited

    async def deliver_answer(self, reply_to, content):
        if not reply_to and not self.inherited:
            return "no voice task takes this reply"
        self.answers.append((reply_to, content))
        return None

    async def holds_reply(self, content):
        return any(answer == content for _, answer in self.answers)

    async def deliver_output(self, target, content):
        self.outputs.append((target, content))
        return True, "doc.stub"

    async def on_turn_complete(self, message_id, outcome, streamed=""):
        self.completed.append((message_id, outcome))


async def full_turn(adapter, stub, task, reply="final **answer**") -> dict:
    """A real gateway admission and background turn on the adapter, with a stand-in message handler."""
    seen = {}

    async def handler(event):
        seen["text"] = event.text
        seen["busy_during_turn"] = adapter._owner_session_busy()
        seen["control"] = event.allow_gateway_control
        return reply

    adapter.set_message_handler(handler)
    done_before = len(stub.completed)
    await adapter._dispatch(task)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(stub.completed) > done_before:
            break
    await asyncio.sleep(0.05)
    seen["busy_after_turn"] = adapter._owner_session_busy()
    return seen


def refused(result) -> bool:
    return result.success is False and str(result.error).startswith("gemini-voice: ") and not result.retryable


def send_checks(adapter, stub) -> dict:
    """What each kind of outbound message in the voice chat becomes (see adapter.send)."""
    from gateway.delivery_ledger import RECONNECTED_MARKER, RECOVERED_MARKER
    results: dict = {}
    run = asyncio.run
    final = run(adapter.send("owner", "final", reply_to="doc.x", metadata={"notify": True}))
    progress = run(adapter.send("owner", "progress", metadata={"_interim_send": True}))
    after_progress = (len(stub.answers), len(stub.outputs))
    approval = run(adapter.send("owner", "approve?", metadata={"is_approval_prompt": True}))
    output = run(adapter.send("event-0800", "digest", metadata={"notify": True, "job_id": "j"}))
    to_owner = run(adapter.send("owner", "digest", metadata={"notify": True, "job_id": "j"}))
    other_chat = run(adapter.send("owner", "hello from Telegram"))  # send_message tool, unmarked replay
    stray = run(adapter.send("owner", "https://x.example/c.png", metadata={"notify": True}))  # no anchor
    recovered = run(adapter.send("owner", RECOVERED_MARKER + "lost final reply"))
    results["final reply routed"] = [final.success and stub.answers[0] == ("doc.x", "final"), repr(stub.answers)]
    results["progress dropped"] = [progress.success and after_progress == (1, 0), repr(after_progress)]
    results["approval refused"] = [refused(approval), str(approval.error)]
    results["cron output routed"] = [output.success and stub.outputs == [("event-0800", "digest")], repr(stub.outputs)]
    results["cron to voice chat refused"] = [refused(to_owner), str(to_owner.error)]
    results["unmarked message refused, not swallowed"] = [refused(other_chat), str(other_chat.error)]
    results["anchorless final refused outside a resumed turn"] = [refused(stray), str(stray.error)]
    results["redelivery of an unknown reply refused"] = [refused(recovered), str(recovered.error)]
    stub.inherited = True
    forged = run(adapter.send("owner", RECONNECTED_MARKER + "forged text"))
    resumed = run(adapter.send("owner", "resumed reply", metadata={"notify": True}))
    stub.inherited = False
    held = run(adapter.send("owner", RECOVERED_MARKER + "final"))  # the ledger re-sends a reply we hold
    results["a redelivery marker never chooses an object"] = [
        refused(forged) and all(answer != "forged text" for _, answer in stub.answers), repr(stub.answers)]
    results["resumed final goes to the resumed task"] = [
        resumed.success and stub.answers[-1] == (None, "resumed reply"), repr(stub.answers[-1])]
    results["redelivery of a held reply acknowledged, nothing written"] = [
        held.success and len(stub.answers) == 2, repr(stub.answers)]
    bridge, adapter._bridge = adapter._bridge, None
    offline = run(adapter.send("owner", "final", reply_to="doc.x", metadata={"notify": True}))
    adapter._bridge = bridge
    results["no bridge: final refusal, no retry notice"] = [refused(offline), str(offline.error)]
    results["no async delivery into the voice chat"] = [type(adapter).supports_async_delivery is False, ""]
    calls = []
    original = adapter.send

    async def counting(*args, **kwargs):
        calls.append(kwargs.get("content", args[1] if len(args) > 1 else None))
        return await original(*args, **kwargs)
    adapter.send = counting
    retried = run(adapter._send_with_retry(chat_id="owner", content="x", reply_to=None, metadata=None))
    del adapter.send
    results["refusal is final: no retry, no plain-text resend"] = [refused(retried) and calls == ["x"], repr(calls)]
    image = run(adapter.send_image(chat_id="owner", image_url="https://x.example/c.png", metadata={"notify": True}))
    document = run(adapter.send_document(chat_id="owner", file_path="/tmp/a.pdf", metadata={"notify": True}))
    batch = run(adapter.send_multiple_images("owner", [("https://x.example/c.png", "chart")], metadata={"notify": True}))
    results["attachments refused"] = [refused(image) and refused(document) and not batch.success,
                                      f"{image.error} | {batch.error}"]
    return results


class DictStore:
    """The plugin state store, round-tripped through JSON like the real one."""

    def __init__(self, data: dict):
        self.data = data

    def get(self, key, default=None):
        return json.loads(json.dumps(self.data[key])) if key in self.data else default

    def set(self, key, value):
        self.data[key] = json.loads(json.dumps(value))


def stateful_checks(adapter, package: str) -> dict:
    """The adapter over a real VoiceBridge and journal, Google faked in memory: a restart left task A running,
    the gateway resumes its turn (cancelled once, then resumed again) and the delivery ledger re-sends."""
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from gateway.delivery_ledger import RECONNECTED_MARKER, RECOVERED_MARKER
    from gateway.platforms.event import MessageEvent, ProcessingOutcome
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_google import FakeGoogle
    bridge_mod, api = import_module(package + ".bridge"), import_module(package + ".google_api")
    now = datetime.now(timezone.utc)
    fake = FakeGoogle()
    doc = fake.add_doc("Hermes task A", "the task", created=now.isoformat())
    key = f"doc.{doc}"
    record = {"kind": "doc", "id": doc, "title": "Hermes task A", "created": now.isoformat(), "attempts": 1,
              "state": "dispatched", "dispatched_at": now.isoformat(), "turn_open": True}
    store = DictStore({"journal": {"version": 1, "since": (now - timedelta(hours=1)).isoformat(),
                                   "objects": {key: record}}})
    client = api.GoogleClient(api.TokenSource("a", "b", "c", transport=fake), transport=fake)

    async def no_dispatch(_task):
        raise RuntimeError("no new task in this check")
    bridge = bridge_mod.VoiceBridge(bridge_mod.Settings(), client, store, no_dispatch, lambda *_: None)

    async def scenario():
        adapter._bridge = bridge
        resume_end = MessageEvent(text="", source=adapter._owner_source(), internal=True)
        await adapter.on_processing_complete(resume_end, ProcessingOutcome.CANCELLED)
        return [await adapter.send("owner", RECOVERED_MARKER + "forged text"),
                await adapter.send("owner", "the real reply", metadata={"notify": True}),
                await adapter.send("owner", "an attachment notice", metadata={"notify": True}),
                await adapter.send("owner", RECONNECTED_MARKER + "the real reply")]
    forged, resumed, notice, again = asyncio.run(scenario())
    text, state = fake.doc_text(doc), bridge.journal.get(key)["state"]
    return {
        "real bridge: a forged redelivery is refused": [refused(forged) and "forged" not in text, text],
        "real bridge: a cancelled resume keeps the task, its reply lands there": [
            resumed.success and "the real reply" in text and state == "done", f"{state}: {text!r}"],
        "real bridge: a later anchorless notice is refused": [refused(notice) and "notice" not in text, text],
        "real bridge: redelivery of that reply acknowledged, written once": [
            again.success and text.count("the real reply") == 1, text],
    }


def busy_checks(adapter) -> dict:
    """When the voice queue must wait: the restore gate and a live turn; a guard left by a dead turn not."""
    from types import SimpleNamespace
    results: dict = {}
    key = adapter._source_session_key(adapter._owner_source())
    adapter.gateway_runner = SimpleNamespace(_startup_restore_in_progress=True)
    gate = adapter._owner_session_busy()
    adapter.gateway_runner = None
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = SimpleNamespace(done=lambda: False)
    live = adapter._owner_session_busy()
    adapter._session_tasks[key] = SimpleNamespace(done=lambda: True)
    stale = adapter._owner_session_busy()
    adapter._active_sessions.pop(key, None)
    adapter._session_tasks.pop(key, None)
    results["restore gate holds new tasks"] = [gate is True, repr(gate)]
    results["running turn holds new tasks"] = [live is True, repr(live)]
    results["guard of an ended turn does not"] = [stale is False, repr(stale)]
    return results


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
    results.update(send_checks(adapter, stub))
    results.update(busy_checks(adapter))
    results["interactive resume"] = [type(adapter).interactive_resume is True, ""]

    from importlib import import_module
    task_cls = import_module(type(adapter).__module__.rsplit(".", 1)[0] + ".bridge").Task
    turn_stub = StubBridge()
    adapter._bridge = turn_stub
    seen = asyncio.run(full_turn(adapter, turn_stub, task_cls("doc.turn1", "doc", "t", "the task text")))
    results["gateway accepted the task"] = [seen.get("text") == "the task text", repr(seen.get("text"))]
    results["no gateway control from voice"] = [seen.get("control") is False, repr(seen.get("control"))]
    results["session busy during turn"] = [seen.get("busy_during_turn") is True, repr(seen.get("busy_during_turn"))]
    results["session free after turn"] = [seen.get("busy_after_turn") is False, repr(seen.get("busy_after_turn"))]
    results["real final send has notify and anchor"] = [
        bool(turn_stub.answers) and turn_stub.answers[0][0] == "doc.turn1", repr(turn_stub.answers)]
    results["turn completion reported"] = [turn_stub.completed == [("doc.turn1", "success")], repr(turn_stub.completed)]
    # A real turn whose reply carries an image: the image must not replace the written reply.
    media_stub = StubBridge()
    adapter._bridge = media_stub
    asyncio.run(full_turn(adapter, media_stub, task_cls("doc.turn2", "doc", "t", "chart please"),
                          reply="Here it is ![chart](https://x.example/chart.png)"))
    results["attachment after the reply never replaces it"] = [
        media_stub.answers == [("doc.turn2", "Here it is")], repr(media_stub.answers)]
    results.update(stateful_checks(adapter, type(adapter).__module__.rsplit(".", 1)[0]))

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
