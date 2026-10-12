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
                      spawn_fn=lambda cmd, *a: spawned.append(cmd) or 4242, clock=clock)
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
    assert not eng.glob.get("last_relay")  # a dry run must not use up the cooldown


def _fake_hermes(tmp_path):
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return str(exe)


def test_relay_live_spawns_named_successor(tmp_path):
    exe = _fake_hermes(tmp_path)
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False, hermes_command=exe)
    out, hp = _relay_to_handoff(eng, clock)
    assert len(spawned) == 1
    cmd = spawned[0]
    assert cmd[:2] == [exe, "chat"] and "--create-if-missing" in cmd and "-p" not in cmd
    qf = Path(cmd[cmd.index("--query-file") + 1])
    assert qf.read_text().startswith(f"from handoff {hp}")
    assert "do not send messages" in qf.read_text()
    assert "hermes -c handydandy-s1" in out and "is starting" in out


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


def test_empty_file_or_directory_is_not_a_handoff(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.mkdir(parents=True)  # a directory at the path
    assert turn(eng, "s1", 121_000, 11) is None
    hp.rmdir()
    hp.write_text("")  # empty file
    assert turn(eng, "s1", 122_000, 12) is None
    assert not spawned and eng.on_pre_llm_call(session_id="s1")  # still asking for it


def test_successor_mark_survives_resume_without_env(tmp_path, monkeypatch):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    monkeypatch.setenv(core.SUCCESSOR_ENV, "1")
    eng.on_pre_api_request(session_id="succ", approx_input_tokens=10, message_count=2, model="m")
    monkeypatch.delenv(core.SUCCESSOR_ENV)
    # a later process resumes the successor session without the env var
    eng2, *_ = make(tmp_path, mode="relay", relay_dry_run=False)
    eng2.data_dir = eng.data_dir
    eng2.state_file, eng2.lock_file, eng2.log_file = eng.state_file, eng.lock_file, eng.log_file
    out = turn(eng2, "succ", 120_000, 10)
    assert "/new" in out and eng2.on_pre_llm_call(session_id="succ") is None


def test_two_processes_cannot_both_relay(tmp_path):
    spawned = []
    s = core.Settings(mode="relay", relay_dry_run=False, handoff_dir=str(tmp_path / "h"))
    clock = Clock()
    mk = lambda: core.Engine(lambda: s, tmp_path / "data", tmp_path / "home", lambda: "default",
                             spawn_fn=lambda cmd, *a: spawned.append(cmd) or 1, clock=clock)
    a, b = mk(), mk()
    turn(a, "x", 120_000, 10)
    turn(b, "y", 120_000, 10)
    for eng, sid in ((a, "x"), (b, "y")):
        hp = Path(eng.sessions[sid]["handoff_path"])
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text("# h")
    clock.t += 5
    turn(a, "x", 121_000, 11)
    out = turn(b, "y", 121_000, 11)  # b reloads state under the lock and sees a's reservation
    assert len(spawned) == 1 and "cooling down" in out
    assert turn(a, "x", 122_000, 12) is None  # never twice for one session


def test_corrupt_state_fails_closed(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.json").write_text("{not json")
    eng, *_ = make(tmp_path, mode="relay", relay_dry_run=False)
    assert turn(eng, "s1", 500_000, 500) is None  # no notice, no relay
    assert eng.on_pre_llm_call(session_id="s1") is None
    assert (d / "state.json").read_text() == "{not json"  # left for a person to inspect
    assert "state_unreadable" in (d / "events.log").read_text()
    (d / "state.json").unlink()  # recovery: delete it
    assert turn(eng, "s1", 500_000, 500)


def test_missing_launcher_does_not_claim_a_restart(tmp_path):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False,
                                  hermes_command=str(tmp_path / "nope" / "hermes"))
    out, hp = _relay_to_handoff(eng, clock)
    assert not spawned and "launcher was not found" in out and "started" not in out


def test_switching_relay_to_handoff_stops_spawning(tmp_path):
    exe = _fake_hermes(tmp_path)
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False, hermes_command=exe)
    turn(eng, "s1", 120_000, 10)
    s.mode = "handoff"
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text("# h")
    clock.t += 5
    out = turn(eng, "s1", 121_000, 11)
    assert not spawned and "Handoff saved" in out


def test_unreadable_handoff_is_not_accepted(tmp_path):
    import os
    if os.geteuid() == 0:
        return
    eng, s, spawned, clock = make(tmp_path, mode="handoff")
    turn(eng, "s1", 120_000, 10)
    hp = Path(eng.sessions["s1"]["handoff_path"])
    hp.parent.mkdir(parents=True)
    hp.write_text("# h")
    hp.chmod(0)
    try:
        assert turn(eng, "s1", 121_000, 11) is None
    finally:
        hp.chmod(0o644)


def test_successor_mark_survives_pruning(tmp_path, monkeypatch):
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    monkeypatch.setenv(core.SUCCESSOR_ENV, "1")
    eng.on_pre_api_request(session_id="succ", approx_input_tokens=1, message_count=1, model="m")
    monkeypatch.delenv(core.SUCCESSOR_ENV)
    for i in range(250):  # arming saves state, pushing it out of the 200 newest records
        clock.t += 1
        turn(eng, f"n{i}", 120_000, 10)
    assert "succ" not in eng.sessions
    out = turn(eng, "succ", 120_000, 10)
    assert "/new" in out


def test_supervisor_kills_term_resistant_descendant(tmp_path):
    import json as _j, os, time as _t
    log, events = tmp_path / "out.log", tmp_path / "events.log"
    pidfile = tmp_path / "child.pid"
    # leader exits at once; its child ignores SIGTERM and would be orphaned
    script = f"(trap '' TERM; exec sleep 60) & echo $! > {pidfile}; sleep 0.3; exit 0"
    core._default_spawn(["/bin/sh", "-c", script], log, 30, events, "sid3")
    for _ in range(200):
        if events.exists() and "relay_finished" in events.read_text():
            break
        _t.sleep(0.2)
    row = _j.loads(events.read_text().splitlines()[-1])
    assert row["event"] == "relay_finished" and row["cleaned_leftovers"] is True
    child = int(pidfile.read_text())
    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_supervisor_kills_overrunning_successor(tmp_path):
    import json as _j, time as _t
    log, events = tmp_path / "out.log", tmp_path / "events.log"
    pid = core._default_spawn(["/bin/sleep", "30"], log, 1, events, "sid1")
    for _ in range(100):
        if events.exists() and "relay_timed_out" in events.read_text():
            break
        _t.sleep(0.2)
    row = _j.loads(events.read_text().splitlines()[-1])
    assert row["event"] == "relay_timed_out" and row["session_id"] == "sid1"


def test_supervisor_records_exit(tmp_path):
    import json as _j, time as _t
    log, events = tmp_path / "out.log", tmp_path / "events.log"
    core._default_spawn(["/bin/echo", "hi"], log, 30, events, "sid2")
    for _ in range(100):
        if events.exists():
            break
        _t.sleep(0.1)
    _t.sleep(0.2)
    row = _j.loads(events.read_text().splitlines()[-1])
    assert row["event"] == "relay_finished" and row["exit_code"] == 0 and log.read_text().strip() == "hi"


def test_legacy_v020_successor_flag_still_blocks_relay(tmp_path):
    import json as _j
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.json").write_text(_j.dumps({"sessions": {"old": {"successor": True, "seen": 1}}, "global": {}}))
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    out = turn(eng, "old", 120_000, 10)
    assert "/new" in out and eng.on_pre_llm_call(session_id="old") is None


def test_legacy_flag_survives_pruning_after_upgrade(tmp_path):
    import json as _j
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.json").write_text(_j.dumps({"sessions": {"old": {"successor": True, "seen": 1}}, "global": {}}))
    eng, s, spawned, clock = make(tmp_path, mode="relay", relay_dry_run=False)
    for i in range(250):  # arming saves state, so these evict "old" from the 200 newest
        clock.t += 1
        turn(eng, f"n{i}", 120_000, 10)
    assert "old" not in eng.sessions
    assert "/new" in turn(eng, "old", 120_000, 10)


def test_supervisor_writes_start_failure_into_linked_log(tmp_path):
    import time as _t
    log, events = tmp_path / "out.log", tmp_path / "events.log"
    core._default_spawn([str(tmp_path / "missing-binary")], log, 30, events, "sid4")
    for _ in range(100):
        if events.exists() and "relay_error" in events.read_text():
            break
        _t.sleep(0.1)
    assert "failed to start" in log.read_text()
