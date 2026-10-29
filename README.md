# handydandy

Session-size notices and structured handoffs. A Hermes plugin by Deploy Axiom.

After each turn, handydandy checks the estimated input tokens and message count Hermes reports. When a limit is reached, it does one of two things:

| Mode | What happens |
|---|---|
| `advise` (default) | Adds one notice to the reply: finish up, write a handoff if needed, then start `/new`. |
| `handoff` | On your next message, asks the agent to finish your request and write a structured handoff file. You start the next session yourself with `from handoff <path>`. |

There are no extra model calls from the plugin. Handoff writing uses the agent's existing turn and normal tool approvals. It is an instruction to the agent, not a guarantee that the agent will write the file.

Requires macOS or Linux and Hermes **0.22.5 or newer**, the version tested here. A percentage threshold is available only when you set `context_limit` to the model's context window in tokens. With the default `context_limit: 0`, that check is unavailable; token and message thresholds still work. The plugin does not look up model metadata or import the private `agent.model_metadata` module.

## Before you install

**Files written:**

- `<HERMES_HOME>/plugin-data/handydandy/`: `state.json` for session flags, `state.lock` for cross-process locking, `events.log` for local events, and `.state.<pid>.<thread>.tmp` during atomic state saves. This follows the active Hermes profile. Events and state can include profile/session identifiers, paths, size thresholds and error details; events do not contain conversation text.
- `handoff_dir` (default `<HERMES_HOME>/handoffs/`): the plugin asks the agent to write `HANDOFF-*.md` files here. These may contain work details, paths and identifiers from your conversation. The agent is told not to copy secret values; review handoffs before sharing them.
- `session_log_csv`, if set: the plugin appends a header for a new file and one row per completed handoff containing timestamp, profile, session ID, a fixed summary and handoff path. Parent folders are created as needed. Off by default.

**Files read:** its own `state.json`; the last byte of `events.log` before appending (to separate interrupted records), plus its records when migrating a saved relay setting so an interrupted migration does not duplicate the event; its lock file is opened for locking; handoff files at the recorded paths are checked for size/modification time and the first byte is read to check readability. Hermes supplies plugin settings via `ctx.get_config` (backed by the active profile's `config.yaml`). The plugin also reads `HANDYDANDY_<KEY>` setting overrides and `HERMES_HOME` from the environment. It does not read the session database, full transcripts, credential files or unrelated work files. The agent writing a handoff still has its normal access to the current conversation and tools.

**No network calls, shell commands, background processes, credentials or telemetry:** handydandy itself makes no network requests, executes no shell commands, starts no processes, reads/stores no credentials, and sends no telemetry. It does not auto-approve tools or change Hermes safety settings. Your normal Hermes model requests and any tools used by the agent remain governed by Hermes; this plugin does not make an agent session offline.

Local state, events and handoffs are not automatically deleted when you disable the plugin. Files created by older versions are not automatically removed either.

## Install

```bash
hermes plugins install percyhendrux123-prog/hermes-handydandy
hermes plugins enable handydandy
```

Or copy this folder to `<HERMES_HOME>/plugins/handydandy/` (usually `~/.hermes/plugins/handydandy/`) and run `hermes plugins enable handydandy`.

## Settings

Set them in the Desktop app (Settings, Plugins, handydandy) or with `hermes config set plugins.entries.handydandy.settings.<key> <value>`.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `advise` | `advise` or `handoff`. |
| `max_input_tokens` | `100000` | Estimated input tokens per request that count as too long. `0` turns it off. |
| `context_fraction` | `0.5` | Share of the configured context window that counts as too long. Requires `context_limit > 0`; `0` turns it off. |
| `max_messages` | `60` | Message count that counts as too long. `0` turns it off. |
| `context_limit` | `0` | Set the model's context window in tokens to enable the percentage threshold. `0` leaves that check unavailable. |
| `handoff_dir` | `<HERMES_HOME>/handoffs` | Where the agent is asked to write handoff files. |
| `session_log_csv` | off | Optional CSV that gets one row per completed handoff. |

Any setting can also come from an environment variable named `HANDYDANDY_<KEY>`, which takes precedence over the saved setting.

## Guards and local state

- One notice per retained session record. The state file retains the 200 most recently saved session records; older records can be pruned and warned again.
- Threshold notices are added after a turn finishes, never in the middle of a tool call.
- In handoff mode, the agent is told to finish your current request before it writes the handoff. The instruction repeats on later turns until a usable handoff is observed.
- A handoff counts only when its recorded path is a readable, non-empty file with a modification time at or after the notice (with a one-second timestamp tolerance).
- State changes use a cross-process lock, so multiple Hermes windows share the same session flags.
- If state cannot be read or saved safely, no threshold notice or completed-handoff confirmation is added.
- `events.log` records events such as `armed`, `handoff_written` and errors. Counters stay in memory; `state.json` stores session flags, paths, reasons and migration bookkeeping.

## Changelog

- **0.3.0:** Removed relay mode and automatic restarts. Saved `mode: relay` settings now act as `handoff`, with one `relay_removed` event per profile on first use; old state files remain readable. Removed automatic model-context lookup: set `context_limit` for the percentage threshold.

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q tests
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

## License

MIT
