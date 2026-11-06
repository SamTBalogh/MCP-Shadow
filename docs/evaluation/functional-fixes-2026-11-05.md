# Functional observation regressions — 2026-11-05

This local validation is separate from the earlier review's 160 passing tests
and GitHub CI result. No new GitHub CI run, Docker deployment or model inference
was performed for this change.

## Reproduction and changes

The existing synthetic reproduction script was executed before editing. It
confirmed all three reported behaviors: failed statistics appeared as zeros;
an inspection failure followed by successful logs saved `not_found` and raised
high severity; two high samples from an old container plus one from its
replacement raised a sustained-consumption alert.

- Inspection failures now produce explicit uncertainty and preserve the last
  valid container baseline. Only a typed absence result (including HTTP 404 on
  container inspection) produces `not_found`. HTTP status classification does
  not inspect exception text. Recovery compares with the retained valid state.
- Failed statistics expose availability and error fields with null measurements.
  State and logs still contribute their valid findings. Reporting, evidence
  sources and the provider prompt preserve this distinction. The compact public
  report includes collection error details and marks incomplete coverage.
- Container IDs flow through inspection, statistics, observation and persisted
  state/metric entries. Counters reset on replacement or missing identity.
  Legacy baselines remain readable; their unidentified metric history is not
  carried into the new sequence. Missing samples break consecutiveness.

The implementation touches `shadow/collector.py`, `containers.py`,
`diagnostics.py`, `discovery.py`, `docker_client.py`, `metrics.py`, `reporting.py`,
`validation.py`, `evidence.py` and `ollama.py`. Public behavior is documented in
`README.md`. Regression coverage is in `tests/test_observation_failures.py`;
the existing sustained-metrics fixture in `tests/test_components.py` now has an
explicit identity. New publishable files are listed in the publication manifest.

## Validation

Interpreter: Python 3.14.4, using an existing local dependency environment.

```sh
python -m unittest discover -s tests -v
git diff --check
```

Result: **173 tests passed**, including **13 new regression tests**. The first
seven new regression methods were also run before the fixes and failed, proving
that they expose the original behaviors rather than only exercising the fixes.

Coverage includes repeated transient failures with successful log collection and
baseline save/reload, first-observation failure, recovery with unchanged historical
restarts/OOM, a real subsequent restart, HTTP 404/403/503 and transport timeout,
stats failure/recovery, null values, preserved state/log severity, provider input
and evidence validation, public report errors, replacement, same-ID continuity,
legacy references and missing IDs. HTTP and provider responses are synthetic
in-process fixtures; no real model is called.

Separate local logs: `/tmp/mcp-shadow-fixes-regressions-before.log` and
`/tmp/mcp-shadow-functional-fixes-unittest.log`. Earlier review artifacts were
not overwritten.

## Limits

Validation used synthetic data and temporary baseline files only. No production
services, private `.env`, real logs, Docker stack or model inference were used.
Docker integration and remote CI remain unexecuted for this patch. Changes are
left local for review without commits, push or pull request.
