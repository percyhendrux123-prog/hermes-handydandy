# Verification evidence (0.4.1)

HandyDandy 0.4.1, based on 0.4.0 (`cb5bab5`). Carryover mode removed per catalog review 5789489691 on PR #148914. Results below were produced locally before publication on Hermes Agent v0.22.7 (`b200f5bc60e3`).

## Test suite

```bash
TMPDIR=<managed-scratch> .venv/bin/python -m pytest -q tests
```

```text
32 passed in 0.05s
```

Coverage: thresholds (token, message, context share), advise and handoff flows, stale/empty/unreadable handoff files, cross-process state locking, corrupt/malformed state fail-closed, legacy relay migration, the plugin's registered hooks with no model-metadata lookup, and the 0.4.1 changes:

- A saved `mode: carryover` runs as `handoff`, logs one `carryover_removed` event and one warning, and does not repeat after a restart.
- 0.4.0 carryover state (seeds, targets, reset intents) loads as manual handoff; no seed is injected.
- Registered hooks never read any attribute of a gateway or session-store object passed to them.
- A source scan of the package (`.py`, `.yaml`, `.md`) finds none of the private gateway method names used by 0.4.0.
- Config settings win; `HANDYDANDY_*` environment variables apply only when a setting is missing, with one deprecation warning per process.

Tests use synthetic sessions and files only. No live gateway, model, account or transcript was used. No production savings are claimed.

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

Validation passed.
```

The 0.4.0 isolation note about live gateway/store placeholders no longer appears: no registered hook receives them.

Observed doctor output (exit 0):

```text
Plugin Doctor: .
  manifest: handydandy 0.4.1 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 3 hook(s)
```

`git diff --check` passed. No npm build applies to this stdlib Python plugin.

## Review/publication boundary

Passing tests and validation is not a catalog-maintainer acceptance decision. The fresh public checkout at the pinned SHA should be re-validated before re-pinning.
