import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hd_core", ROOT / "core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["hd_core"] = core
spec.loader.exec_module(core)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("HANDYDANDY_"):
            monkeypatch.delenv(k)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, **overrides):
    s = core.Settings(**{"handoff_dir": str(tmp_path / "handoffs"), **overrides})
    clock = Clock()
    eng = core.Engine(lambda: s, tmp_path / "data", tmp_path / "home", lambda: "default", clock=clock)
    return eng, s, clock


def turn(eng, sid, tokens, msgs, platform="cli", text="answer"):
    eng.on_pre_api_request(session_id=sid, approx_input_tokens=tokens, message_count=msgs, model="m")
    return eng.on_transform_llm_output(response_text=text, session_id=sid, platform=platform)


def test_removed_relay_setting_falls_back_to_handoff_once(tmp_path):
    vals = {"mode": "relay", "handoff_dir": str(tmp_path / "handoffs")}
    settings = lambda: core.Settings.from_getter(lambda k, d: vals.get(k, d))
    assert settings().mode == "handoff"
    clock = Clock()
    mk = lambda: core.Engine(settings, tmp_path / "data", tmp_path / "home",
                             lambda: "default", clock=clock)
    eng = mk()
    out = turn(eng, "old-config", 120_000, 10)
    hp = Path(eng.sessions["old-config"]["handoff_path"])
    assert "automatically" not in out and str(hp) in out
    assert eng.on_pre_llm_call(session_id="old-config")
    hp.parent.mkdir(parents=True)
    hp.write_text("# handoff")
    assert "Handoff saved" in turn(eng, "old-config", 121_000, 11)
    eng = mk()  # the saved relay setting must not log again after a restart
    assert eng.on_pre_llm_call(session_id="old-config") is None
    turn(eng, "another-session", 120_000, 10)
    events = [json.loads(line) for line in eng.log_file.read_text().splitlines()]
    assert [e["event"] for e in events].count("relay_removed") == 1
    assert not (eng.data_dir / "prompts").exists()
    assert not (eng.data_dir / "relay-logs").exists()


@pytest.mark.parametrize("completed", [False, True])
def test_old_state_loads_without_relay_flags(tmp_path, completed):
    data = tmp_path / "data"
    data.mkdir()
    hp = tmp_path / "handoffs" / "legacy.md"
    state = {"sessions": {"old": {
        "seen": 1, "armed_at": 1, "mode": "relay", "handoff_path": str(hp),
        "reasons": ["old limit"], "successor": True, "relay_reserved": 2,
        "relay_name": "old-name", "relay_pid": 123,
        **({"handoff_done": 2} if completed else {}),
    }}, "global": {"successors": ["old"], "relayed": ["old"], "last_relay": 2}}
    (data / "state.json").write_text(json.dumps(state))
    eng, *_ = make(tmp_path, mode="handoff")
    assert not eng.broken
    assert eng.sessions["old"]["mode"] == "handoff"
    assert not any(k.startswith("relay") or k == "successor" for k in eng.sessions["old"])
    assert not any(k in eng.glob for k in ("successors", "relayed", "last_relay"))
    if completed:
        assert eng.on_pre_llm_call(session_id="old") is None
        assert turn(eng, "old", 120_000, 10) is None
    else:
        assert str(hp) in eng.on_pre_llm_call(session_id="old")["context"]
        hp.parent.mkdir()
        hp.write_text("# handoff")
        assert "Handoff saved" in turn(eng, "old", 120_000, 10)
    reloaded, *_ = make(tmp_path, mode="handoff")
    assert reloaded.sessions["old"]["handoff_done"]
    assert turn(reloaded, "old", 120_000, 10) is None


@pytest.mark.parametrize("context_limit", [0, 200_000])
def test_percentage_threshold_needs_explicit_context_limit(tmp_path, context_limit):
    eng, *_ = make(tmp_path, context_limit=context_limit, max_input_tokens=0, max_messages=0)
    out = turn(eng, "s1", 110_000, 10)
    if context_limit:
        assert "55%" in out
    else:
        assert out is None


