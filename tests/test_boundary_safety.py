"""Adversarial acceptance tests; synthetic routes only, no model/network.

Run the unit cases with the repo venv and PYTHONPATH=<repo>/tests.
Host cases additionally need the supplied stable snapshot and existing host libs.
Regression cases authored by the codereview profile during A213 pass 1.
"""
import asyncio
import json
import importlib.util
import os
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_carryover import Event, Gateway, SUMMARY, dispatch, ready
from test_core import core


def test_manual_new_after_interrupted_rotation_stays_clean(tmp_path):
    engine, settings, clock = ready(tmp_path)
    gateway = Gateway(engine)
    # Durable intent, then host rotated, then process died before completion save.
    with engine._txn() as ok:
        assert ok
        engine.sessions['old'].update(rotation_intent=True, reset_key='demo-route')
        assert engine._save()
    gateway.store.entry = SimpleNamespace(session_id='rotated-before-crash', active_turn_token=None)
    engine = core.Engine(lambda: settings, engine.data_dir, engine.hermes_home,
                         lambda: 'default', clock=clock)
    gateway.engine = engine
    # First post-restart event is explicit /new, not ordinary input.
    dispatch(engine, gateway, Event(text='/new'))
    gateway.store.entry = SimpleNamespace(session_id='manual-clean', active_turn_token=None)
    engine.on_session_reset(old_session_id='rotated-before-crash', session_id='manual-clean')
    dispatch(engine, gateway, Event(text='A completely new topic'))
    assert engine.on_pre_llm_call(session_id='manual-clean') is None, (
        'recovery imported the pre-/new handoff into a manually reset session')


def test_failed_manual_cancellation_save_stays_clean(tmp_path, monkeypatch):
    engine, *_ = ready(tmp_path)
    gateway = Gateway(engine)
    save = engine._save
    monkeypatch.setattr(engine, '_save', lambda: False)
    dispatch(engine, gateway, Event(text='/new'))
    monkeypatch.setattr(engine, '_save', save)
    engine.on_session_reset(old_session_id='old', session_id='manual-clean')
    assert engine.on_pre_llm_call(session_id='manual-clean') is None, (
        'failed cancellation save causes explicit /new to inherit old work')


def test_active_deferral_does_not_discard_new_work(tmp_path):
    engine, *_ = ready(tmp_path)
    gateway = Gateway(engine)
    gateway.store.entry.active_turn_token = 'turn-after-handoff'
    dispatch(engine, gateway, Event(text='Change the widget to blue'))
    # That ordinary turn can finish after the ready handoff, changing its state.
    engine.on_pre_api_request(session_id='old', approx_input_tokens=123000, message_count=14)
    engine.on_transform_llm_output(session_id='old', platform='telegram',
                                   response_text='The widget is now blue.')
    gateway.store.entry.active_turn_token = None
    dispatch(engine, gateway, Event(text='Continue'))
    # A safe carryover must either defer for a refreshed handoff or include the work.
    seed = engine.on_pre_llm_call(session_id=gateway.store.entry.session_id)
    assert gateway.resets == 0 or (seed and 'blue' in seed['context']), (
        'rotated using the old summary after an intervening ordinary turn')


def test_registered_hook_handles_unstamped_owning_named_profile(tmp_path, monkeypatch):
    root = core.Path(core.__file__).parent
    spec = importlib.util.spec_from_file_location('a213_registered_probe', root / '__init__.py',
                                                 submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, '_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(plugin, '_data_dir', lambda: tmp_path / 'data')
    class Context:
        profile_name = 'demo-profile'
        def __init__(self):
            self.hooks = {}
        def get_config(self, key, default):
            return {'mode': 'carryover'}.get(key, default)
        def register_hook(self, name, callback):
            self.hooks[name] = callback
    ctx = Context()
    plugin.register(ctx)
    ctx.hooks['pre_api_request'](session_id='old', approx_input_tokens=120000, message_count=10)
    ctx.hooks['transform_llm_output'](session_id='old', response_text='First turn', platform='telegram')
    state = json.loads((tmp_path / 'data/state.json').read_text())
    hp = core.Path(state['sessions']['old']['handoff_path'])
    hp.parent.mkdir(parents=True)
    hp.write_text(SUMMARY)
    ctx.hooks['transform_llm_output'](session_id='old', response_text='Handoff ready', platform='telegram')
    gateway = Gateway(SimpleNamespace(on_session_reset=ctx.hooks['on_session_reset']))
    event = Event()
    event.source.profile = None  # Valid standalone SessionSource default; no multiplex canonicalization.
    asyncio.run(ctx.hooks['pre_gateway_dispatch'](event=event, gateway=gateway,
                                                 session_store=gateway.store))
    assert gateway.resets == 1, 'registered hook never supplies the new active_profile argument'


def host_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_bootstrap', SimpleNamespace(
        _happy_eyeballs_create_connection=socket.create_connection))
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource, SessionStore, AsyncSessionStore
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    from hermes_cli import lifecycle

    class Runtime(GatewayInboundMixin, GatewaySessionCommandsMixin):
        def _session_key_for_source(self, source):
            return self.session_store._generate_session_key(source)

    config = GatewayConfig()
    config.multiplex_profiles = False
    store = SessionStore(tmp_path / 'sessions', config)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='demo-chat', user_id='demo-user')
    old = store.get_or_create_session(source)
    engine, settings, clock = ready(tmp_path / 'plugin')
    # Remap synthetic ready state to the real synthetic store's generated ID.
    with engine._txn() as ok:
        assert ok
        engine.sessions[old.session_id] = engine.sessions.pop('old')
        assert engine._save()
    runner = Runtime()
    runner.config = config
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner._session_db = None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    effects = []
    for name in ('_invalidate_session_run_generation', '_release_running_agent_state',
                 '_evict_cached_agent', '_clear_conversation_scope'):
        monkeypatch.setattr(runner, name, lambda *a, _n=name, **k: effects.append(_n), raising=False)
    monkeypatch.setattr(runner, '_cleanup_old_agent_for_reset', AsyncMock(), raising=False)
    if hasattr(runner, '_hm_stop_owner_turn'):
        monkeypatch.setattr(runner, '_hm_stop_owner_turn', AsyncMock())
    monkeypatch.setattr(runner, '_reset_notice_session_info', lambda *a: '', raising=False)
    monkeypatch.setattr(runner, '_telegram_topic_new_header', lambda *a: '', raising=False)
    monkeypatch.setattr(runner, '_is_telegram_topic_lane', lambda *a: False, raising=False)
    monkeypatch.setattr(runner, '_is_user_authorized_for_source', lambda *a, **k: True, raising=False)

    def hook(name, **kw):
        if name == 'on_session_reset':
            engine.on_session_reset(**kw)
        return []

    monkeypatch.setattr(lifecycle, 'invoke_hook', hook)
    async def ingress(name, **kw):
        await engine.on_pre_gateway_dispatch(**kw)
        return [None]
    monkeypatch.setattr(lifecycle, 'ainvoke_hook', ingress)
    return engine, runner, store, source, MessageEvent, old, effects


