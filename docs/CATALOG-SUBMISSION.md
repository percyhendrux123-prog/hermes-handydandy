# Catalog submission (0.4.0)

## PR title

`catalog: add handydandy — threshold-triggered structured gateway carryover`

## Proposed single-file catalog change

Target: `NousResearch/hermes-agent`, file `plugin-catalog/handydandy.yaml`.

```yaml
name: handydandy
title: HandyDandy
repo: https://github.com/percyhendrux123-prog/hermes-handydandy
sha: APPROVED_FULL_40_HEX_COMMIT_SHA
description: Session-size notices, structured handoffs, and opt-in automatic gateway carryover.
maintainer: Deploy Axiom
tier: community
category: automation
requires_hermes: ">=0.22.2"
version: "0.4.0"
readme: true
platforms: [macos, linux]
known_issues:
  - Carryover requires in-process plugins; live gateway/store objects are placeholders under host isolation.
  - Carryover uses disclosed private gateway reset, authorization and route-key helpers until a public reset API is available.
  - Handoff writing uses a normal agent turn and tool approvals; no reset occurs until a bounded structured file is ready.
capabilities:
  provides_tools: []
  provides_hooks:
    - pre_api_request
    - transform_llm_output
    - pre_llm_call
    - pre_gateway_dispatch
    - on_session_reset
  provides_middleware: []
  requires_env: []
```

## Proposed PR body

### What this plugin does

HandyDandy is an MIT-licensed standalone plugin by Deploy Axiom. It watches estimated request tokens/message count and supports an explicit model-context fraction. The default `advise` and manual `handoff` modes stay opt-in/manual as before. New `carryover` mode requests a short structured handoff through the existing agent turn; after a valid file is observed, the next ordinary gateway message starts fresh with that bounded handoff rather than the old transcript. Users do not type `/new` or copy a handoff manually.

The problem is operational: gateway users often continue a single never-reset chat. The only deployment observation offered here is:

> 30% of one deployment's input came from one never-reset chat

No production after-cost measurement or percentage savings is claimed. The mechanical after-state is a fresh session with at most 6,000 characters of structured handoff content plus the current inbound event, not a full transcript. Resetting too frequently can reduce prompt-cache benefits; operators must measure their own workload.

This is an independent plugin lineage, not a fork/rebrand of `hermes-session-reset-policy`. That plugin restores time-driven boundaries and now has optional excerpt carryover. HandyDandy's distinction is size thresholds and an agent-written five-section handoff. Both orderings with the actual time-reset plugin are tested. Disable that plugin's optional excerpt carryover if you want only the structured seed.

### Hermes surfaces and compatibility

Registered documented hooks: `pre_api_request`, `transform_llm_output`, `pre_llm_call`, `pre_gateway_dispatch`, `on_session_reset`. No tools, middleware, providers, UI/desktop assets or hidden registrations.

Supported/tested floor is Hermes 0.22.2; this corrects the previous overly conservative 0.22.5 manifest floor. Stable-source and current-source integration results and limitations are in `docs/VERIFICATION.md`. Older versions are not claimed supported on the basis of hook presence alone.

**Private call disclosure:** the ingress hook provides live gateway/store objects but no public full-reset API. Carryover calls the handed gateway's `_is_user_authorized_for_source`, `_session_key_for_source`, `_handle_reset_command`, and the store's `lookup_by_session_key`. It does not replace/wrap/rebind core functions or private dictionaries. It reuses the host's complete reset funnel rather than copying cache/scope/delegation teardown or silently falling back to an incomplete store-only reset. Absent helpers suppress automatic rotation and preserve normal inbound dispatch. These dependencies are not stable public API; reviewer acceptance is explicitly requested, not assumed. If a public reset seam is required for admission, this candidate must wait for that API rather than patch around it.

`plugins.isolation: in_process` is required for automatic carryover. Host isolation supplies gateway/store placeholders. Do not weaken a pooled-host isolation policy to enable this plugin mode.

