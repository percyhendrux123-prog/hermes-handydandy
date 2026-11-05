# Catalog submission (0.4.1)

Re-pin for `NousResearch/hermes-agent` PR #148914 after review 5789489691 (requested changes on `cb5bab5`, v0.4.0).

## What changed since the reviewed pin

- `carryover` mode is removed. The plugin no longer registers `pre_gateway_dispatch` or `on_session_reset` and calls no private gateway or session-store methods (catalog rule 9).
- A saved `mode: carryover` falls back to `handoff` with one logged event and one warning; 0.4.0 session state loads as manual handoff.
- Behaviour settings live in `config.yaml` (`plugins.entries.handydandy.settings`). `HANDYDANDY_*` environment variables are read only as a deprecated fallback, with one warning.
- Carryover returns only if Hermes adds a public session-reset hook: https://github.com/NousResearch/hermes-agent/issues/149633.

## Proposed single-file catalog change

Target: `NousResearch/hermes-agent`, file `plugin-catalog/handydandy.yaml`.

```yaml
name: handydandy
title: HandyDandy
repo: https://github.com/percyhendrux123-prog/hermes-handydandy
sha: APPROVED_FULL_40_HEX_COMMIT_SHA
description: Session-size notices and structured handoffs for long Hermes sessions.
maintainer: Deploy Axiom
tier: community
category: automation
requires_hermes: ">=0.22.2"
version: "0.4.1"
readme: true
platforms: [macos, linux]
known_issues:
  - Handoff writing uses a normal agent turn and tool approvals; the plugin cannot guarantee the model writes the file.
capabilities:
  provides_tools: []
  provides_hooks:
    - pre_api_request
    - transform_llm_output
    - pre_llm_call
  provides_middleware: []
  requires_env: []
```

## Proposed PR body

### What this plugin does

HandyDandy is an MIT-licensed standalone plugin by Deploy Axiom. It watches estimated request tokens, message count and an optional share of an explicitly configured context window. In `advise` mode (default) it adds one notice suggesting `/new`. In `handoff` mode it asks the agent, on its next normal turn, to finish the request and write a five-section handoff; the user starts the next session from it with `from handoff <path>`.

The only deployment observation offered is:

> 30% of one deployment's input came from one never-reset chat

No production savings are claimed.

### Hermes surfaces

Registered documented hooks: `pre_api_request`, `transform_llm_output`, `pre_llm_call`. No tools, middleware, providers, UI assets, private method calls or core overrides. Supported floor Hermes 0.22.2.

### Rule 13 disclosures

- **Network:** none. No telemetry, self-updater or extra LLM requests.
- **Read access:** own state/lock/events, metadata and first byte of recorded handoff files, settings via `ctx.get_config`, deprecated `HANDYDANDY_*` environment fallbacks, `HERMES_HOME`. No database or transcript reads.
- **Write access:** own `plugin-data/handydandy` state/lock/events/atomic temporary files; optional CSV; agent-written handoff files in the configured `handoff_dir`. Nothing auto-deletes on disable.
- **Host state changes:** none. The plugin never resets sessions.
- **Shell/background:** no shell commands, child processes, timers or daemons.
- **Credentials:** none required or read.
- **Privacy:** the agent is instructed not to copy secrets into handoffs; arbitrary prose is not guaranteed sanitized.

### Verification

See `docs/VERIFICATION.md`.

### Submission checklist (operator completes after approval)

- [ ] Approved full commit exists in the public plugin repo; no placeholder pin remains.
- [ ] Fresh checkout at that commit passes `hermes plugins validate <path> --install-deps`.
- [ ] Entry capabilities, version and `requires_hermes` match the pinned manifest.
- [ ] Only `plugin-catalog/handydandy.yaml` changes in the Hermes catalog PR.