def test_under_limits_does_nothing(tmp_path):
    eng, *_ = make(tmp_path)
    assert turn(eng, "s1", 10_000, 10) is None
    assert eng.on_pre_llm_call(session_id="s1") is None


def test_each_threshold_fires():
    s = core.Settings()
    assert core.crossed(100_000, 1, None, s)
    assert core.crossed(1, 60, None, s)
    assert core.crossed(60_000, 1, 100_000, s)  # 60% of context
    assert not core.crossed(40_000, 10, 100_000, s)
    off = core.Settings(max_input_tokens=0, context_fraction=0, max_messages=0)
    assert not core.crossed(10**9, 10**6, 1000, off)


def test_advise_notices_once_and_never_injects(tmp_path):
    eng, *_ = make(tmp_path, mode="advise")
    out = turn(eng, "s1", 120_000, 10)
    assert out.startswith("answer") and core.NOTICE_MARK in out and "/new" in out
    assert turn(eng, "s1", 130_000, 12) is None
    assert eng.on_pre_llm_call(session_id="s1") is None


def test_handoff_mode_injects_until_file_written(tmp_path):
    eng, s, clock = make(tmp_path, mode="handoff", session_log_csv=str(tmp_path / "log.csv"))
    out = turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    assert str(hp) in out
    ctxmsg = eng.on_pre_llm_call(session_id="s1")["context"]
    assert str(hp) in ctxmsg and "finish the user's current request" in ctxmsg
    clock.t += 30
    assert turn(eng, "s1", 125_000, 12) is None  # agent hasn't written it yet
    hp.parent.mkdir(parents=True)
    hp.write_text("# handoff")
    out = turn(eng, "s1", 126_000, 14)
    assert "Handoff saved" in out and f"from handoff {hp}" in out
    assert eng.on_pre_llm_call(session_id="s1") is None
    rows = (tmp_path / "log.csv").read_text().splitlines()
    assert rows[0].startswith("timestamp,profile") and "s1" in rows[1]
    assert turn(eng, "s1", 130_000, 16) is None
    assert len((tmp_path / "log.csv").read_text().splitlines()) == 2


def test_stale_handoff_file_is_ignored(tmp_path):
    eng, s, clock = make(tmp_path, mode="handoff")
    turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text("old")
    os.utime(hp, (clock.t - 3600, clock.t - 3600))
    assert turn(eng, "s1", 121_000, 11) is None


def test_state_survives_reload(tmp_path):
    eng, s, clock = make(tmp_path, mode="advise")
    turn(eng, "s1", 120_000, 10)
    eng2 = core.Engine(lambda: s, tmp_path / "data", tmp_path / "home", lambda: "default", clock=clock)
    assert eng2.sessions["s1"]["armed_at"]
    assert eng2.on_transform_llm_output(response_text="x", session_id="s1", platform="cli") is None


def test_settings_from_getter_sanitizes():
    vals = {"mode": "BOGUS", "max_messages": "7", "context_limit": "invalid"}
    s = core.Settings.from_getter(lambda k, d: vals.get(k, d))
    assert s.mode == "advise" and s.max_messages == 7 and s.context_limit == 0


def test_environment_can_supply_legacy_mode(monkeypatch):
    monkeypatch.setenv("HANDYDANDY_MODE", " RELAY ")
    monkeypatch.setenv("HANDYDANDY_CONTEXT_LIMIT", "200000")
    s = core.Settings.from_getter(lambda k, d: d)
    assert s.mode == "handoff" and s.context_limit == 200_000


def test_empty_file_or_directory_is_not_a_handoff(tmp_path):
    eng, *_ = make(tmp_path, mode="handoff")
    turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.mkdir(parents=True)  # a directory at the path
    assert turn(eng, "s1", 121_000, 11) is None
    hp.rmdir()
    hp.write_text("")  # empty file
    assert turn(eng, "s1", 122_000, 12) is None
    assert eng.on_pre_llm_call(session_id="s1")  # still asking for it


