# handydandy

**Keep the thread. Stop carrying the whole chat.** A Hermes plugin by Deploy Axiom. MIT.

Gateway conversations stay attached to the same session until they are explicitly reset. Users who never type `/new` can keep sending a growing context on every turn. In one observed deployment:

> 30% of one deployment's input came from one never-reset chat

That is an input-share observation, not a measured savings claim. There is no production before/after cost study for carryover yet. The verifiable change is mechanical: after a structured handoff is ready, the next ordinary inbound message reaches a new session with at most 6,000 characters of handoff content instead of the previous transcript. The original session remains in Hermes history.

## Modes

| Mode | What happens |
|---|---|
| `advise` (default) | One threshold notice: finish up and start `/new` yourself. Unchanged. |
| `handoff` | On your next message, the agent is asked to finish the request and write a handoff. Start the next session yourself with `from handoff <path>`. Unchanged. |
| `carryover` (opt-in) | The same threshold notice requests a short structured handoff on the next normal agent turn. Once a valid file is observed, the **following ordinary gateway message** continues in a fresh session seeded from it. No `/new` or manual copy/paste. CLI/local surfaces use the manual handoff flow. |

HandyDandy does not call a model directly. Handoff writing uses an existing agent turn and normal tool approvals. This is an instruction, **not a guarantee** that a model writes a file. If it refuses, writes an invalid/oversized file, lacks file tools or awaits approval, HandyDandy keeps the old session and repeats the instruction. It never discards a message to force a reset.

Carryover waits for all five Markdown sections, with non-empty content, in a readable recent file no larger than 6,000 characters: standing orders/approvals; workstream state and paths/IDs; open items/owners; next action; what was not touched. Handoff context is labelled historical background, not fresh authorization. Current policy and the current user message take precedence. This is not a security sanitizer: review handoffs before sharing them.

## Install and configure

The remote install command selects the **published** revision, not this unshipped branch:

```bash
hermes plugins install percyhendrux123-prog/hermes-handydandy
hermes plugins enable handydandy
hermes config set plugins.entries.handydandy.settings.mode carryover
```

Restart your gateway after changing installed code. Do not change a production gateway until you have reviewed the candidate. For a local checkout, validate it first; copying into `<HERMES_HOME>/plugins/handydandy/` installs it only in that profile. This repository does not install itself.

Example active-profile `config.yaml` fragment (merge into your existing `plugins` block):

```yaml
plugins:
  enabled: [handydandy]
  isolation: in_process
  entries:
    handydandy:
      settings:
        mode: carryover
        max_input_tokens: 100000
        max_messages: 60
        context_limit: 0
```

`carryover` needs `plugins.isolation: in_process`: the host-isolation process cannot receive live gateway/store objects. Do not weaken an existing pooled-host isolation policy to use this mode; keep advise/handoff or wait for a public remote reset API instead.

### Settings

Use Desktop Settings → Plugins → handydandy, or `hermes config set plugins.entries.handydandy.settings.<key> <value>`.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `advise` | `advise`, `handoff`, or `carryover`. |
| `max_input_tokens` | `100000` | Estimated input tokens **per request**, not cumulative billing. `0` disables this check. |
| `context_fraction` | `0.5` | Share of the configured context window. Requires `context_limit > 0`; `0` disables this check. |
| `max_messages` | `60` | Message count in the request; `0` disables this check. |
| `context_limit` | `0` | Explicit model context window in tokens. `0` leaves the percentage check unavailable; token/message thresholds still work. |
| `handoff_dir` | `<HERMES_HOME>/handoffs` | Where the agent is asked to write handoff files. |
| `session_log_csv` | off | Optional one-row-per-completed-handoff CSV. |

`HANDYDANDY_<KEY>` environment overrides win over saved settings. `.env.example` lists non-secret examples; prefer profile YAML under multiplexing, since process environment overrides are shared. The plugin never looks up model metadata.

## Reset safety and other plugins

- Only ordinary gateway input in the owning profile can trigger automatic rotation. Internal traffic, commands, unauthorized senders and active turns do not. `/new` and `/reset` cancel pending carryovers for that route; an in-memory denial also protects the immediate reset callback if the cancellation save fails.
- The inbound event is never skipped, rewritten, re-sent or re-enqueued by HandyDandy. Text, attachments, reply anchors and message IDs remain on the original event. A text-free **copy** goes to the reset funnel solely to avoid using the inbound text as a session title.
- A durable reset intent is saved **before** the reset. Confirmed source/target session IDs prevent double resets across hook calls and restarts. A seed remains available on model retries until a response transform consumes it.
- If reset throws after rotation, HandyDandy adopts the actual replacement rather than resetting again. A process death before the completion save leaves the intent reserved. **It never guesses that a later occupant of the same route is the intended replacement**: a manual clean reset may have happened in between. Messages still dispatch normally, but an unconfirmed replacement is not seeded automatically. Operator recovery must confirm exact source/target IDs; do not delete state or import an old summary blindly. If the old route never changed, stop the gateway, inspect that specific record and clear only its `rotation_intent`/`reset_key` to allow a retry.
- If ordinary work continues in an active old session, or a queued follow-up produces another response after the handoff, readiness is invalidated. A rewritten, valid handoff with a later modification timestamp is required before rotation. An unchanged old file cannot silently become ready again. This may take another normal turn; no extra model request is launched to force a refresh.
- With `hermes-session-reset-policy`, either plugin can reset first. HandyDandy adopts a lifecycle-reported reset of a ready handoff; a fresh route is no longer due for the time reset. Set that plugin's optional `session_reset.carry_over.enabled: false` if you want only the structured HandyDandy seed, not its additional excerpt context. A time reset **before** a handoff is ready is governed by that plugin; HandyDandy cannot preserve a summary that was never written.
- One notice/reset per retained source session. The 200 most recently saved records are retained; pruned records can be warned again. Very low thresholds can cause frequent legitimate rotations and lose prompt-cache benefits. Measure with your workload; no fixed savings percentage is promised.
- Unreadable state, lock failures or failed pre-reset state saves suppress rotation. Failures after Hermes has already rotated cannot undo that boundary; filesystem failures may prevent seed persistence. Normal dispatch remains the host's responsibility.

