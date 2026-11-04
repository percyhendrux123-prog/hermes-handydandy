"""Readiness must be refreshed after old-session work, including queued follow-ups."""
import os
from pathlib import Path

import pytest
from test_carryover import Event, Gateway, SUMMARY, dispatch, ready
from test_core import core, turn


@pytest.mark.parametrize("save_failure", [False, True])
def test_deferred_work_requires_new_file_then_can_rotate(tmp_path, monkeypatch, save_failure):
    engine, settings, clock = ready(tmp_path)
    gateway = Gateway(engine)
    hp = Path(engine.sessions["old"]["handoff_path"])
    previous_ns = hp.stat().st_mtime_ns
    save = engine._save
    if save_failure:
        monkeypatch.setattr(engine, "_save", lambda: False)
    gateway.store.entry.active_turn_token = "demo-active"
    assert dispatch(engine, gateway, Event(text="Make the widget blue")) is None
    monkeypatch.setattr(engine, "_save", save)
    assert turn(engine, "old", 121000, 14, platform="telegram", text="The widget is blue") is None
    gateway.store.entry.active_turn_token = None
    dispatch(engine, gateway, Event(text="Continue"))
    assert gateway.resets == 0
    assert engine.on_pre_llm_call(session_id="old")
    hp.write_text(SUMMARY.replace("Demo widget", "Blue demo widget"))
    os.utime(hp, ns=(previous_ns + 1000000, previous_ns + 1000000))
    assert "Handoff saved" in turn(engine, "old", 121000, 16, platform="telegram")
    dispatch(engine, gateway, Event(text="Now continue"))
    assert gateway.resets == 1
    assert "Blue demo widget" in engine.on_pre_llm_call(session_id="new-1")["context"]


def test_queued_followup_invalidates_ready_seed_durably(tmp_path):
    engine, settings, clock = ready(tmp_path)
    # Recursive queued turns do not pass ingress again, so output must invalidate too.
    assert turn(engine, "old", 121000, 14, platform="telegram", text="Extra work completed") is None
    restarted = core.Engine(lambda: settings, engine.data_dir, engine.hermes_home,
                            lambda: "default", clock=clock)
    gateway = Gateway(restarted)
    dispatch(restarted, gateway, Event())
    assert gateway.resets == 0
    assert restarted.on_pre_llm_call(session_id="old")
