"""Contract probe run in a child process against a real Hermes checkout (see test_engine_contract.py).

argv: <engine root> <plugin dir>. HERMES_HOME must point to an empty temporary directory.
GEMINI_VOICE_TEST_SYS_PATH (JSON list), when set, goes ahead of this interpreter's own path.
Prints one JSON object: {check name: [ok, detail]}.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path


def install(plugin_dir: Path, home: Path) -> None:
    target = home / "plugins" / "gemini-voice"
    shutil.copytree(plugin_dir, target, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    # A checkout of Hermes between releases reports the last release and would skip a plugin that needs the next
    # one before loading it. The probe tests the code against this checkout; route_checks tests the gate itself.
    manifest = target / "plugin.yaml"
    manifest.write_text(re.sub(r"(?m)^requires_hermes:.*\n", "", manifest.read_text(encoding="utf-8")),
                        encoding="utf-8")
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

    def holds_reply(self, content):
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
        seen["session"] = adapter._source_session_key(event.source)
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
    from gateway.run_shutdown import GatewayShutdownMixin
    outputs_before = list(stub.outputs)
    notice = run(GatewayShutdownMixin._send_notice_logged(  # how the gateway tells a home channel it stops
        adapter, "doc", "Hermes is shutting down", "gemini_voice", "shutdown notice to %s:%s failed: %s"))
    results["shutdown notice to a home channel never becomes a document"] = [
        notice is False and stub.outputs == outputs_before, repr(stub.outputs)]
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
    client = api.GoogleClient(transport=fake)

    async def no_dispatch(_task):
        raise RuntimeError("no new task in this check")
    bridge = bridge_mod.VoiceBridge(bridge_mod.Settings(), client, store, no_dispatch, lambda *_: None)

    async def scenario():
        adapter._bridge = bridge
        resume_end = MessageEvent(text="", source=adapter._owner_source(key), internal=True)
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


def ledger_checks(adapter_factory, package: str, home: Path) -> dict:
    """The engine's own delivery ledger around a real VoiceBridge: a final reply cut off while a poll held the
    bridge is still held after the restart (the boot re-send closes its row, the reply is written once), and
    a re-send of a reply nobody holds is refused for good."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from importlib import import_module
    from gateway import delivery_ledger as ledger
    from gateway.config import PlatformConfig
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_google import FakeGoogle
    bridge_mod, api = import_module(package + ".bridge"), import_module(package + ".google_api")
    session, targets = "agent:main:gemini_voice:dm:owner", {("gemini_voice", "default"), ("gemini_voice", None)}
    fake, store, now = FakeGoogle(), DictStore({}), [datetime.now(timezone.utc)]

    async def dispatch(_task):
        return None

    async def inline(fn, *args):
        return fn(*args)

    def process():
        adapter = adapter_factory(PlatformConfig(enabled=True))
        client = api.GoogleClient(transport=fake)
        adapter._bridge = bridge_mod.VoiceBridge(bridge_mod.Settings(labels=["Hermes"]), client, store, dispatch,
                                                 lambda *_: None, clock=lambda: now[0], run_blocking=inline)
        return adapter, adapter._bridge

    def owe(key, text):  # what the gateway records before it sends a final reply
        oid = ledger.compute_obligation_id(session, key, text)
        ledger.record_obligation(obligation_id=oid, session_key=session, platform="gemini_voice", chat_id="owner",
                                 thread_id=None, content=text)
        ledger.mark_attempting(oid)
        return oid

    def die(oid):  # the process that owned the row is gone: the next boot claims it
        with sqlite3.connect(home / "state.db") as conn:
            conn.execute("UPDATE delivery_obligations SET owner_pid=999999, owner_started_at=NULL "
                         "WHERE obligation_id=?", (oid,))

    def row_state(oid):
        with sqlite3.connect(home / "state.db") as conn:
            return conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id=?", (oid,)).fetchone()[0]

    async def boot_resend(adapter, oid):  # as _redeliver_claimed_obligations in gateway/run_startup.py
        row = next(r for r in ledger.sweep_recoverable(deliverable_platforms={"gemini_voice"},
                                                       deliverable_targets=targets) if r["obligation_id"] == oid)
        content = row.get("marker", ledger.RECOVERED_MARKER) + row["content"] if row.get("needs_marker") else row["content"]
        result = await adapter.send(chat_id=row["chat_id"], content=content, metadata=None)
        if result.success:
            ledger.mark_delivered(oid)
        else:
            ledger.mark_failed(oid, str(result.error or ""))
        return result

    async def scenario():
        adapter, bridge = process()
        await bridge.tick()
        now[0] += timedelta(minutes=1)
        doc = fake.add_doc("Hermes task A", "what is 2+2", created=now[0].isoformat())
        now[0] += timedelta(minutes=1)
        await bridge.tick()
        key = f"doc.{doc}"
        oid = owe(key, "A: four")
        await bridge._lock.acquire()  # a poll is in the middle of its Google calls
        send = asyncio.create_task(adapter.send("owner", "A: four", reply_to=key, metadata={"notify": True}))
        for _ in range(200):  # until the reply is in the plugin's journal and the send waits for the poll
            await asyncio.sleep(0.01)
            if (bridge.journal.get(key) or {}).get("state") == "answered":
                break
        journaled = (bridge.journal.get(key) or {}).get("state") == "answered" and not send.done()
        send.cancel()  # a graceful shutdown cancels the turn's send
        await asyncio.gather(send, return_exceptions=True)
        bridge._lock.release()
        die(oid)
        adapter, bridge = process()
        held = await boot_resend(adapter, oid)
        now[0] += timedelta(minutes=1)
        await bridge.tick()
        stray_oid = owe(key, "B: a reply nobody holds")
        die(stray_oid)
        stray = await boot_resend(adapter, stray_oid)
        return journaled, held, row_state(oid), stray, row_state(stray_oid), fake.doc_text(doc)
    journaled, held, held_row, stray, stray_row, text = asyncio.run(scenario())
    return {
        "ledger: a reply waiting behind a poll is journaled before it waits": [journaled, repr(journaled)],
        "ledger: a reply cut off behind a poll is held, its row closed, written once": [
            held.success and held_row == "delivered" and text.count("A: four") == 1, f"{held_row}: {text!r}"],
        "ledger: a re-send nobody holds is refused for good, nothing written": [
            refused(stray) and stray_row == "failed" and "nobody" not in text, f"{stray_row}: {stray.error}"],
    }