### Safety and idempotency

- Existing-turn handoff, under ordinary tool approvals; no extra plugin model calls or approval bypass.
- Original inbound event is not skipped, rewritten, re-sent or re-enqueued. A text-free copy goes only to the reset funnel so the inbound is not treated as a session title.
- Ownership-profile filter, authorization check before ingress mutation, active-turn deferral and explicit-command bypass.
- Durably saved reset intent before rotation and source/target IDs after confirmation. A reset that throws after changing the route is adopted, not retried as another reset. A process death before completion persistence leaves a reserved intent; the plugin never infers the intended replacement from later route occupancy across a possible manual clean boundary. Inbound still dispatches normally, but uncertain carryover needs operator recovery.
- Intervening active/queued work invalidates handoff readiness; a rewritten, newer handoff is required. Manual reset cancellation covers unresolved intents for the route, with an in-memory denial for a transient cancellation-save failure.
- Seeds are bounded structured background, not new authority. The seed is available for first-response retries, then consumed on a response transform. The old transcript remains in history.
- Other reset plugins may still reset before a handoff is ready; no plugin can seed a handoff that has not been written. No claim of universal compatibility with arbitrary mutating ingress plugins.

### Rule 13 disclosures

- **Network:** no direct plugin network calls, telemetry, self-updater or extra LLM requests. Hermes's ordinary model/tool/lifecycle operations remain its own.
- **Read access:** own state/lock/events, bounded handoff files, environment non-secret setting overrides, settings via `ctx.get_config`, and hook-supplied routing-entry IDs/active-turn status. No direct database reads, transcript loading, unrelated work files or credential stores. Agent handoff generation uses its existing conversation/tools.
- **Write access:** own `plugin-data/handydandy` state/lock/events/atomic temporary files; optional CSV; agent-created handoff files in configured `handoff_dir`. State includes bounded handoff text and route/source/target IDs. Events contain metadata, not conversation text. Nothing auto-deletes on disable.
- **Host state changes:** opting into carryover invokes Hermes's `/new` funnel: routing/session persistence, old-session closure (not transcript deletion), cached-agent eviction, conversation-scope cleanup, delegation interruption and lifecycle hooks. Other plugins reacting to those hooks can have their own effects.
- **Shell/background:** no shell commands, child processes, timers, daemons or background agent restarts.
- **Credentials:** no keys required, credential files read/stored, OAuth flows or billing actions.
- **Privacy:** the agent is instructed not to copy secrets/personal data. Arbitrary handoff prose is not guaranteed sanitized; review before sharing. No client data in repository tests/docs.

### Verification

See `docs/VERIFICATION.md` for exact commands/results, the stable Hermes commit, current Hermes commit and time-reset-plugin pin. Repository and opt-in integration tests cover threshold → file → fresh session → seed; original inbound preservation; double-reset prevention; guards; IO/error cases; concurrent engines; and both time-plugin orderings. Catalog validator and Plugin Doctor outputs are recorded there, including the host-isolation warning.

Tests use synthetic local sessions only. UI/runtime services and authorization/launcher services are test doubles. There is no live Telegram/provider/model test and no production savings measurement. Maintainer review of the exact public SHA is still required.

### Submission checklist (operator completes after approval)

- [ ] Approved full commit exists in the public plugin repo; no placeholder pin remains.
- [ ] Fresh checkout at that commit passes `hermes plugins validate <path> --install-deps` and doctor; warnings reviewed.
- [ ] Entry capabilities, version and `requires_hermes` match the pinned manifest.
- [ ] Only `plugin-catalog/handydandy.yaml` changes in the Hermes catalog PR.
- [ ] Private reset dependency and in-process requirement accepted by catalog reviewer, or a public seam lands first.
- [ ] No claim of measured production savings, official endorsement, exclusive continuity capability or live deployment verification.
