# handydandy

**Keep the thread. Stop carrying the whole chat.** A Hermes plugin by Deploy Axiom. MIT. Version 0.4.1.

Gateway conversations stay attached to the same session until they are explicitly reset. Users who never type `/new` can keep sending a growing context on every turn. In one observed deployment:

> 30% of one deployment's input came from one never-reset chat

That is an input-share observation, not a measured savings claim. HandyDandy watches session size and, once a session is past your limits, tells the user and (in `handoff` mode) asks the agent to write a structured handoff the next session can start from. The original session remains in Hermes history.

## Modes

| Mode | What happens |
|---|---|
| `advise` (default) | One threshold notice: finish up and start `/new` yourself. |
| `handoff` | On your next message, the agent is asked to finish the request and write a handoff. Start the next session yourself with `from handoff <path>`. |

HandyDandy does not call a model directly. Handoff writing uses an existing agent turn and normal tool approvals. This is an instruction, **not a guarantee** that a model writes a file. If it refuses, lacks file tools or awaits approval, HandyDandy keeps asking on later turns. It never discards, rewrites or re-sends a message, and it never resets a session.

The handoff instruction asks for five sections: standing orders/approvals; workstream state and paths/IDs; open items/owners; next action; what was not touched. This is not a security sanitizer: review handoffs before sharing them.

### Carryover

The automatic `carryover` mode from 0.4.0 has been **removed** until Hermes offers a public session-reset hook for plugins. It depended on private gateway methods, which the plugin catalog does not allow. Upstream request: https://github.com/NousResearch/hermes-agent/issues/149633.

If your config still says `mode: carryover`, HandyDandy runs as `handoff`, logs one `carryover_removed` event and one warning, and does not crash. Session state saved by 0.4.0 loads as manual handoff; no seed from an old session is injected.

## Install and configure

```bash
hermes plugins install percyhendrux123-prog/hermes-handydandy
hermes plugins enable handydandy
hermes config set plugins.entries.handydandy.settings.mode handoff
```

Restart your gateway after changing installed code. For a local checkout, validate it first; copying into `<HERMES_HOME>/plugins/handydandy/` installs it only in that profile. This repository does not install itself.

Example active-profile `config.yaml` fragment (merge into your existing `plugins` block):

```yaml
plugins:
  enabled: [handydandy]
  entries:
    handydandy:
      settings:
        mode: handoff
        max_input_tokens: 100000
        context_fraction: 0.5
        max_messages: 60
        context_limit: 0
        handoff_dir: ""
        session_log_csv: ""
```

### Settings

All behaviour settings live in `config.yaml` under `plugins.entries.handydandy.settings`. Use Desktop Settings → Plugins → handydandy, or `hermes config set plugins.entries.handydandy.settings.<key> <value>`.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `advise` | `advise` or `handoff`. A saved `carryover` runs as `handoff`. |
| `max_input_tokens` | `100000` | Estimated input tokens **per request**, not cumulative billing. `0` disables this check. |
| `context_fraction` | `0.5` | Share of the configured context window. Requires `context_limit > 0`; `0` disables this check. |
| `max_messages` | `60` | Message count in the request; `0` disables this check. |
| `context_limit` | `0` | Explicit model context window in tokens. `0` leaves the percentage check unavailable; token/message thresholds still work. |
| `handoff_dir` | `<HERMES_HOME>/handoffs` | Where the agent is asked to write handoff files. |
| `session_log_csv` | off | Optional one-row-per-completed-handoff CSV. |

**Deprecated:** `HANDYDANDY_<KEY>` environment variables from 0.4.0 and earlier are still read, but only when the matching config setting is missing, and the first use logs one warning. Move them into `config.yaml`; the fallback will be removed in a later release. The plugin needs no `.env` entries and no secrets. It never looks up model metadata.

## Compatibility and extension surfaces

Supported floor: **Hermes 0.22.2**, macOS/Linux. Older versions are not claimed supported; hook presence alone is not a runtime compatibility test. See `docs/VERIFICATION.md` for versions, commands and limitations.

Registered documented hooks: `pre_api_request`, `transform_llm_output`, `pre_llm_call`. HandyDandy calls no private Hermes methods and does not touch the gateway or session store. It neither replaces nor patches any core function or dictionary.

## Before you install: data and side effects

**Files written:**

- `<HERMES_HOME>/plugin-data/handydandy/`: `state.json`, `state.lock`, `events.log`, and atomic `.state.<pid>.<thread>.tmp` files. State holds per-session flags (armed, handoff path, handoff done); events contain metadata, paths and errors, not handoff or transcript text. Paths follow the profile where the plugin is registered.
- `handoff_dir`: the **agent**, under normal tool approvals, writes `HANDOFF-*.md` files. They may contain work details and identifiers. It is instructed not to copy secrets; there is no guaranteed redaction of arbitrary prose.
- `session_log_csv`, if set: fixed summary, timestamp, profile/session ID and handoff path; parent directories are created. Off by default.

**Files/data read:** own state, lock file, the last byte of `events.log` before append and migration records when needed; metadata and the first byte of recorded handoff files; settings through `ctx.get_config`, deprecated `HANDYDANDY_*` environment fallbacks and `HERMES_HOME`. It does **not** read the session database or load transcripts. The agent writing the handoff has its normal conversation/tool access.

**No plugin network calls, shell commands, child processes, credential stores or telemetry.** No self-updater, extra LLM request, session reset, auto-approval or safety-settings mutation. Disabling does not delete local state, handoffs, logs or files created by older versions.

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python 'pytest>=8,<9'
.venv/bin/python -m pytest -q tests
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

No `npm` app/build applies to this stdlib Python plugin.

## Changelog

- **0.4.1:** removed `carryover` mode and the `pre_gateway_dispatch`/`on_session_reset` hooks, so no private gateway methods are used; saved `carryover` settings fall back to `handoff` with one logged event. Behaviour settings moved to `config.yaml`; `HANDYDANDY_*` environment variables are a deprecated fallback with one warning.
- **0.4.0:** opt-in gateway `carryover` (submitted to the catalog; withdrawn in 0.4.1 because it relied on private gateway methods); supported floor corrected to 0.22.2.
- **0.3.0:** removed relay/automatic subprocess restarts; old relay settings normalize to handoff with one migration event. Explicit `context_limit` replaces automatic model-context lookup.

## License

MIT