def enabled_platform(entry):
    """The platform's config after the gateway's enablement from env (credentials present, no YAML entry)."""
    from gateway.config import GatewayConfig, Platform
    from gateway.config_env import _enable_plugin_platform
    config = GatewayConfig()
    _enable_plugin_platform(config, entry)
    return config.platforms.get(Platform("gemini_voice"))


def cron_checks(entry, package: str) -> dict:
    """Delivery outside the gateway (the registered standalone sender, Google faked in memory): text into a new
    document or a slot event, attachments refused, the home channel must be a real target, and there is none
    unless the operator names one (gateway notices go to every home channel)."""
    from importlib import import_module
    from gateway.config import PlatformConfig
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_google import FakeGoogle
    api, fake = import_module(package + ".google_api"), FakeGoogle()
    real_client = api.GoogleClient
    api.GoogleClient = lambda *_, **__: real_client(transport=fake)
    names = {"GEMINI_VOICE_CLIENT_ID": "id", "GEMINI_VOICE_CLIENT_SECRET": "secret",
             "GEMINI_VOICE_HOME_CHANNEL": "event-2561"}
    saved = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    send = entry.standalone_sender_fn
    try:
        enablement = entry.env_enablement_fn()
        media = asyncio.run(send(PlatformConfig(enabled=True), "doc", "digest", media_files=[("/tmp/chart.png", False)]))
        doc = asyncio.run(send(PlatformConfig(enabled=True), "doc", "Radar 20.10\nT1: no switch yet"))
        event = asyncio.run(send(PlatformConfig(enabled=True), "event-0800", "Briefing\nmail: 3 letters"))
        os.environ.pop("GEMINI_VOICE_HOME_CHANNEL", None)
        bare = entry.env_enablement_fn()
        unnamed = enabled_platform(entry)  # the gateway's own enablement path, as at startup
        os.environ["GEMINI_VOICE_HOME_CHANNEL"] = "doc"
        named = enabled_platform(entry)
    finally:
        api.GoogleClient = real_client
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    doc_id = str(doc.get("message_id") or "").partition(".")[2]
    event_id = str(event.get("message_id") or "").partition(".")[2]
    doc_text = fake.docs.get(doc_id, {}).get("text", "")
    summary = fake.events.get(event_id, {}).get("summary")
    return {
        "cron home channel must be a target": [enablement["home_channel"]["chat_id"] == "doc", repr(enablement)],
        "no home channel unless named": [bare is None, repr(bare)],
        "gateway enables the platform without a home channel": [
            unnamed is not None and unnamed.enabled and unnamed.home_channel is None, repr(unnamed)],
        "a named home channel reaches the gateway": [
            named is not None and named.home_channel is not None and named.home_channel.chat_id == "doc", repr(named)],
        "cron outside the gateway refuses attachments": [
            str(media.get("error", "")).startswith("gemini-voice: files") and not media.get("success"), repr(media)],
        "cron outside the gateway writes a new document": [
            bool(doc.get("success")) and doc_text.startswith("[Hermes digest] Radar 20.10\nT1: no switch yet"),
            f"{doc} {doc_text!r}"],
        "cron outside the gateway writes a slot event": [
            bool(event.get("success")) and summary == "Briefing", f"{event} {summary!r}"],
    }


