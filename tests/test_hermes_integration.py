"""Opt-in integration against a caller-provided Hermes checkout and timer plugin.

Run with HERMES_COMPAT_ROOT and RESET_POLICY_ROOT in an isolated HERMES_HOME.
No model calls or platform transport are involved. Actual Hermes ingress, reset
funnel, routing store and lifecycle dispatcher are used; UI/runtime services are fakes.
"""
import asyncio
import importlib.util
import os
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_carryover import SUMMARY
from test_core import make, turn

pytestmark = pytest.mark.skipif(not os.environ.get("HERMES_COMPAT_ROOT"),
                                reason="set HERMES_COMPAT_ROOT for isolated Hermes integration")


@pytest.mark.parametrize("timer_first", [False, True])
def test_real_hermes_reset_and_timer_plugin(tmp_path, monkeypatch, timer_first):
    # Skip CLI dependency-provisioning at import. Gateway methods and SQLite below
    # remain real; the only bootstrap function imported by the agent is not called.
    monkeypatch.setitem(sys.modules, "hermes_bootstrap", SimpleNamespace(
        _happy_eyeballs_create_connection=socket.create_connection))
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import AsyncSessionStore, SessionSource, SessionStore
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    from hermes_cli import lifecycle

    class Runtime(GatewayInboundMixin, GatewaySessionCommandsMixin):
        def _session_key_for_source(self, source):
            return self.session_store._generate_session_key(source)

    root = Path(os.environ["HERMES_COMPAT_ROOT"]).resolve()
    import gateway.session
    assert Path(gateway.session.__file__).resolve().is_relative_to(root)
    engine, settings, clock = make(tmp_path, mode="carryover")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="demo-chat", user_id="demo-user")
    event = MessageEvent(text="Continue the demo.", source=source, message_id="demo-1",
                         media_urls=["demo-attachment"], reply_to_message_id="demo-anchor")
    config = GatewayConfig()
    config.multiplex_profiles = False
    store = SessionStore(tmp_path / "sessions", config)
    original = store.get_or_create_session(source)
    sid = original.session_id
    turn(engine, sid, 120_000, 10, platform="telegram")
    assert engine.on_pre_llm_call(session_id=sid)
    hp = Path(engine.sessions[sid]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text(SUMMARY)
    turn(engine, sid, 121_000, 12, platform="telegram")
    # Make both thresholds due on the same incoming event.
    original.created_at = original.updated_at = datetime.now() - timedelta(hours=2)

    runner = Runtime()
    runner.config = config
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner._session_db = None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    effects = []
    for method in ("_invalidate_session_run_generation", "_release_running_agent_state",
                   "_evict_cached_agent", "_clear_conversation_scope"):
        monkeypatch.setattr(runner, method, lambda *a, _m=method, **k: effects.append(_m), raising=False)
    monkeypatch.setattr(runner, "_cleanup_old_agent_for_reset", AsyncMock(), raising=False)
    if hasattr(runner, "_hm_stop_owner_turn"):
        monkeypatch.setattr(runner, "_hm_stop_owner_turn", AsyncMock())
    monkeypatch.setattr(runner, "_reset_notice_session_info", lambda *a: "", raising=False)
    monkeypatch.setattr(runner, "_telegram_topic_new_header", lambda *a: "", raising=False)
    monkeypatch.setattr(runner, "_is_telegram_topic_lane", lambda *a: False, raising=False)
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *a, **k: True, raising=False)

    timer_path = Path(os.environ["RESET_POLICY_ROOT"]) / "__init__.py"
    spec = importlib.util.spec_from_file_location("a213_timer", timer_path)
    timer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timer)
    monkeypatch.setattr(timer, "_read_session_reset_policy", lambda profile: {
        "mode": "idle", "idle_minutes": 60, "carry_over": {"enabled": False}})

    def lifecycle_hook(name, **kw):
        if name == "on_session_reset":
            engine.on_session_reset(**kw)
        if name == "pre_llm_call":
            return [engine.on_pre_llm_call(**kw)]
        return []

    async def ingress_hooks(name, **kw):
        assert name == "pre_gateway_dispatch"
        callbacks = [timer._maybe_reset, engine.on_pre_gateway_dispatch]
        if not timer_first:
            callbacks.reverse()
        return [await fn(**kw) for fn in callbacks]

    monkeypatch.setattr(lifecycle, "invoke_hook", lifecycle_hook)
    monkeypatch.setattr(lifecycle, "ainvoke_hook", ingress_hooks)
    result = asyncio.run(runner._hm_pre_gateway_dispatch_hook(event, source))
    assert result is event and event.text == "Continue the demo."
    assert event.media_urls == ["demo-attachment"] and event.reply_to_message_id == "demo-anchor"
    key = runner._session_key_for_source(source)
    fresh = store.lookup_by_session_key(key)
    assert fresh.session_id != sid
    assert effects.count("_evict_cached_agent") == 1
    assert effects.count("_clear_conversation_scope") == 1
    from agent.turn_context import _collect_pre_llm_call_context
    agent = SimpleNamespace(session_id=fresh.session_id, model="demo-model", platform="telegram")
    seed = _collect_pre_llm_call_context(
        agent, effective_task_id="demo-task", turn_id="demo-turn", original_user_message=event.text,
        messages=[{"role": "user", "content": event.text}], conversation_history=[])
    assert "Run the demo tests" in seed
    assert len(seed) < 7000
    # Exercise normal dispatch consumption: exactly the original event text in fresh storage.
    store.append_to_transcript(fresh.session_id, {"role": "user", "content": result.text})
    transcript = store.load_transcript(fresh.session_id)
    assert [m["content"] for m in transcript if m.get("role") == "user"] == [event.text]
    again = asyncio.run(runner._hm_pre_gateway_dispatch_hook(event, source))
    assert again is event
    assert store.lookup_by_session_key(key).session_id == fresh.session_id
    assert effects.count("_evict_cached_agent") == 1
