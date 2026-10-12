"""HandyDandy core: threshold checks, handoff detection and successor launch.

Pure logic with no Hermes imports so it can be unit-tested on its own. The
Hermes wiring lives in ``__init__.py``.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

MODES = ("advise", "handoff", "relay")
SUCCESSOR_ENV = "HANDYDANDY_SUCCESSOR"
NOTICE_MARK = "[handydandy]"


@dataclass
class Settings:
    mode: str = "advise"
    max_input_tokens: int = 100_000
    context_fraction: float = 0.5
    max_messages: int = 60
    context_limit: int = 0  # 0 = ask Hermes for the model's limit
    handoff_dir: str = ""  # "" = <HERMES_HOME>/handoffs
    session_log_csv: str = ""  # "" = no CSV log
    relay_dry_run: bool = True
    relay_platforms: list = field(default_factory=lambda: ["cli", "tui", "desktop"])
    relay_cooldown_minutes: int = 30
    hermes_command: str = ""  # "" = first `hermes` on PATH

    @classmethod
    def from_getter(cls, getter: Callable[[str, Any], Any]) -> "Settings":
        """Read settings via ``getter``; HANDYDANDY_<KEY> environment variables win (handy for tests)."""
        d = cls()

        def get(key: str, default: Any) -> Any:
            env = os.environ.get(f"HANDYDANDY_{key.upper()}")
            return env if env is not None else getter(key, default)

        s = cls(
            mode=str(get("mode", d.mode) or d.mode).strip().lower(),
            max_input_tokens=_as_int(get("max_input_tokens", d.max_input_tokens), d.max_input_tokens),
            context_fraction=_as_float(get("context_fraction", d.context_fraction), d.context_fraction),
            max_messages=_as_int(get("max_messages", d.max_messages), d.max_messages),
            context_limit=_as_int(get("context_limit", d.context_limit), d.context_limit),
            handoff_dir=str(get("handoff_dir", d.handoff_dir) or ""),
            session_log_csv=str(get("session_log_csv", d.session_log_csv) or ""),
            relay_dry_run=_as_bool(get("relay_dry_run", d.relay_dry_run), d.relay_dry_run),
            relay_platforms=_as_list(get("relay_platforms", None), d.relay_platforms),
            relay_cooldown_minutes=_as_int(get("relay_cooldown_minutes", d.relay_cooldown_minutes), d.relay_cooldown_minutes),
            hermes_command=str(get("hermes_command", d.hermes_command) or ""),
        )
        if s.mode not in MODES:
            s.mode = "advise"
        return s


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.strip().lower() in ("1", "true", "yes", "on"):
            return True
        if v.strip().lower() in ("0", "false", "no", "off"):
            return False
    return default


def _as_list(v: Any, default: list) -> list:
    if isinstance(v, (list, tuple)):
        return [str(x).strip().lower() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [x.strip().lower() for x in v.split(",") if x.strip()]
    return list(default)


def crossed(tokens: int, messages: int, ctx_limit: Optional[int], s: Settings) -> list[str]:
    """Return the human-readable reasons a session is over its limits (empty = fine)."""
    reasons = []
    if s.max_input_tokens > 0 and tokens >= s.max_input_tokens:
        reasons.append(f"{tokens:,} input tokens (limit {s.max_input_tokens:,})")
    if ctx_limit and s.context_fraction > 0 and tokens >= ctx_limit * s.context_fraction:
        pct = round(100 * tokens / ctx_limit)
        reasons.append(f"{pct}% of the model's {ctx_limit:,}-token context")
    if s.max_messages > 0 and messages >= s.max_messages:
        reasons.append(f"{messages} messages (limit {s.max_messages})")
    return reasons


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def handoff_path(handoff_dir: Path, profile: str, session_id: str, now: Optional[datetime] = None) -> Path:
    now = now or datetime.now()
    sid = _SAFE.sub("-", session_id or "session")[-24:]
    prof = _SAFE.sub("-", profile or "default")
    return handoff_dir / f"HANDOFF-{now:%Y-%m-%d}-{prof}-{sid}.md"


def notice_text(mode: str, reasons: list[str], path: Path) -> str:
    why = "; ".join(reasons)
    if mode == "advise":
        tail = ("Finish the current task, write a handoff if the work continues, "
                "then start a fresh session with /new.")
    elif mode == "handoff":
        tail = (f"On your next message the agent will finish that request, then write a handoff to "
                f"{path}. Start a fresh session from it with: from handoff {path}")
    else:
        tail = (f"On your next message the agent will finish that request and write a handoff to "
                f"{path}; a fresh session will then start from it automatically.")
    return f"\n\n---\n{NOTICE_MARK} This session is getting long ({why}). {tail}"


def handoff_instruction(path: Path, reasons: list[str]) -> str:
    return (
        f"{NOTICE_MARK} This session has passed its size limit ({'; '.join(reasons)}). "
        "First finish the user's current request as normal. Then, in this same turn, write a "
        f"handoff for a fresh session to {path} (create the folder if needed). Write it for the next "
        "session, not as a recap, with these sections: Standing orders and approvals still in force; "
        "State of each workstream (with file paths and ids); Open items and who owns them; "
        "Next action; What was not touched. Never copy secret values into it. After writing it, "
        f"tell the user in one line that the handoff is at {path}."
    )


def successor_prompt(path: Path) -> str:
    return (
        f"from handoff {path}\n\n"
        "This session was started automatically by the handydandy plugin because the previous one got "
        "too long. Read the handoff file first and re-check the live state of its open items. This is an "
        "unattended start: do not send messages, publish, deploy, spend money or change live data. "
        "Summarize the current state in a few bullets and name the next action, then stop and wait "
        "for the user."
    )


def append_session_log(csv_path: Path, profile: str, session_id: str, summary: str, handoff: Path) -> None:
    csv_path = Path(os.path.expanduser(str(csv_path)))
    new = not csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "profile", "session_id", "summary", "handoff_path"])
        w.writerow([datetime.now().astimezone().isoformat(timespec="seconds"), profile, session_id,
                    summary, str(handoff)])


def successor_command(hermes: str, profile: str, name: str, query_file: Path) -> list[str]:
    cmd = [hermes]
    if profile and profile != "default":
        cmd += ["-p", profile]
    cmd += ["chat", "-Q", "--oneshot", "-c", name, "--create-if-missing", "--query-file", str(query_file)]
    return cmd


class Engine:
    """Per-process state machine. One instance per plugin load."""

    def __init__(self, settings_fn: Callable[[], Settings], data_dir: Path, hermes_home: Path,
                 profile_fn: Callable[[], str],
                 ctx_limit_fn: Optional[Callable[[str, str, str], Optional[int]]] = None,
                 spawn_fn: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.time):
        self.settings_fn = settings_fn
        self.data_dir = Path(data_dir)
        self.hermes_home = Path(hermes_home)
        self.profile_fn = profile_fn
        self.ctx_limit_fn = ctx_limit_fn
        self.spawn_fn = spawn_fn or _default_spawn
        self.clock = clock
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self._ctx_cache: dict[tuple, Optional[int]] = {}
        self.state_file = self.data_dir / "state.json"
        self.log_file = self.data_dir / "events.log"
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        try:
            self.sessions = json.loads(self.state_file.read_text()).get("sessions", {})
        except Exception:
            self.sessions = {}

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # keep the file small: newest 200 sessions
            items = sorted(self.sessions.items(), key=lambda kv: kv[1].get("seen", 0))[-200:]
            self.sessions = dict(items)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": self.sessions}, indent=1))
            tmp.replace(self.state_file)
        except Exception:
            pass

    def log(self, event: str, **fields: Any) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            row = {"t": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields}
            with self.log_file.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    def _s(self, sid: str) -> dict:
        st = self.sessions.setdefault(sid, {})
        st["seen"] = self.clock()
        return st

    # ---- hooks -------------------------------------------------------
    def on_pre_api_request(self, session_id: str = "", approx_input_tokens: Any = None,
                           message_count: Any = None, model: str = "", provider: str = "",
                           base_url: str = "", **_: Any) -> None:
        if not session_id:
            return
        with self.lock:
            st = self._s(session_id)
            st["tokens"] = _as_int(approx_input_tokens, st.get("tokens", 0))
            st["messages"] = _as_int(message_count, st.get("messages", 0))
            st["model"], st["provider"], st["base_url"] = model or "", provider or "", base_url or ""

    def _ctx_limit(self, st: dict, s: Settings) -> Optional[int]:
        if s.context_limit > 0:
            return s.context_limit
        key = (st.get("model", ""), st.get("provider", ""), st.get("base_url", ""))
        if key not in self._ctx_cache:
            val = None
            if self.ctx_limit_fn and key[0]:
                try:
                    val = self.ctx_limit_fn(*key)
                except Exception:
                    val = None
            self._ctx_cache[key] = val if isinstance(val, int) and val > 0 else None
        return self._ctx_cache[key]

    def on_transform_llm_output(self, response_text: str = "", session_id: str = "",
                                platform: str = "", **_: Any) -> Optional[str]:
        """End of a turn, before delivery. Arms the session or fires the relay."""
        if not session_id or not isinstance(response_text, str):
            return None
        s = self.settings_fn()
        successor = os.environ.get(SUCCESSOR_ENV) == "1"
        mode = "advise" if successor and s.mode == "relay" else s.mode
        with self.lock:
            st = self._s(session_id)
            st["platform"] = platform or st.get("platform", "")
            # 1) handoff written this turn -> log it, maybe relay
            if st.get("armed_at") and not st.get("handoff_done") and mode in ("handoff", "relay"):
                hp = Path(st["handoff_path"])
                if hp.exists() and hp.stat().st_mtime >= st["armed_at"] - 1:
                    st["handoff_done"] = self.clock()
                    extra = self._after_handoff(session_id, st, s, mode, hp)
                    self._save()
                    return response_text + extra if extra else None
            # 2) not yet armed -> check thresholds
            if st.get("armed_at"):
                return None
            reasons = crossed(st.get("tokens", 0), st.get("messages", 0), self._ctx_limit(st, s), s)
            if not reasons:
                return None
            hdir = Path(os.path.expanduser(s.handoff_dir)) if s.handoff_dir else self.hermes_home / "handoffs"
            hp = handoff_path(hdir, self.profile_fn(), session_id)
            st.update(armed_at=self.clock(), handoff_path=str(hp), reasons=reasons, mode=mode)
            self.log("armed", session_id=session_id, mode=mode, reasons=reasons, platform=platform,
                     handoff=str(hp))
            self._save()
            return response_text + notice_text(mode, reasons, hp)

    def on_pre_llm_call(self, session_id: str = "", **_: Any) -> Optional[dict]:
        s = self.settings_fn()
        if s.mode not in ("handoff", "relay") or not session_id:
            return None
        with self.lock:
            st = self.sessions.get(session_id) or {}
            if not st.get("armed_at") or st.get("handoff_done") or st.get("mode") == "advise":
                return None
            return {"context": handoff_instruction(Path(st["handoff_path"]), st.get("reasons", []))}

    # ---- relay -------------------------------------------------------
    def _after_handoff(self, sid: str, st: dict, s: Settings, mode: str, hp: Path) -> str:
        profile = self.profile_fn()
        if s.session_log_csv:
            try:
                append_session_log(Path(s.session_log_csv), profile, sid,
                                   "handydandy: handoff written at session size limit", hp)
            except Exception as e:
                self.log("csv_error", error=str(e))
        self.log("handoff_written", session_id=sid, handoff=str(hp))
        if mode != "relay":
            return f"\n\n{NOTICE_MARK} Handoff saved. Start a fresh session with: from handoff {hp}"
        platform = (st.get("platform") or "").lower()
        if platform not in s.relay_platforms:
            self.log("relay_skipped", session_id=sid, reason=f"platform {platform!r} not allowed")
            return (f"\n\n{NOTICE_MARK} Handoff saved. Automatic restart is off for this channel; "
                    f"start a fresh session with: from handoff {hp}")
        last = self._global_last_relay()
        if last and self.clock() - last < s.relay_cooldown_minutes * 60:
            self.log("relay_skipped", session_id=sid, reason="cooldown")
            return f"\n\n{NOTICE_MARK} Handoff saved. Relay is cooling down; start a fresh session with: from handoff {hp}"
        name = f"handydandy-{_SAFE.sub('-', sid)[-24:]}"
        qf = self.data_dir / "prompts" / f"{name}.txt"
        qf.parent.mkdir(parents=True, exist_ok=True)
        qf.write_text(successor_prompt(hp))
        hermes = s.hermes_command or shutil.which("hermes") or "hermes"
        cmd = successor_command(hermes, profile, name, qf)
        st["relay_name"] = name
        if s.relay_dry_run:
            self.log("relay_dry_run", session_id=sid, command=cmd)
            return (f"\n\n{NOTICE_MARK} Handoff saved. Dry run: a fresh session would start now with "
                    f"`{' '.join(cmd)}`. Start it yourself with: from handoff {hp}")
        self._set_global_last_relay(self.clock())
        try:
            out = self.data_dir / "relay-logs" / f"{name}.log"
            out.parent.mkdir(parents=True, exist_ok=True)
            pid = self.spawn_fn(cmd, out)
            self.log("relay_started", session_id=sid, command=cmd, pid=pid, log=str(out))
        except Exception as e:
            self.log("relay_error", session_id=sid, error=str(e))
            return f"\n\n{NOTICE_MARK} Handoff saved, but the fresh session failed to start. Start it with: from handoff {hp}"
        resume = f"hermes -p {profile} -c {name}" if profile and profile != "default" else f"hermes -c {name}"
        return f"\n\n{NOTICE_MARK} Handoff saved. A fresh session started from it. Continue there with: {resume}"

    def _global_last_relay(self) -> float:
        return float(self.sessions.get("__global__", {}).get("last_relay", 0) or 0)

    def _set_global_last_relay(self, t: float) -> None:
        self.sessions.setdefault("__global__", {})["last_relay"] = t
        self.sessions["__global__"]["seen"] = t


def _default_spawn(cmd: list[str], log_path: Path) -> int:
    env = dict(os.environ)
    env[SUCCESSOR_ENV] = "1"
    f = open(log_path, "ab")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                         start_new_session=True, cwd=os.getcwd())
    return p.pid
