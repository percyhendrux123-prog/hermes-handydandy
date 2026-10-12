# handydandy

Sessions that hand off to themselves. A Hermes plugin by Deploy Axiom.

Long agent sessions get slow and expensive, and compaction drops detail. Somebody has to notice, ask for a handoff, and open a fresh session. handydandy does that part.

After every turn it checks how big the session has grown. Past the limit it does one of three things, depending on the mode you pick:

| Mode | What happens |
|---|---|
| `advise` (default) | One short notice at the end of the reply: finish up, write a handoff, start `/new`. |
| `handoff` | On your next message the agent finishes your request, then writes a structured handoff file and tells you where it is. |
| `relay` | Same as `handoff`, then a fresh session starts from the handoff on its own. You continue there with `hermes -c handydandy-<old session id>`. |

No extra model calls. The checks are plain arithmetic on numbers Hermes already reports.

## Install

```bash
hermes plugins install deployaxiom/handydandy
hermes plugins enable handydandy
```

Or copy this folder to `~/.hermes/plugins/handydandy/` and run `hermes plugins enable handydandy`.

## Settings

Set them in the Desktop app (Settings, Plugins, handydandy) or with `hermes config set plugins.entries.handydandy.settings.<key> <value>`.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `advise` | `advise`, `handoff` or `relay` |
| `max_input_tokens` | `100000` | Estimated input tokens that count as too long. `0` turns it off. |
| `context_fraction` | `0.5` | Share of the model's context window that counts as too long. `0` turns it off. |
| `max_messages` | `60` | Message count that counts as too long. `0` turns it off. |
| `context_limit` | `0` | Context window override in tokens. `0` asks Hermes for the active model's window. |
| `handoff_dir` | `<HERMES_HOME>/handoffs` | Where handoff files are written. |
| `session_log_csv` | off | Optional CSV that gets one row per handoff. |
| `relay_dry_run` | `true` | In relay mode, log the restart command instead of running it. Turn off once you trust it. |
| `relay_platforms` | `cli, tui, desktop` | Channels where relay may restart. Messaging channels are off on purpose. |
| `relay_cooldown_minutes` | `30` | Minimum gap between automatic restarts across all sessions. |
| `relay_timeout_minutes` | `20` | A fresh session started by relay is stopped if it runs longer than this. |
| `hermes_command` | `hermes` on PATH | Launcher used for the fresh session. |

Any setting can also come from an environment variable named `HANDYDANDY_<KEY>`, which is handy for testing.

## Guards

- One notice and at most one relay per session.
- Checks run when a turn has finished, never in the middle of a tool call.
- The agent is told to finish your current request before it writes the handoff.
- Relay only runs on the channels you list. Chats with other people (Telegram, Discord, SMS) never restart on their own.
- The fresh session is told it started unattended: it reads the handoff, re-checks state, summarizes and waits. It is told not to send messages, publish, deploy, spend money or change live data.
- A session started by handydandy never starts another one, including after you resume it later.
- A global cooldown stops chains of restarts. It holds across every Hermes window on the profile: the state file is changed only under a cross-process lock, and a restart is recorded before it is launched.
- The fresh session runs under a small supervisor that stops it after the time limit and records how it ended.
- A handoff only counts once it is a real, non-empty file written after the limit was hit.
- If the state file can't be read or written safely, handydandy does nothing rather than guess.
- Dry run is on by default.

## What it records

Requires macOS or Linux. Everything stays on your machine, under `<HERMES_HOME>/plugin-data/handydandy/`:

- `events.log`: one JSON line per event (armed, handoff written, relay started or skipped).
- `state.json`: per-session counters and flags, so a resumed session is not warned twice.
- `prompts/` and `relay-logs/`: the fresh session's first message and its output.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
hermes plugins validate .
hermes plugins doctor . --ci
```

## License

MIT
