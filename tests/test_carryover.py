"""Carryover was removed in 0.4.1: saved settings fall back safely and no private gateway API is used."""
import importlib.util
import json
import logging
import sys
from pathlib import Path

from test_core import ROOT, Clock, core, turn

# Built by concatenation so this file does not match its own source scan.
PRIVATE_GATEWAY_NAMES = (
    "_handle_" + "reset_command",
    "_session_key_" + "for_source",
    "_is_user_authorized_" + "for_source",
)


class TouchGuard:
    """Stand-in gateway/store: any attribute access is recorded as a failure."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"handydandy touched gateway attribute {name!r}")


def load_plugin(monkeypatch, tmp_path, config):
    spec = importlib.util.spec_from_file_location("hd_plugin_fallback", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hd_plugin_fallback", plugin)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(plugin, "_hermes_home", lambda: tmp_path / "home")

    class Context:
        profile_name = "demo"

        def __init__(self):
            self.hooks = {}

        def get_config(self, key, default):
            return config.get(key, default)

        def register_hook(self, name, fn):
            self.hooks[name] = fn

    ctx = Context()
    plugin.register(ctx)
    return ctx


def test_saved_carryover_setting_falls_back_to_handoff_once(tmp_path, caplog):
    vals = {"mode": "carryover", "handoff_dir": str(tmp_path / "handoffs")}
    settings = lambda: core.Settings.from_getter(lambda k, d: vals.get(k, d))
    assert settings().mode == "handoff"
    clock = Clock()
    mk = lambda: core.Engine(settings, tmp_path / "data", tmp_path / "home", lambda: "default", clock=clock)
    eng = mk()
    with caplog.at_level(logging.WARNING):
        out = turn(eng, "gw", 120_000, 10, platform="telegram")
    hp = Path(eng.sessions["gw"]["handoff_path"])
    assert eng.sessions["gw"]["mode"] == "handoff"
    assert f"from handoff {hp}" in out and "automatically" not in out
    assert sum("carryover" in r.getMessage() for r in caplog.records) == 1
    assert str(hp) in eng.on_pre_llm_call(session_id="gw")["context"]
    hp.parent.mkdir(parents=True)
    hp.write_text("# handoff")
    assert "Handoff saved" in turn(eng, "gw", 121_000, 11, platform="telegram")
    eng = mk()  # a restart must not log the fallback again
    turn(eng, "another", 120_000, 10, platform="telegram")
    events = [json.loads(line)["event"] for line in eng.log_file.read_text().splitlines()]
    assert events.count("carryover_removed") == 1


def test_v040_carryover_state_loads_as_handoff(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    hp = tmp_path / "handoffs" / "old.md"
    (data / "state.json").write_text(json.dumps({"sessions": {
        "old": {"seen": 1, "armed_at": 1, "mode": "carryover", "handoff_path": str(hp),
                "reasons": ["limit"], "handoff_done": 2, "carryover_seed": "# seed",
                "carryover_target": "new", "rotation_intent": True, "reset_key": "route"},
        "new": {"seen": 2, "seed": "# seed", "seed_from": "old"},
    }, "global": {}}))
    s = core.Settings(mode="handoff", handoff_dir=str(tmp_path / "handoffs"))
    eng = core.Engine(lambda: s, data, tmp_path / "home", lambda: "default", clock=Clock())
    assert not eng.broken
    assert eng.sessions["old"]["mode"] == "handoff"
    leftovers = {"seed", "seed_from", "carryover_seed", "carryover_target", "rotation_intent", "reset_key"}
    assert not leftovers & set(eng.sessions["old"]) and not leftovers & set(eng.sessions["new"])
    assert eng.on_pre_llm_call(session_id="new") is None  # no seed is ever injected


def test_registered_hooks_never_touch_gateway(tmp_path, monkeypatch):
    ctx = load_plugin(monkeypatch, tmp_path, {"mode": "carryover", "context_limit": 200_000})
    assert set(ctx.hooks) == {"pre_api_request", "transform_llm_output", "pre_llm_call"}
    gateway, store = TouchGuard(), TouchGuard()
    extra = {"gateway": gateway, "session_store": store}
    ctx.hooks["pre_api_request"](session_id="s", approx_input_tokens=150_000, message_count=10, **extra)
    out = ctx.hooks["transform_llm_output"](session_id="s", response_text="ok", platform="telegram", **extra)
    assert "from handoff" in out
    assert ctx.hooks["pre_llm_call"](session_id="s", **extra)
    assert gateway.touched == [] and store.touched == []


def test_package_has_no_private_gateway_calls():
    sources = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".md"}
               and ".venv" not in p.parts and "__pycache__" not in p.parts]
    assert sources
    hits = [(str(p.relative_to(ROOT)), name) for p in sources
            for name in PRIVATE_GATEWAY_NAMES if name in p.read_text(encoding="utf-8")]
    assert hits == []
    assert not hasattr(core.Engine, "on_pre_gateway_dispatch")
    assert not hasattr(core.Engine, "on_session_reset")
    assert "carryover" not in core.MODES