def cron_wrapper_checks(adapter, package: str) -> dict:
    """The real scheduler end to end (``_deliver_result``: the wrapper of ``cron.wrap_response``, on by default,
    media cleanup, secret redaction, target resolution and both send lanes); nothing in the engine is patched.
    Standalone: the plugin's own sender writes to Google faked in memory. Live: the gateway's adapter on a
    running loop hands the text to a stand-in bridge, and the plugin's strip is applied to exactly that text.
    The object must hold the job's output as redacted by the engine, never the wrapper."""
    import threading
    from contextlib import ExitStack
    from importlib import import_module
    from unittest import mock
    import cron.scheduler_delivery as delivery
    from agent.redact import redact_sensitive_text
    from gateway.config import Platform
    api, textutil = import_module(package + ".google_api"), import_module(package + ".textutil")
    split_title = import_module(package + ".bridge").split_title
    real_client = api.GoogleClient
    secret = "ghp_" + "Ab3" * 12
    output = ("Briefing\n\nmail: 3 letters, token " + secret + "\n"
              'To stop or manage this job, send me a new message (e.g. "x"). Quoted by the job itself')
    expected = redact_sensitive_text(output, force=True)  # what the engine lets out of a cron job
    title, body = split_title(textutil.plain_text(expected), "default")
    config_wrap = (delivery._sched.load_config() or {}).get("cron", {}).get("wrap_response", True)

    def job(target: str) -> dict:
        return {"id": "job1", "name": "Briefing job", "deliver": f"gemini_voice:{target}"}

    names = {"GEMINI_VOICE_CLIENT_ID": "id", "GEMINI_VOICE_CLIENT_SECRET": "secret"}
    stub, previous = StubBridge(), adapter._bridge
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(sys, "path", [str(Path(__file__).resolve().parent)] + sys.path))
        from fake_google import FakeGoogle
        fake = FakeGoogle()
        stack.enter_context(mock.patch.dict(os.environ, names))
        os.environ.pop("GEMINI_VOICE_HOME_CHANNEL", None)  # restored with the rest when the stack closes
        stack.enter_context(mock.patch.object(api, "GoogleClient", lambda *_, **__: real_client(transport=fake)))
        standalone_event = delivery._deliver_result(job("event-0800"), output)
        standalone_doc = delivery._deliver_result(job("doc"), output)
        loop = asyncio.new_event_loop()
        runner = threading.Thread(target=loop.run_forever, daemon=True)

        stopped: list = []

        def stop_loop() -> None:  # a loop still running after the wait is reported below, never closed
            if runner.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                runner.join(5)
            if not runner.is_alive():
                loop.close()
                stopped.append(True)

        stack.callback(stop_loop)
        runner.start()
        stack.callback(setattr, adapter, "_bridge", previous)
        adapter._bridge = stub
        live = delivery._deliver_result(job("event-0800"), output,
                                        adapters={Platform("gemini_voice"): adapter}, loop=loop)
    events = list(fake.events.values())
    docs = [doc_id for doc_id, doc in fake.docs.items() if doc.get("title")]
    sent = stub.outputs[0][1] if len(stub.outputs) == 1 else ""
    return {
        "probe config leaves cron.wrap_response on": [config_wrap is True, repr(config_wrap)],
        "engine redaction changes the probe output": [secret not in expected and expected != output, repr(expected)],
        "real cron run, standalone lane: the event is the redacted output": [
            standalone_event is None and len(events) == 1 and events[0].get("summary") == title
            and events[0].get("description") == body, f"{standalone_event!r} {title!r} {body!r} {events!r}"],
        "real cron run, standalone lane: the document is the redacted output": [
            standalone_doc is None and len(docs) == 1
            and fake.doc_text(docs[0]) == f"{textutil.OUTPUT_MARKER} {title}\n{body}\n",
            f"{standalone_doc!r} {[fake.doc_text(d) for d in docs]!r}"],
        "real cron run, live lane: the adapter gets the wrapped redacted output": [
            live is None and len(stub.outputs) == 1 and stub.outputs[0][0] == "event-0800"
            and sent.startswith(textutil.CRON_HEADER) and secret not in sent, f"{live!r} {stub.outputs!r}"],
        "real cron run, live lane: strip gives the redacted output back exactly": [
            textutil.strip_cron_wrapper(sent) == expected, repr(sent)],
        "real cron run, live lane: the loop stopped and closed": [stopped == [True], repr(stopped)],
    }


