import asyncio
from pathlib import Path

import pytest
from test_carryover import Event, Gateway, SUMMARY, dispatch, ready
from test_core import core, make, turn


@pytest.mark.parametrize("case", ["active", "unauthorized", "other-profile", "internal", "status", "no-helper"])
def test_guards_preserve_the_inbound(tmp_path, case):
    engine, settings, clock = ready(tmp_path)
    gateway = Gateway(engine)
    event = Event()
    if case == "active":
        gateway.store.entry.active_turn_token = "synthetic-turn"
    elif case == "unauthorized":
        gateway.authorized = False
    elif case == "other-profile":
        event.source.profile = "another-profile"
    elif case == "internal":
        event.internal = True
    elif case == "status":
        event.text = "/status"
    elif case == "no-helper":
        gateway._handle_reset_command = None
    assert dispatch(engine, gateway, event) is None
    assert gateway.resets == 0


def test_explicit_new_does_not_inherit_a_ready_handoff(tmp_path):
    engine, *_ = ready(tmp_path)
    gateway = Gateway(engine)
    assert dispatch(engine, gateway, Event(text="/new")) is None
    assert gateway.resets == 0
    engine.on_session_reset(old_session_id="old", session_id="manual-new", platform="telegram")
    assert engine.on_pre_llm_call(session_id="manual-new") is None


@pytest.mark.parametrize("body", ["# not structured", SUMMARY + "x" * core.SEED_MAX_CHARS,
                                  "\n".join("## " + section for section in core.HANDOFF_SECTIONS)])
def test_invalid_handoff_never_rotates(tmp_path, body):
    engine, *_ = make(tmp_path, mode="carryover")
    turn(engine, "old", 120_000, 10, platform="telegram")
    gateway = Gateway(engine)
    dispatch(engine, gateway, Event())
    assert gateway.resets == 0
    hp = Path(engine.sessions["old"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text(body)
    assert turn(engine, "old", 121_000, 12, platform="telegram") is None
    assert engine.on_pre_llm_call(session_id="old")
    dispatch(engine, gateway, Event())
    assert gateway.resets == 0


def test_concurrent_ingress_and_engine_reload_reset_once(tmp_path):
    engine, settings, clock = ready(tmp_path)
    gateway = Gateway(engine)
    second = core.Engine(lambda: settings, engine.data_dir, engine.hermes_home,
                         lambda: "default", clock=clock)
    original = gateway._handle_reset_command

    async def slow_reset(event):
        await asyncio.sleep(0)
        return await original(event)

    gateway._handle_reset_command = slow_reset

    async def both():
        return await asyncio.gather(
            engine.on_pre_gateway_dispatch(Event(), gateway, gateway.store),
            second.on_pre_gateway_dispatch(Event(message_id="demo-2"), gateway, gateway.store))

    assert asyncio.run(both()) == [None, None]
    assert gateway.resets == 1
    restarted = core.Engine(lambda: settings, engine.data_dir, engine.hermes_home,
                            lambda: "default", clock=clock)
    assert restarted.on_pre_llm_call(session_id="new-1")
    dispatch(restarted, gateway, Event(message_id="demo-3"))
    assert gateway.resets == 1


@pytest.mark.parametrize("after_rotation", [False, True])
def test_reset_exception_never_consumes_or_double_resets(tmp_path, after_rotation):
    engine, *_ = ready(tmp_path)
    gateway = Gateway(engine)
    original = gateway._handle_reset_command

    async def failing(event):
        if after_rotation:
            await original(event)
        raise OSError("synthetic reset failure")

    gateway._handle_reset_command = failing
    assert dispatch(engine, gateway, Event()) is None
    gateway._handle_reset_command = original
    assert dispatch(engine, gateway, Event(message_id="demo-2")) is None
    assert gateway.resets == 1
    assert engine.on_pre_llm_call(session_id="new-1")


def test_state_write_failure_prevents_reset(tmp_path, monkeypatch):
    engine, *_ = ready(tmp_path)
    gateway = Gateway(engine)
    monkeypatch.setattr(engine, "_save", lambda: False)
    assert dispatch(engine, gateway, Event()) is None
    assert gateway.resets == 0


def test_mode_change_and_local_surface_never_auto_reset(tmp_path):
    engine, settings, clock = ready(tmp_path)
    gateway = Gateway(engine)
    settings.mode = "handoff"
    assert dispatch(engine, gateway, Event()) is None
    assert gateway.resets == 0
    local, *_ = make(tmp_path / "local", mode="carryover")
    notice = turn(local, "local-demo", 120_000, 10, platform="cli")
    assert "from handoff" in notice
    assert local.sessions["local-demo"]["mode"] == "handoff"


def test_unstamped_source_uses_active_named_profile_not_default(tmp_path):
    engine, settings, clock = ready(tmp_path)
    engine.profile_fn = lambda: "demo-profile"
    gateway = Gateway(engine)
    event = Event()
    event.source.profile = None
    result = asyncio.run(engine.on_pre_gateway_dispatch(event, gateway, gateway.store,
                                                       active_profile="demo-profile"))
    assert gateway.resets == 1


def test_concurrent_message_waits_for_rotation_before_dispatch(tmp_path):
    engine, *_ = ready(tmp_path)
    gateway = Gateway(engine)
    original = gateway._handle_reset_command

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_reset(event):
            entered.set()
            await release.wait()
            return await original(event)

        gateway._handle_reset_command = blocked_reset
        first = asyncio.create_task(engine.on_pre_gateway_dispatch(Event(), gateway, gateway.store))
        await entered.wait()
        second = asyncio.create_task(engine.on_pre_gateway_dispatch(Event(message_id="demo-2"),
                                                                    gateway, gateway.store))
        await asyncio.sleep(0)
        released_before_rotation = second.done()
        release.set()
        await asyncio.gather(first, second)
        assert not released_before_rotation, "Second inbound could dispatch into the old session"
        assert gateway.store.entry.session_id == "new-1"

    asyncio.run(scenario())