def test_two_engines_record_handoff_once(tmp_path):
    a, s, clock = make(tmp_path, mode="handoff", session_log_csv=str(tmp_path / "log.csv"))
    b = core.Engine(lambda: s, a.data_dir, a.hermes_home, lambda: "default", clock=clock)
    turn(a, "s1", 120_000, 10)
    assert turn(b, "s1", 120_000, 10) is None
    hp = Path(a.sessions["s1"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text("# h")
    assert "Handoff saved" in turn(b, "s1", 121_000, 11)
    assert turn(a, "s1", 122_000, 12) is None
    assert len((tmp_path / "log.csv").read_text().splitlines()) == 2


def test_corrupt_state_fails_closed(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.json").write_text("{not json")
    eng, *_ = make(tmp_path, mode="handoff")
    assert turn(eng, "s1", 500_000, 500) is None
    assert eng.on_pre_llm_call(session_id="s1") is None
    assert (d / "state.json").read_text() == "{not json"  # left for a person to inspect
    assert "state_unreadable" in (d / "events.log").read_text()
    (d / "state.json").unlink()  # recovery: delete it
    assert turn(eng, "s1", 500_000, 500)


def test_unreadable_handoff_is_not_accepted(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read mode-000 files")
    eng, *_ = make(tmp_path, mode="handoff")
    turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text("# h")
    hp.chmod(0)
    try:
        assert turn(eng, "s1", 121_000, 11) is None
    finally:
        hp.chmod(0o644)


def test_removed_mode_event_retries_after_log_failure(tmp_path):
    eng, *_ = make(tmp_path, mode="relay")
    eng.data_dir.mkdir()
    eng.log_file.mkdir()  # deterministic unreadable/unwritable event destination
    assert turn(eng, "first", 120_000, 10) is None
    assert not eng.glob.get("relay_removed")
    eng.log_file.rmdir()
    restarted, *_ = make(tmp_path, mode="relay")
    assert turn(restarted, "second", 120_000, 10)
    rows = [json.loads(line) for line in restarted.log_file.read_text().splitlines()]
    assert sum(row["event"] == "relay_removed" for row in rows) == 1


def test_removed_mode_event_retries_after_log_append_failure(tmp_path, monkeypatch):
    eng, *_ = make(tmp_path, mode="relay")
    original_open = Path.open
    failures = []

    def fail_first_append(path, mode="r", *args, **kwargs):
        if path == eng.log_file and mode == "a" and not failures:
            failures.append(True)
            raise OSError("synthetic event append failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_first_append)
    assert turn(eng, "first", 120_000, 10) is None
    assert failures and not eng.glob.get("relay_removed")
    assert turn(eng, "second", 120_000, 10)
    rows = [json.loads(line) for line in eng.log_file.read_text().splitlines()]
    assert sum(row["event"] == "relay_removed" for row in rows) == 1


@pytest.mark.parametrize("partial_length", [15, -1])
def test_removed_mode_event_recovers_after_interrupted_write(tmp_path, monkeypatch, partial_length):
    eng, *_ = make(tmp_path, mode="relay")
    original_open = Path.open
    failures = []

    class InterruptedWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, key):
            return getattr(self.handle, key)

        def write(self, data):
            if not failures:
                failures.append(True)
                self.handle.write(data[:partial_length])
                self.handle.flush()
                raise OSError("synthetic interrupted event write")
            return self.handle.write(data)

    def interrupt_first_append(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == eng.log_file and mode in ("a", "a+b") and not failures:
            return InterruptedWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", interrupt_first_append)
    assert turn(eng, "first", 120_000, 10) is None
    assert failures and not eng.glob.get("relay_removed")
    restarted, *_ = make(tmp_path, mode="relay")
    assert turn(restarted, "second", 120_000, 10)
    again, *_ = make(tmp_path, mode="relay")
    assert turn(again, "third", 120_000, 10)
    rows = []
    for line in again.log_file.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass  # the deliberately interrupted prefix is not a completed event
    assert sum(row.get("event") == "relay_removed" for row in rows) == 1
    assert sum(row.get("event") == "armed" for row in rows) == 2
    assert json.loads(again.state_file.read_text())["global"]["relay_removed"] is True


def test_removed_mode_event_recovers_after_marker_save_failure(tmp_path, monkeypatch):
    eng, *_ = make(tmp_path, mode="relay")
    original_replace = Path.replace
    failures = []

    def fail_first_replace(path, target):
        if Path(target) == eng.state_file and not failures:
            failures.append(True)
            raise OSError("synthetic first state-save failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_first_replace)
    assert turn(eng, "first", 120_000, 10) is None
    assert failures and not eng.glob.get("relay_removed")
    restarted, *_ = make(tmp_path, mode="relay")
    assert turn(restarted, "second", 120_000, 10)
    assert turn(eng, "third", 120_000, 10)
    rows = [json.loads(line) for line in restarted.log_file.read_text().splitlines()]
    assert sum(row["event"] == "relay_removed" for row in rows) == 1
    assert json.loads(eng.state_file.read_text())["global"]["relay_removed"] is True


@pytest.mark.parametrize("state", [
    {"sessions": [], "global": {}},
    {"sessions": {}, "global": []},
    {"sessions": {"bad": []}, "global": {}},
])
def test_malformed_state_fails_closed_without_overwriting(tmp_path, state):
    data = tmp_path / "data"
    data.mkdir()
    before = json.dumps(state)
    (data / "state.json").write_text(before)
    eng, *_ = make(tmp_path, mode="handoff")
    assert eng.broken
    assert turn(eng, "s1", 120_000, 10) is None
    assert eng.on_pre_llm_call(session_id="s1") is None
    assert eng.state_file.read_text() == before
    assert "state_unreadable" in eng.log_file.read_text()


def test_registered_hooks_use_explicit_settings_without_metadata_lookup(tmp_path, monkeypatch):
    import builtins

    imports = []
    original_import = builtins.__import__

    def track_import(name, *args, **kwargs):
        if name.startswith("agent.model_metadata"):
            imports.append(name)
            raise AssertionError("private metadata lookup is not allowed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", track_import)
    spec = importlib.util.spec_from_file_location("hd_plugin", ROOT / "__init__.py",
                                                submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hd_plugin", plugin)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(plugin, "_hermes_home", lambda: tmp_path / "home")

    class Context:
        profile_name = "demo"

        def __init__(self):
            self.hooks = {}

        def get_config(self, key, default):
            return {"mode": "handoff", "context_limit": 200_000}.get(key, default)

        def register_hook(self, name, fn):
            self.hooks[name] = fn

    ctx = Context()
    plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_api_request", "transform_llm_output", "pre_llm_call"}
    ctx.hooks["pre_api_request"](session_id="demo", approx_input_tokens=110_000, message_count=10,
                                 model="unknown", provider="custom", base_url="https://invalid.example")
    out = ctx.hooks["transform_llm_output"](session_id="demo", response_text="answer", platform="cli")
    assert "55%" in out
    instruction = ctx.hooks["pre_llm_call"](session_id="demo")["context"]
    state = json.loads((tmp_path / "data" / "state.json").read_text())
    hp = Path(state["sessions"]["demo"]["handoff_path"])
    assert str(hp) in instruction and hp.parent == tmp_path / "home" / "handoffs"
    hp.parent.mkdir(parents=True)
    hp.write_text("# Synthetic handoff\nNext action: wait for the user.\n")
    assert "Handoff saved" in ctx.hooks["transform_llm_output"](session_id="demo", response_text="written")
    assert ctx.hooks["pre_llm_call"](session_id="demo") is None
    assert imports == []
