"""HandyDandy core: threshold checks and handoff detection.

Pure logic with no Hermes imports so it can be unit-tested on its own. The
Hermes wiring lives in ``__init__.py``.
"""
from __future__ import annotations

import csv
import fcntl
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

MODES = ("advise", "handoff")
NOTICE_MARK = "[handydandy]"


@dataclass
class Settings:
    mode: str = "advise"
    _relay_removed: bool = field(default=False, init=False, repr=False)
    max_input_tokens: int = 100_000
    context_fraction: float = 0.5
    max_messages: int = 60
    context_limit: int = 0  # 0 = percentage check unavailable
    handoff_dir: str = ""  # "" = <HERMES_HOME>/handoffs
    session_log_csv: str = ""  # "" = no CSV log

    def __post_init__(self) -> None:
        self.mode = str(self.mode).strip().lower()
        if self.mode == "relay":
            self.mode = "handoff"
            self._relay_removed = True
        elif self.mode not in MODES:
            self.mode = "advise"

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
        )
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
    else:
        tail = (f"On your next message the agent will finish that request, then write a handoff to "
                f"{path}. Start a fresh session from it with: from handoff {path}")
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


class Engine:
    """State machine shared by every Hermes process on this profile.

    Live counters (tokens, messages) stay in memory. Flags that must never repeat (armed,
    handoff done) live in state.json and are only changed inside
    ``_txn()``, which holds a cross-process file lock and re-reads the file first.
    """

    LOCK_WAIT_S = 5.0

    def __init__(self, settings_fn: Callable[[], Settings], data_dir: Path, hermes_home: Path,
                 profile_fn: Callable[[], str],
                 clock: Callable[[], float] = time.time):
        self.settings_fn = settings_fn
        self.data_dir = Path(data_dir)
        self.hermes_home = Path(hermes_home)
        self.profile_fn = profile_fn
        self.clock = clock
        self.lock = threading.RLock()
        self.live: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.glob: dict[str, Any] = {}
        self.state_file = self.data_dir / "state.json"
        self.lock_file = self.data_dir / "state.lock"
        self.log_file = self.data_dir / "events.log"
        self.broken = False
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text())
            if not isinstance(raw, dict):
                raise ValueError("state.json is not an object")
            self.sessions = raw.get("sessions", {})
            self.glob = raw.get("global", {})
            if not isinstance(self.sessions, dict) or not isinstance(self.glob, dict):
                raise ValueError("state.json sessions and global must be objects")
            if not all(isinstance(st, dict) for st in self.sessions.values()):
                raise ValueError("state.json session records must be objects")
            # Read v0.2.x state without retaining the removed launch bookkeeping.
            for st in self.sessions.values():
                if isinstance(st, dict):
                    if st.get("mode") == "relay":
                        st["mode"] = "handoff"
                    for key in ("successor", "relay_reserved", "relay_name", "relay_pid"):
                        st.pop(key, None)
            for key in ("successors", "relayed", "last_relay"):
                self.glob.pop(key, None)
            self.broken = False
        except FileNotFoundError:
            self.sessions, self.glob, self.broken = {}, {}, False
        except Exception as e:
            # Fail closed: leave the file as it is and do nothing until a person fixes or deletes it.
            if not self.broken:
                self.log("state_unreadable", error=str(e), fix=f"inspect or delete {self.state_file}")
            self.sessions, self.glob, self.broken = {}, {}, True

    def _save(self) -> bool:
        if self.broken:
            return False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            items = sorted(self.sessions.items(), key=lambda kv: kv[1].get("seen", 0))[-200:]
            self.sessions = dict(items)
            tmp = self.state_file.with_name(f".state.{os.getpid()}.{threading.get_ident()}.tmp")
            with tmp.open("w") as f:
                json.dump({"sessions": self.sessions, "global": self.glob}, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.state_file)
            return True
        except Exception as e:
            self.log("state_save_failed", error=str(e))
            return False

    @contextmanager
    def _txn(self):
        """Cross-process critical section. Yields True if the lock was taken (state reloaded)."""
        with self.lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_file, "a+")
            got = False
            try:
                deadline = time.monotonic() + self.LOCK_WAIT_S
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = True
                        break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            break
                        time.sleep(0.05)
                if got:
                    self._load()
                else:
                    self.log("lock_timeout")
                yield got and not self.broken
            finally:
                if got:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()

    def log(self, event: str, **fields: Any) -> bool:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            row = {"t": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields}
            with self.log_file.open("a") as f:
                if f.tell():
                    with self.log_file.open("rb") as previous:
                        previous.seek(-1, os.SEEK_END)
                        if previous.read(1) != b"\n":
                            f.write("\n")  # keep interrupted records separate from this event
                f.write(json.dumps(row, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception:
            return False

    def _s(self, sid: str) -> dict:
        st = self.sessions.setdefault(sid, {})
        st["seen"] = self.clock()
        return st

    def _note_removed_mode(self, s: Settings) -> bool:
        """Record once under _txn(); recover if the event outlives its state marker."""
        if not s._relay_removed or self.glob.get("relay_removed"):
            return True
        found = False
        try:
            with self.log_file.open() as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("event") == "relay_removed":
                        found = True
                        break
        except FileNotFoundError:
            pass
        except OSError:
            return False
        if not found and not self.log("relay_removed", mode="handoff"):
            return False
        self.glob["relay_removed"] = True
        if self._save():
            return True
        self.glob.pop("relay_removed", None)
        return False

    # ---- hooks -------------------------------------------------------
    def on_pre_api_request(self, session_id: str = "", approx_input_tokens: Any = None,
                           message_count: Any = None, **_: Any) -> None:
        if not session_id:
            return
        with self.lock:
            lv = self.live.setdefault(session_id, {})
            lv["tokens"] = _as_int(approx_input_tokens, lv.get("tokens", 0))
            lv["messages"] = _as_int(message_count, lv.get("messages", 0))

    def on_transform_llm_output(self, response_text: str = "", session_id: str = "",
                                platform: str = "", **_: Any) -> Optional[str]:
        """End of a turn, before delivery. Arms the session or records its handoff."""
        if not session_id or not isinstance(response_text, str):
            return None
        s = self.settings_fn()
        with self.lock:
            lv = dict(self.live.get(session_id, {}))
        with self._txn() as ok:
            if not ok:
                return None  # fail closed: no notice or handoff instruction
            if not self._note_removed_mode(s):
                return None
            mode = s.mode
            st = self._s(session_id)
            st["platform"] = platform or st.get("platform", "")
            # 1) handoff written this turn -> log it
            if st.get("armed_at") and not st.get("handoff_done") and st.get("mode") == "handoff":
                hp = Path(st["handoff_path"])
                if not _usable_handoff(hp, st["armed_at"]):
                    return None
                st["handoff_done"] = self.clock()
                if not self._save():
                    return None
                extra = self._after_handoff(session_id, s, hp)
                return response_text + extra
            # 2) not yet armed -> check thresholds
            if st.get("armed_at"):
                return None
            reasons = crossed(lv.get("tokens", 0), lv.get("messages", 0),
                              s.context_limit if s.context_limit > 0 else None, s)
            if not reasons:
                return None
            hdir = Path(os.path.expanduser(s.handoff_dir)) if s.handoff_dir else self.hermes_home / "handoffs"
            hp = handoff_path(hdir, self.profile_fn(), session_id)
            st.update(armed_at=self.clock(), handoff_path=str(hp), reasons=reasons, mode=mode)
            if not self._save():
                return None
            self.log("armed", session_id=session_id, mode=mode, reasons=reasons, platform=platform,
                     handoff=str(hp))
            return response_text + notice_text(mode, reasons, hp)

    def on_pre_llm_call(self, session_id: str = "", **_: Any) -> Optional[dict]:
        s = self.settings_fn()
        if not session_id or s.mode != "handoff":
            return None
        with self._txn() as ok:
            if not ok or not self._note_removed_mode(s):
                return None
            st = self.sessions.get(session_id) or {}
            if not st.get("armed_at") or st.get("handoff_done") or st.get("mode") == "advise":
                return None
            return {"context": handoff_instruction(Path(st["handoff_path"]), st.get("reasons", []))}

    # ---- handoff logging (called inside _txn) -------------------------
    def _after_handoff(self, sid: str, s: Settings, hp: Path) -> str:
        profile = self.profile_fn()
        if s.session_log_csv:
            try:
                append_session_log(Path(s.session_log_csv), profile, sid,
                                   "handydandy: handoff written at session size limit", hp)
            except Exception as e:
                self.log("csv_error", error=str(e))
        self.log("handoff_written", session_id=sid, handoff=str(hp))
        return f"\n\n{NOTICE_MARK} Handoff saved. Start a fresh session with: from handoff {hp}"


def _usable_handoff(hp: Path, armed_at: float) -> bool:
    try:
        st = hp.stat()
    except OSError:
        return False
    if not (hp.is_file() and st.st_size > 0 and st.st_mtime >= armed_at - 1):
        return False
    try:
        with hp.open("rb") as f:
            return bool(f.read(1))
    except OSError:
        return False
