# Observation quality and recovery

## Expected inventory

`EXPECTED_CONTAINER_NAMES` is an optional comma-separated list of exact Docker
container names, for example `web-1,db-1`. Empty preserves discovery through
`io.shadowofintent.mcp.enabled=true`. Reports observe the union of discovered
labelled containers and configured expectations. Configuration never authorizes
reading an unlabelled container. If a configured name exists without that label,
inspection reports a query error; only absence from a successful Docker inventory
or an inspection 404 confirms `not_found`.

Expectations survive removal because they live in configuration. Remove a name
from the configuration and remove its enabled label (or delete that service) to
retire it. A previously expected service no longer in either inventory produces
`retired_service`. A changed known Docker ID produces `recreated_service`, without
subtracting restart counters or OOM flags belonging to different instances.
Changing configuration requires restarting MCP Shadow. Names must be real Docker
names, not the display-name label. Renaming a container requires updating its
expectation. There is no automatic learning or permanent retention of every
container ever discovered.

## Timed sustained consumption

CPU and memory each require all of the following:

- At least `SUSTAINED_METRIC_SAMPLES` high samples (default 3).
- At least `SUSTAINED_MIN_DURATION_SECONDS` between the first and current high
  sample (default 120).
- No gap above `SUSTAINED_MAX_SAMPLE_GAP_SECONDS` (default 300).
- The same known container ID and valid measurements for that resource.

Equal observation timestamps do not add samples. A backward clock, missing ID,
replacement, missing measurement or excessive gap starts a new sequence. Old
baselines lacking sequence start times also start fresh. CPU and memory sequences
are independent. No background scheduler is added: callers must poll frequently
enough to supply the samples. Counts and duration describe sampled observations;
they do not prove uninterrupted load between calls. A wall-clock adjustment
forward beyond the maximum gap resets the sequence.

Persisted metric entries include `high_cpu_since`, `high_memory_since`, their
`*_duration_seconds`, configured timing limits and `sustained_high_cpu` /
`sustained_high_memory`. Summary counts use both time and sample requirements.

## Partial and stale statistics

Inspection, statistics and diagnostic output carry `container_id`. Statistics,
diagnostics and observation snapshots carry these metric fields:

| Field | Meaning |
| --- | --- |
| `metrics_available` | All four measurement groups are available. |
| `metrics_error` | Errors for unavailable groups; empty on complete success. |
| `metric_status` | Availability, error and collection time for `cpu_percent`, `memory`, `network`, `processes`. |
| `metrics_observed_at` | When the statistics response was processed. |
| `metrics_sampled_at` | Docker's `read` timestamp, if supplied. |

Missing, negative, nonfinite, invalid or uncomputable measurements are null.
Measured zero remains zero. Unavailable memory/network groups retain their object
shape with null values. A missing CPU delta or nonpositive memory limit is not a
zero measurement. Valid groups remain usable when another group fails, including
for objective severity and sustained counters. State and log findings survive
statistics failures. If Docker supplies a timestamp older than
`METRIC_MAX_AGE_SECONDS` (default 300), invalid, or more than five seconds in the
future, the response's measurements are unavailable. When Docker omits `read`,
only collection time is known; source freshness cannot be independently proven.

Provider inputs and evidence references include the quality metadata. Nulls are
never evidence of low consumption or good health. A stopped container may skip
statistics entirely; its observation has unavailable values without a query
failure. Advice remains optional and cannot change objective severity.

## Monitor health

Reports include `monitoring`, with `last_attempt_at`,
`last_complete_observation_at`, inspection coverage, per-service
`last_valid_at`, current availability and query-error counts by operation.
Completeness means the current observation has services and no query errors;
log pagination completeness remains `baseline.complete_logs`. Confirmed absence
is a valid observation, so coverage is not a measure of service health.

`/ready` adds ages in seconds for attempts, complete observations, each service's
last valid observation, and the pending report. Unknown timestamps yield null.
For an unacknowledged report it exposes the staged observation; replay does not
refresh its timestamps. Monitor metadata can be saved even when objective/log
checkpoints cannot advance. `baseline.monitoring_saved` distinguishes this from
advancing the objective baseline and is false until acknowledged delivery is
committed. Existing clients must continue using `baseline.saved` and
`complete_logs` for their original purposes.

## Explicitly closing an incomplete log interval

If an interval cannot be completed, such as after a member container is replaced:

1. Durably process the available report and acknowledge its delivery as usual.
2. Read its `baseline.interval_id` and decide whether losing the remaining log
   coverage is acceptable.
3. Call `close_incomplete_log_interval(interval_id=..., reason=...)` with that ID
   and a nonempty reason of at most 500 characters.
4. Request the next report to resume collection from the closed interval's end.

The operation refuses a mismatched ID or an outstanding delivery. It records
the interval, affected container names, timestamp, reason and
`coverage_lost=true` in `log_gaps`. Repeating a recorded ID returns
`already_closed=true`, including after restart. Baseline replacement and audit
record are atomic. Docker is never modified. The checkpoint advances only to the
old interval's cutoff; later events are not intentionally skipped.

Captured snapshots and their saved cursors remain referenced in the audit entry
and are protected from automatic cleanup. Public reports show the coverage gaps
without embedding private snapshot cursors. Captures still consume the existing
spool quota. Audit entries are capped at 128; there is no automatic deletion or
expiry. Archive baseline and snapshots together and arrange offline retention
before exhausting this limit or the storage budget. Closing an interval accepts
unread coverage loss; it does not deliver all remaining captured pages or recover
logs Docker has already discarded.

## Synthetic validation

Run `python -m unittest discover -s tests -v`, then
`bash scripts/run_integration.sh --resilience`. The latter uses `.env.example`,
disabled analysis, a fresh Compose project, its own network/volume and automatic
cleanup. It verifies normal observation, proxy failure/recovery, pending report
replay after restart, container replacement, explicit interval closure with
retained snapshots, disappearance and return. The existing full integration
command remains `bash scripts/run_integration.sh`. CI runs both scenarios.