## Compatibility and extension surfaces

Supported floor: **Hermes 0.22.2**, macOS/Linux. The stable 0.22.2 source is tested for ingress, the full reset funnel, routing/SQLite transcript storage and timer-plugin cooperation. The previous `>=0.22.5` floor reflected only the version tested at the time, not a missing hook in 0.22.2. Older versions are not claimed supported; hook presence alone is not a runtime compatibility test. See `docs/VERIFICATION.md` for exact versions, commands and limitations.

Registered documented hooks: `pre_api_request`, `transform_llm_output`, `pre_llm_call`, `pre_gateway_dispatch`, `on_session_reset`.

**Private-method dependency, deliberately disclosed:** Hermes exposes no documented public full-reset API through the ingress hook. Carryover uses the handed gateway's `_is_user_authorized_for_source`, `_session_key_for_source` and `_handle_reset_command`, plus `session_store.lookup_by_session_key`. It neither replaces nor patches any core function/dictionary. Missing helpers disable automatic rotation rather than degrade to a store-only reset that could leave cached agents, scoped settings or delegation state behind. These methods are not stable public API; future releases can break carryover and catalog reviewers may require a public reset seam. Passing validation is not catalog acceptance.

## Before you install: data and side effects

**Files written:**

- `<HERMES_HOME>/plugin-data/handydandy/`: `state.json`, `state.lock`, `events.log`, and atomic `.state.<pid>.<thread>.tmp` files. State now includes bounded handoff content, source/target IDs and route-reset intent; events contain metadata, paths and errors, not handoff or transcript text. Paths follow the profile where the plugin is registered.
- `handoff_dir`: the **agent**, under normal tool approvals, writes `HANDOFF-*.md` files. They may contain work details and identifiers. It is instructed not to copy secrets or personal data; there is no guaranteed redaction of arbitrary prose.
- `session_log_csv`, if set: fixed summary, timestamp, profile/session ID and handoff path; parent directories are created. Off by default.
- **Through Hermes's own reset funnel:** the gateway's routing index/session database and lifecycle state change exactly as a `/new` boundary does. The old transcript is retained; it is not a privacy delete. Agent-cache eviction, conversation-scope cleanup, delegation interruption and reset lifecycle hooks are host effects. Other installed plugins can react to those hooks.

**Files/data read:** own state, lock file, the last byte of `events.log` before append and migration records when needed; recorded handoff files (metadata/first-byte checks in manual mode, up to 6,001 text characters in carryover); settings through `ctx.get_config`, non-secret `HANDYDANDY_*` overrides and `HERMES_HOME`. Carryover also reads routing-entry identifiers/active-turn status through the hook-supplied store. It does **not** read the session database directly or load full transcripts. The host's reset funnel can read/write its normal session storage. The agent writing the handoff has its normal conversation/tool access.

**No plugin network calls, shell commands, child processes, credential stores or telemetry.** No self-updater, extra LLM request, auto-approval or safety-settings mutation. Ordinary Hermes model/tool requests and host/plugin lifecycle side effects remain governed by Hermes. Disabling does not delete local state, handoffs, logs or files created by older versions.

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python 'pytest>=8,<9'
.venv/bin/python -m pytest -q tests
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

The five Hermes integration cases skip unless `HERMES_COMPAT_ROOT` and `RESET_POLICY_ROOT` point at real checkouts. Run them with Python compatible with Hermes and its dependencies, an isolated `HERMES_HOME`, managed `TMPDIR`, and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. See `docs/VERIFICATION.md` for the tested matrix. No `npm` app/build applies to this stdlib Python plugin.

## Changelog

- **0.4.0 (local candidate):** opt-in gateway `carryover`, bounded structured seeds, durable rotation deduplication, timer-plugin coordination; supported floor corrected to 0.22.2. Not released or submitted from this build.
- **0.3.0:** removed relay/automatic subprocess restarts; old relay settings normalize to handoff with one migration event. Explicit `context_limit` replaces automatic model-context lookup.

## License

MIT
