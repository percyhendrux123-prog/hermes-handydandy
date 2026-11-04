# Verification evidence (0.4.0)

HandyDandy 0.4.0, based on 0.3.0 (`7cc035707f18590db3957fbcc73e3e4064bc8f38`). All results below were produced before publication.

## Source/runtime matrix

| Source under test | Exact commit | Test result |
|---|---|---|
| Stable Hermes `v0.22.2` | `d923a5eb487a17c0a68801bba9ef0f56c81040a8` | `56 passed in 0.39s` |
| Installed Hermes 0.22.5 source | `bf0203a794a07aac3ff764972ab9087baa4e4f4e` | `56 passed in 0.38s` |
| Time-reset plugin 0.4.0 in both integration cases | `76ad1aecc4067f74158ca69c40d74c56451ebef2` | Both callback orderings included above |

The plugin core unit suite also runs on the existing Python 3.9.6 test venv:

```text
51 passed, 5 skipped in 0.10s
```

The five skips are explicitly opt-in Hermes integration cases, not silently missing acceptance coverage: they are executed in both matrix rows with Python 3.14.9, pinned Hermes source paths and the host's existing dependency libraries. This is a stable-source API/runtime test, **not** a clean dependency installation of historical Hermes or a claim to the earliest release that ever had these hooks. The supported/tested floor is 0.22.2; older versions remain unclaimed.

## What was actually exercised

- Threshold -> notice -> handoff instruction -> synthetic five-section handoff file -> next ordinary inbound -> fresh session -> bounded background context.
- The actual Hermes ingress method, full `/new` reset mixin, pre-LLM context collector and routing `SessionStore`, including real temporary SQLite/transcript persistence. The original inbound text is appended once to the actual fresh-session transcript by the test consumer. Text, attachments, reply anchor and object identity remain unchanged through ingress.
- Actual `hermes-session-reset-policy` reset callback code, configured for an overdue idle reset, before and after HandyDandy's callback. Exactly one reset/cache-eviction/scope-clear boundary in each ordering; repeated ingress does not reset the replacement again.
- Existing advise/handoff modes and legacy relay migration regressions; no private model-metadata lookup.
- Active-turn, unauthorized, other-profile, internal-event, slash-command and missing-helper guards; explicit `/new` remains a clean break; mode change disables rotation.
- Invalid, empty-section and oversized handoffs; retry until valid; failed pre-reset state save suppresses reset.
- Concurrent engines and reload: one reset. Concurrent inbound waits until a pending rotation completes rather than releasing a follower into the old session.
- Exception before rotation retries safely; exception after rotation adopts the actual replacement. Seed retries until a response transform consumes it.
- Unstamped sources resolve to the active named profile, not an assumed default profile.
- Reviewer-authored manual clean-boundary regressions (lost completion and failed cancellation save), active/queued stale-handoff invalidation, refreshed-file progress and durable invalidation across restart. Ambiguous post-crash route occupancy is never used to infer a seed target.

### Test doubles and unobserved behavior

Tests use synthetic files/messages only. The gateway authorization service, UI banners, running-agent services/cache/scope teardown implementations, transport, and launch/dependency bootstrap are controlled doubles. Real reset code is checked to call the boundary services once; this is not an end-to-end test of an actual running model/cache/delegation process. The lifecycle dispatcher is wired to the candidate callbacks by the harness. No real Telegram message, live transcript, client account, paid API or model-generated handoff was used. No production cost reduction or real power-loss/disk-full outcome is claimed.

## Reproduce

Core suite, from this repo:

```bash
TMPDIR=<managed-scratch> .venv/bin/python -m pytest -q tests
```

Integration requires a Hermes-compatible Python environment with its dependencies plus pytest, a real source checkout and a pinned checkout of the time-reset plugin. Set `HERMES_HOME` **before** importing Hermes; use a fresh isolated demo home, never a live profile:

```bash
export HERMES_HOME=<isolated-demo-home>
export HERMES_COMPAT_ROOT=<Hermes-source-checkout>
export RESET_POLICY_ROOT=<session-reset-policy-checkout>
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export TMPDIR=<managed-scratch>
PYTHONPATH="$HERMES_COMPAT_ROOT" <Hermes-compatible-python> -m pytest -q --tb=short tests
```

The worker used Python 3.14.9 with existing read-only Hermes dependency site-packages and the repo's pure-Python pytest site-packages added to `PYTHONPATH`; it did not alter a managed Hermes environment. Exact worker runner/logs are task artifacts under its isolated workspace. `gateway.session.__file__` is asserted to resolve inside `HERMES_COMPAT_ROOT`, preventing a silent current-source import from being presented as the stable-source test.

## Catalog checks

```bash
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
git diff --check
```

Observed validator output (exit 0):

```text
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.22.2' parses
✓ config schema — shape valid
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no tools to check
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
◆ Isolation — runs in the plugin host (plugins.isolation: host)
  · hook pre_gateway_dispatch receives the live gateway runner and session
store; those fields are placeholders in the host

Validation passed.
```

This host-isolation note is material: carryover requires in-process plugin execution. Validation does not prove automatic rotation works in host isolation and does not approve weakening a deployment's isolation policy. Private helper dependencies are explicitly disclosed even though indirect calls did not produce a private-call scanner warning.

Observed doctor output (exit 0):

```text
Plugin Doctor: .
  manifest: handydandy 0.4.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 5 hook(s)
```

`git diff --check` passed. Runtime source search found no `os.system`, `shell=True`, `eval`, pickle loading or interpolated SQL calls. No npm build/audit applies to this standalone stdlib Python plugin.

## Review/publication boundary

Mandatory codereview-profile adversarial findings/fixes and final rerun results are supplied in the structured task handoff. Passing tests/validation is not a catalog-maintainer acceptance decision. The private full-reset seam may still need a public API before admission.

