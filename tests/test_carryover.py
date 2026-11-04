"""Synthetic gateway tests: no real messages, accounts or transcripts."""
import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

from test_core import core, make, turn


SUMMARY = """# Carryover
## Standing orders and approvals still in force
Local-only demo; no publishing approval.
## State of each workstream (with file paths and ids)
Demo widget in demo/widget.py; synthetic task demo-1.
## Open items and who owns them
Demo operator reviews the widget.
## Next action
Run the demo tests.
## What was not touched
No accounts or external systems.
"""


@dataclass
class Source:
    profile: str = "default"
    user_id: str = "demo-user"
    chat_id: str = "demo-chat"
    platform: object = field(default_factory=lambda: SimpleNamespace(value="telegram"))


@dataclass
class Event:
    text: str = "Continue the demo, please."
    source: Source = field(default_factory=Source)
    message_id: str = "demo-message-1"
    internal: bool = False
    media_urls: list = field(default_factory=lambda: ["demo-attachment"])

    def get_command(self):
        return self.text[1:].split()[0] if self.text.startswith("/") else None

    def get_command_args(self):
        return self.text


class Store:
    def __init__(self):
        self.entry = SimpleNamespace(session_id="old", active_turn_token=None)

    def lookup_by_session_key(self, key):
        return self.entry if key == "demo-route" else None


class Gateway:
    def __init__(self, engine):
        self.engine = engine
        self.store = Store()
        self.resets = 0
        self.authorized = True

    def _session_key_for_source(self, source):
        return "demo-route"

    def _is_user_authorized_for_source(self, source):
        return self.authorized

    async def _handle_reset_command(self, event):
        assert not event.get_command_args(), "Inbound must not become the new session title"
        self.resets += 1
        old = self.store.entry.session_id
        self.store.entry = SimpleNamespace(session_id=f"new-{self.resets}", active_turn_token=None)
        self.engine.on_session_reset(old_session_id=old, session_id=self.store.entry.session_id,
                                     platform="telegram")
        return "unsent banner"


def ready(tmp_path):
    engine, settings, clock = make(tmp_path, mode="carryover")
    notice = turn(engine, "old", 120_000, 10, platform="telegram")
    instruction = engine.on_pre_llm_call(session_id="old")
    assert instruction and "handoff" in instruction["context"]
    hp = Path(engine.sessions["old"]["handoff_path"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(SUMMARY)
    turn(engine, "old", 121_000, 12, platform="telegram")
    return engine, settings, clock


def dispatch(engine, gateway, event):
    return asyncio.run(engine.on_pre_gateway_dispatch(event=event, gateway=gateway,
                                                     session_store=gateway.store))


def test_threshold_handoff_next_inbound_is_fresh_and_seeded(tmp_path):
    engine, settings, clock = ready(tmp_path)
    gateway = Gateway(engine)
    event = Event()
    before = replace(event)
    assert dispatch(engine, gateway, event) is None
    assert gateway.resets == 1
    assert event == before
    seed = engine.on_pre_llm_call(session_id="new-1")["context"]
    assert "Run the demo tests" in seed
    assert "background" in seed.lower()
    assert "not" in seed and "authorization" in seed
    assert SUMMARY not in (engine.log_file.read_text())
    # A real successful response consumes the seed; retries before that keep it.
    assert engine.on_pre_llm_call(session_id="new-1")["context"] == seed
    turn(engine, "new-1", 500, 2, platform="telegram")
    assert engine.on_pre_llm_call(session_id="new-1") is None
    dispatch(engine, gateway, Event(message_id="demo-message-2"))
    assert gateway.resets == 1