def busy_checks(adapter) -> dict:
    """When the voice queue must wait: the restore gate and a live turn; a guard left by a dead turn not."""
    from types import SimpleNamespace
    results: dict = {}
    key = adapter._source_session_key(adapter._owner_source("doc.busy"))
    adapter.gateway_runner = SimpleNamespace(_startup_restore_in_progress=True)
    gate = adapter._owner_session_busy()
    adapter.gateway_runner = None
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = SimpleNamespace(done=lambda: False)
    live = adapter._owner_session_busy()
    adapter._session_tasks[key] = SimpleNamespace(done=lambda: True)
    stale = adapter._owner_session_busy()
    healed = key not in adapter._active_sessions and key not in adapter._session_tasks
    adapter._active_sessions.pop(key, None)
    adapter._session_tasks.pop(key, None)
    results["restore gate holds new tasks"] = [gate is True, repr(gate)]
    results["running turn holds new tasks"] = [live is True, repr(live)]
    results["guard of an ended turn does not"] = [stale is False, repr(stale)]
    results["guard of an ended turn is cleared, not left behind"] = [healed, ""]
    return results


def _link(target: Path, link: Path) -> str:
    """A directory link the way a checkout is often installed (``ln -s``); a junction where Windows allows no symlink."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        import _winapi  # Windows only: a junction needs no privilege
        _winapi.CreateJunction(str(target), str(link))
        return "junction"


# The first Hermes release with the plugin OAuth route; set together with requires_hermes once it ships.
FIRST_ROUTE_RELEASE = "0.23.0"
EARLIER_RELEASES = ("0.21.6", "0.22.0", "0.22.1", "0.22.2")


def route_checks(package: str, home: Path, source_dir: Path) -> dict:
    """Hermes's plugin OAuth route around the plugin loaded by the real loader. Only the network is faked: Google
    and the token endpoint answer in memory. A grant is written the way ``hermes auth add`` writes it."""
    import importlib.util
    import time
    from contextlib import ExitStack
    from importlib import import_module
    from unittest import mock
    import httpx
    try:
        from hermes_cli import plugin_oauth
    except ImportError as exc:  # a release before NousResearch/hermes-agent#141510
        return {"route: this Hermes has the plugin OAuth route": [False, str(exc)]}
    from hermes_cli.plugins_manifest import version_satisfies
    from pm.plugin_declarations import read_native_manifest
    api = import_module(package + ".google_api")
    drive, docs = "https://www.googleapis.com/drive/v3/files", "https://docs.googleapis.com/v1/documents/d1:batchUpdate"
    manifest = read_native_manifest(source_dir / "plugin.yaml")
    clients, problems = plugin_oauth.parse_oauth_clients(manifest)
    problems += plugin_oauth.oauth_admission_problems(manifest, source_dir)
    warnings = plugin_oauth.oauth_admission_warnings(manifest, source_dir)  # the whole tree, tests included
    spec = str(manifest.get("requires_hermes") or "")
    installed = home / "plugins" / "gemini-voice"
    # The entry as written, even if Hermes refuses it: the grant then carries its real fingerprint.
    client = plugin_oauth._client(read_native_manifest(installed / "plugin.yaml")["oauth"][0])
    key = f"{plugin_oauth.plugin_key(installed)}:{client.id}"
    canary = "probe-client-secret-canary"
    sent: list = []
    google = {"status": 200}
    posts: list = []
    token = {"status": 200, "body": {}}
    real_client = httpx.Client

    def answer(request):
        sent.append(request)
        return httpx.Response(google["status"], json={"ok": True} if google["status"] == 200 else {"error": {}})

    def offline_client(*args, **kwargs):
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(answer)})

    def token_post(url, data=None, **kwargs):
        posts.append({"url": url, "data": data, "follow_redirects": kwargs.get("follow_redirects")})
        return httpx.Response(token["status"], json=token["body"], request=httpx.Request("POST", url))

    def grant(**row):
        plugin_oauth._write_grant(key, {"access_token": "probe-access", "refresh_token": "probe-refresh",
                                        "expires_at_ms": int(time.time() * 1000) + 3_600_000,
                                        "fingerprint": client.fingerprint, **row})

    def call(method="GET", url=drive, module=api, **kwargs):
        try:
            return module.GoogleClient().request(method, url, **kwargs)
        except Exception as exc:  # the plugin's own errors: GoogleError of whichever module sent the request
            return exc

    def step(**kwargs):  # one call of the plugin, and exactly what it made Hermes send to Google and to the token URL
        sent_before, posts_before = len(sent), len(posts)
        result = call(**kwargs)
        return result, sent[sent_before:], posts[posts_before:]

    def one_request(made, method, url, bearer):  # exactly one request to Google: this method, URL and token
        return (len(made) == 1 and made[0].method == method and str(made[0].url) == url
                and made[0].headers.get("authorization") == f"Bearer {bearer}")

    expired = {"expires_at_ms": int(time.time() * 1000) - 1000}
    env = {"GEMINI_VOICE_CLIENT_ID": "probe-client-id", "GEMINI_VOICE_CLIENT_SECRET": canary}
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(httpx, "Client", offline_client))
        stack.enter_context(mock.patch.object(httpx, "post", token_post))
        stack.enter_context(mock.patch.dict(os.environ, env))
        unsigned, unsigned_sent, unsigned_posts = step()
        grant()
        ok, ok_sent, ok_posts = step(method="POST", url=docs, body={"requests": [{"insertText": {"text": "Привет"}}]},
                                     headers={"If-Match": "e1"})
        off_list, off_sent, off_posts = step(url="https://example.com/x")
        google["status"] = 401
        rejected, rejected_sent, rejected_posts = step()
        google["status"] = 200
        grant(**expired)
        token.update(status=503, body={"error": "backend"})
        flaky, flaky_sent, flaky_posts = step()
        token.update(status=200, body={"access_token": "probe-access-2", "expires_in": 3600})
        refreshed, refreshed_sent, refresh_posts = step()
        kept_refresh = plugin_oauth._read_grant(key).get("refresh_token")
        grant(**expired)
        token.update(status=400, body={"error": "invalid_grant"})
        revoked, revoked_sent, revoked_posts = step()
        dead = (plugin_oauth._read_grant(key) or {}).get("dead")
        grant(**expired)
        os.environ.pop("GEMINI_VOICE_CLIENT_ID")
        no_client, no_client_sent, no_client_posts = step()
        os.environ["GEMINI_VOICE_CLIENT_ID"] = env["GEMINI_VOICE_CLIENT_ID"]
        link = home / "plugins" / "gemini-voice-link"
        link_kind = _link(source_dir, link)
        stack.callback(lambda: link.unlink() if link_kind == "symlink" else os.rmdir(link))
        spec_link = importlib.util.spec_from_file_location("gemini_voice_link_google_api", link / "google_api.py")
        linked = importlib.util.module_from_spec(spec_link)
        stack.enter_context(mock.patch.dict(sys.modules, {spec_link.name: linked}))  # dataclasses look it up
        spec_link.loader.exec_module(linked)
        link_key = f"{plugin_oauth.plugin_key(link)}:{client.id}"
        # The link's own key: no grant yet, whatever the other copy holds.
        link_unsigned, link_unsigned_sent, link_unsigned_posts = step(module=linked)
        plugin_oauth._write_grant(link_key, {"access_token": "probe-access-link", "refresh_token": "probe-refresh",
                                             "expires_at_ms": int(time.time() * 1000) + 3_600_000,
                                             "fingerprint": client.fingerprint})
        through_link, link_sent, link_posts = step(module=linked)
    texts = " ".join(str(x) for x in (unsigned, off_list, rejected, flaky, revoked, no_client, link_unsigned))
    floor = re.fullmatch(r"\s*>=\s*(\d+\.\d+\.\d+)\s*", spec)  # one plain floor, no pre-release or local suffix
    refresh_form = {"grant_type": "refresh_token", "refresh_token": "probe-refresh",
                    "client_id": env["GEMINI_VOICE_CLIENT_ID"], "client_secret": canary}

    def one_token_post(made):  # exactly one POST, to the declared token URL, no redirect followed, the exact form
        return (len(made) == 1 and made[0]["url"] == client.token_url and made[0]["follow_redirects"] is False
                and isinstance(made[0]["data"], dict) and made[0]["data"] == refresh_form)

    def nothing(*made):
        return all(len(m) == 0 for m in made)

    def code(result):
        return getattr(result, "code", repr(result))

    return {
        "route: Hermes admits the oauth: entry": [
            len(clients) == 1 and clients[0].id == api.OAUTH_ID and not problems, repr(problems)],
        "route: no reviewer warning, tests included": [bool(clients) and not problems and warnings == [],
                                                       repr(warnings or problems)],
        "route: requires_hermes starts at the first release with the route": [
            floor is not None and floor.group(1) == FIRST_ROUTE_RELEASE and version_satisfies(spec, FIRST_ROUTE_RELEASE)
            and not any(version_satisfies(spec, v) for v in EARLIER_RELEASES), f"{spec} vs {FIRST_ROUTE_RELEASE}"],
        "route: no sign-in, nothing sent, the command named": [
            isinstance(unsigned, api.SignInError) and unsigned.code == "not_signed_in" and api.SIGN_IN in str(unsigned)
            and nothing(unsigned_sent, unsigned_posts), str(unsigned)],
        "route: Hermes attaches the bearer, the plugin's body and headers arrive, once": [
            getattr(ok, "body", None) == {"ok": True} and one_request(ok_sent, "POST", docs, "probe-access")
            and ok_sent[0].headers.get("if-match") == "e1"
            and json.loads(ok_sent[0].content) == {"requests": [{"insertText": {"text": "Привет"}}]} and nothing(ok_posts),
            repr([(r.method, str(r.url), dict(r.headers)) for r in ok_sent])[:400]],
        "route: a host outside oauth: hosts is refused before sending": [
            code(off_list) == "route_refused" and "PermissionError" in str(off_list) and "example.com" in str(off_list)
            and nothing(off_sent, off_posts), str(off_list)],
        "route: a 401 is one request and an auth error": [
            code(rejected) == "unauthorized" and one_request(rejected_sent, "GET", drive, "probe-access")
            and nothing(rejected_posts), f"{len(rejected_sent)} {rejected}"],
        "route: a refresh that fails is retried later, Google not called": [
            isinstance(flaky, api.TransientError) and code(flaky) == "oauth_refresh_failed" and nothing(flaky_sent)
            and one_token_post(flaky_posts), f"{flaky} {[p['url'] for p in flaky_posts]}"],
        "route: Hermes refreshes with the client from .env and keeps the refresh token": [
            getattr(refreshed, "status", 0) == 200 and one_request(refreshed_sent, "GET", drive, "probe-access-2")
            and kept_refresh == "probe-refresh" and one_token_post(refresh_posts),
            f"sent {len(refreshed_sent)} {[(p['url'], repr(p.get('data'))[:80]) for p in refresh_posts]}"],
        "route: a refused refresh is a revoked sign-in": [
            code(revoked) == "not_signed_in" and "revoked" in str(revoked) and dead == "invalid_grant"
            and nothing(revoked_sent) and one_token_post(revoked_posts), str(revoked)],
        "route: a missing client ID needs the operator": [
            isinstance(no_client, api.SignInError) and code(no_client) == "oauth_client_id_missing"
            and nothing(no_client_sent, no_client_posts), str(no_client)],
        "route: no secret or token in the plugin's errors": [
            all(s not in texts for s in (canary, "probe-access", "probe-refresh")), texts[:300]],
        "route: a plugin installed through a link is the caller, under its own key": [
            code(link_unsigned) == "not_signed_in" and "'gemini-voice-link'" in str(link_unsigned)
            and nothing(link_unsigned_sent, link_unsigned_posts)
            and getattr(through_link, "status", 0) == 200 and one_request(link_sent, "GET", drive, "probe-access-link")
            and nothing(link_posts), f"{link_kind}: {link_unsigned} | {[str(r.url) for r in link_sent]}"],
    }


def probe(plugin_dir: Path) -> dict:
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
    one, other = (adapter._source_session_key(adapter._owner_source(k)) for k in ("doc.a", "cal.b"))
    results["one session per task object"] = [
        one == "agent:main:gemini_voice:dm:owner:doc.a" and other == "agent:main:gemini_voice:dm:owner:cal.b",
        f"{one} {other}"]
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
    media = asyncio.run(full_turn(adapter, media_stub, task_cls("doc.turn2", "doc", "t", "chart please"),
                                  reply="Here it is ![chart](https://x.example/chart.png)"))
    results["attachment after the reply never replaces it"] = [
        media_stub.answers == [("doc.turn2", "Here it is")], repr(media_stub.answers)]
    results["two tasks in a row, two sessions"] = [
        seen.get("session") == "agent:main:gemini_voice:dm:owner:doc.turn1"
        and media.get("session") == "agent:main:gemini_voice:dm:owner:doc.turn2", f"{seen.get('session')} {media.get('session')}"]
    results.update(stateful_checks(adapter, type(adapter).__module__.rsplit(".", 1)[0]))
    results.update(ledger_checks(entry.adapter_factory, type(adapter).__module__.rsplit(".", 1)[0],
                                 Path(os.environ["HERMES_HOME"])))
    results.update(cron_checks(entry, type(adapter).__module__.rsplit(".", 1)[0]))
    results.update(cron_wrapper_checks(adapter, type(adapter).__module__.rsplit(".", 1)[0]))
    results.update(route_checks(type(adapter).__module__.rsplit(".", 1)[0], Path(os.environ["HERMES_HOME"]),
                                plugin_dir.resolve()))

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
    inherited = json.loads(os.environ.get("GEMINI_VOICE_TEST_SYS_PATH") or "[]")
    sys.path[:] = [*inherited, *(entry for entry in sys.path if entry not in inherited)]
    sys.path.insert(0, str(engine_root))
    install(plugin_dir, Path(os.environ["HERMES_HOME"]))
    print(json.dumps(probe(plugin_dir), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
