# Observation improvements — local validation, 2026-11-06

This record is separate from the previous 173-test functional-fix verification
and the earlier 160-test review. Existing working-tree fixes were preserved.

## Implemented scope

1. Optional configured expected inventory, explicit retirement and recreation,
   and continued enforcement of enabled labels.
2. Sustained CPU/memory alerts gated by duration, maximum gap, sample count and
   container identity, with conservative handling of old baselines and clocks.
3. Shared validation of partial, invalid and stale statistics; per-group
   availability, timestamps, nulls and preserved valid state/log/metric findings.
4. Explicit, idempotent closure of incomplete intervals, recording lost coverage
   atomically while retaining captured snapshots and their cursors.
5. Persisted observation health, coverage, per-operation query failures and
   freshness ages in readiness, including pending report age.
6. A new isolated Docker resilience sequence and CI step, alongside the existing
   integration suite.

Public fields, defaults, tools and retention limits are documented in
[the observation contract](../observation-contract.md). README, example environment,
Compose configuration, script documentation and publication manifest were updated.

## Executed validation

- Python 3.14.4: `python -m unittest discover -s tests -v` — **186 passed**,
  including 13 new test methods in `test_observation_improvements.py`. Existing
  tests were adapted to explicit sample timestamps and the new tool/metadata
  contracts while retaining their original assertions about observation safety.
- `bash scripts/run_integration.sh --resilience` — **passed** against Docker
  Engine 29.1.3. Normal observation, actual proxy outage/recovery, unacknowledged
  replay after restart and replacement, explicit closure with retained captures,
  disappearance and return all passed.
- `bash scripts/run_integration.sh` — **passed**. Built-image dependencies,
  proxy read restrictions, official MCP SDK queries, 40 generated events,
  replay/acknowledgement across restart, actual ENOSPC, Docker log rotation and
  unavailable-provider fallback passed.
- `git diff --check` and `bash -n scripts/run_integration.sh` — **passed**.

Local output is recorded separately in:

- `/tmp/mcp-shadow-improvements-unittest.log`
- `/tmp/mcp-shadow-improvements-docker.log`
- `/tmp/mcp-shadow-improvements-full-integration.log`

Both Docker runs used `.env.example` and separate temporary Compose projects
with their own internal network and volume; cleanup removed their containers,
networks and volumes. No production configuration or private `.env` was read.
Only fixture services and synthetic data were used. The new sequence disabled
analysis; the existing suite verified unavailable-provider fallback without
running a model. No inference was performed.

## Remaining boundaries

The updated GitHub Actions workflow has not been executed remotely for this
working-tree change. No production deployment, commit, push or PR was performed.
Expectations require explicit configuration using actual Docker names. Sampling
remains request-driven and cannot prove continuous load between observations.
Freshness cannot be independently verified if Docker omits its sample timestamp.
Recovery accepts missing coverage; it neither reconstructs lost logs nor delivers
all unread archived pages. Retained captures consume the spool quota, and the
audit history has a 128-entry cap requiring offline archival/retention work.