@pytest.mark.skipif(not os.environ.get('HERMES_COMPAT_ROOT'), reason='isolated host fixture required')
def test_real_reset_does_not_rotate_over_concurrent_inbound(tmp_path, monkeypatch):
    engine, runner, store, source, MessageEvent, old, effects = host_runtime(tmp_path, monkeypatch)
    first = MessageEvent(text='First ordinary inbound', source=source, message_id='one')
    second = MessageEvent(text='Second ordinary inbound', source=source, message_id='two')
    async def scenario():
        cleanup_entered, release = asyncio.Event(), asyncio.Event()
        async def cleanup(key):
            cleanup_entered.set()
            await release.wait()
        runner._cleanup_old_agent_for_reset = cleanup
        reset_task = asyncio.create_task(runner._hm_pre_gateway_dispatch_hook(first, source))
        await cleanup_entered.wait()
        # Ingress must serialize the second ordinary message behind the first rotation.
        pending = asyncio.create_task(runner._hm_pre_gateway_dispatch_hook(second, source))
        await asyncio.sleep(0)
        released_early = pending.done()
        key = runner._session_key_for_source(source)
        if released_early:
            assert pending.result() is second
            assert store.lookup_by_session_key(key).session_id == old.session_id
            assert await runner._mark_durable_active_turn(second, key)
            store.append_to_transcript(old.session_id, {'role': 'user', 'content': second.text})
        release.set()
        assert await reset_task is first
        assert await pending is second
        assert not released_early, 'concurrent inbound passed ingress during the pending rotation'
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get('HERMES_COMPAT_ROOT'), reason='isolated host fixture required')
@pytest.mark.parametrize('failure', ['lost_completion', 'cancel_io'])
def test_real_manual_reset_stays_clean_across_recovery(tmp_path, monkeypatch, failure):
    engine, runner, store, source, MessageEvent, old, effects = host_runtime(tmp_path, monkeypatch)
    async def scenario():
        if failure == 'lost_completion':
            key = runner._session_key_for_source(source)
            with engine._txn() as ok:
                assert ok
                engine.sessions[old.session_id].update(rotation_intent=True, reset_key=key)
                assert engine._save()
            adopt = engine.on_session_reset
            monkeypatch.setattr(engine, 'on_session_reset', lambda **kw: None)
            await runner._handle_reset_command(MessageEvent(text='', source=source))
            monkeypatch.setattr(engine, 'on_session_reset', adopt)
            # Simulate restart: persisted intent survives, completion mapping does not.
            engine._load()
        original_save = engine._save
        if failure == 'cancel_io':
            monkeypatch.setattr(engine, '_save', lambda: False)
        manual = MessageEvent(text='/new', source=source, message_id='explicit-new')
        assert await runner._hm_pre_gateway_dispatch_hook(manual, source) is manual
        monkeypatch.setattr(engine, '_save', original_save)
        await runner._handle_reset_command(manual)
        fresh = store.lookup_by_session_key(runner._session_key_for_source(source))
        inbound = MessageEvent(text='Completely unrelated new topic', source=source, message_id='after-new')
        assert await runner._hm_pre_gateway_dispatch_hook(inbound, source) is inbound
        assert engine.on_pre_llm_call(session_id=fresh.session_id) is None, (
            f'{failure}: real full-reset path contaminated a manually clean session')
    asyncio.run(scenario())
