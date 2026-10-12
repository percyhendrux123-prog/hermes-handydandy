import importlib.util
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
    for k in list(__import__("os").environ):
        if k.startswith("HANDYDANDY_"):
            monkeypatch.delenv(k)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, **overrides):
    s = core.Settings(**{"handoff_dir": str(tmp_path / "handoffs"), **overrides})
    spawned = []
    clock = Clock()
    eng = core.Engine(lambda: s, tmp_path / "data", tmp_path / "home", lambda: "default",
                      ctx_limit_fn=lambda m, p, b: 200_000,
                      spawn_fn=lambda cmd, log: spawned.append(cmd) or 4242, clock=clock)
    return eng, s, spawned, clock


def turn(eng, sid, tokens, msgs, platform="cli", text="answer"):
    eng.on_pre_api_request(session_id=sid, approx_input_tokens=tokens, message_count=msgs, model="m")
    return eng.on_transform_llm_output(response_text=text, session_id=sid, platform=platform)


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
    eng, s, spawned, clock = make(tmp_path, mode="handoff", session_log_csv=str(tmp_path / "log.csv"))
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
    assert not spawned


def test_stale_handoff_file_is_ignored(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="handoff")
    turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text("old")
    import os
    os.utime(hp, (clock.t - 3600, clock.t - 3600))
    assert turn(eng, "s1", 121_000, 11) is None


def _relay_to_handoff(eng, clock, sid="s1", platform="cli"):
    turn(eng, sid, 120_000, 10, platform=platform)
    hp = Path(eng.sessions[sid]["handoff_path"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text("# handoff")
    clock.t += 5
    return turn(eng, sid, 121_000, 11, platform=platform), hp


def test_relay_dry_run_does_not_spawn(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="relay")
    out, hp = _relay_to_handoff(eng, clock)
    assert "Dry run" in out and not spawned
    assert "relay_dry_run" in (tmp_path / "data" / "events.log").read_text()
    assert not eng._global_last_relay()  # a dry run must not use up the cooldown


def test_relay_live_spawns_named_successor(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False, hermes_command="/x/hermes")
    out, hp = _relay_to_handoff(eng, clock)
    assert len(spawned) == 1
    cmd = spawned[0]
    assert cmd[:2] == ["/x/hermes", "chat"] and "--create-if-missing" in cmd and "-p" not in cmd
    qf = Path(cmd[cmd.index("--query-file") + 1])
    assert qf.read_text().startswith(f"from handoff {hp}")
    assert "do not send messages" in qf.read_text()
    assert "hermes -c handydandy-s1" in out


def test_relay_blocked_on_messaging_platform(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    out, hp = _relay_to_handoff(eng, clock, platform="telegram")
    assert not spawned and "off for this channel" in out


def test_relay_cooldown_across_sessions(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    _relay_to_handoff(eng, clock, sid="a")
    out, _ = _relay_to_handoff(eng, clock, sid="b")
    assert len(spawned) == 1 and "cooling down" in out


def test_successor_process_never_relays(tmp_path, monkeypatch):
    monkeypatch.setenv(core.SUCCESSOR_ENV, "1")
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    out = turn(eng, "s1", 120_000, 10)
    assert "/new" in out  # downgraded to advise
    assert eng.on_pre_llm_call(session_id="s1") is None
    assert not spawned


def test_profile_flag_and_state_survives_reload(tmp_path):
    cmd = core.successor_command("hermes", "axiomops", "n", Path("/q"))
    assert cmd[:3] == ["hermes", "-p", "axiomops"]
    eng, s, spawned, clock = make(tmp_path, mode="advise")
    turn(eng, "s1", 120_000, 10)
    eng2 = core.Engine(lambda: s, tmp_path / "data", tmp_path / "home", lambda: "default", clock=clock)
    assert eng2.sessions["s1"]["armed_at"]
    assert eng2.on_transform_llm_output(response_text="x", session_id="s1", platform="cli") is None


def test_settings_from_getter_sanitizes():
    vals = {"mode": "BOGUS", "max_messages": "7", "relay_platforms": "cli, Desktop", "relay_dry_run": "false"}
    s = core.Settings.from_getter(lambda k, d: vals.get(k, d))
    assert s.mode == "advise" and s.max_messages == 7
    assert s.relay_platforms == ["cli", "desktop"] and s.relay_dry_run is False
