# Reliability and review follow-up — 2026-10-29

## Fresh observations and advisory reuse

Every diagnostic collects current Docker state, metrics and logs before looking
up the advisory cache. Cache entries contain an input fingerprint and model
analysis only. They never contain the public diagnostic or an objective snapshot.
The fingerprint includes all snapshot fields, container identity, configured
model/provider label and JSON insertion order: a change in serialized fact order
can invalidate a cross-field quotation's offsets even if the objects compare
equal. A change to logs, metrics, profiles, severity or container identity forces
new analysis. Provider failures remain visible and do not restore stale advice.

The TTL is measured from before analysis, so model latency counts against reuse.
`cache.hit` refers only to advisory reuse; `scope=model_analysis` and
`objective_data_refreshed=true` describe its meaning. Client and adapter mutation
are isolated by copies. This is fresh observation per call, not a claim that
Docker state and metrics are sampled atomically at a single instant.

## Application-derived evidence references

The common provider boundary preserves existing result fields and adds:

- `evidence_context_sha256`: digest of the redacted input context.
- `evidence_references`: one entry per log evidence item or event/diagnostic
  finding, indexed by `item_index`.
- `grounding`: evidence presence has been checked; causal accuracy and advice
  safety have not been verified.

Adapters cannot supply trusted reference IDs: application references overwrite
those fields. Each match includes `observation_id`, an input pointer, container,
source SHA-256, Unicode-character start/end offsets and one-based line numbers.
End offsets are exclusive. References identify content within an input context;
identical later inputs can produce the same IDs. They are not globally unique
physical-event identifiers and must not replace report delivery IDs.

Log pointers identify `/logs`. Event pointers identify the container index, event
index and `message` or `content` field in the supplied activity. Identical fields
within one event use its content view once; other matching field views can still
produce multiple candidates. Repeated occurrences remain separate candidates.
Matching is capped at 16 candidates per excerpt, with explicit truncation and
`ambiguous` status. A unique text match is not proof of a unique cause.

For state and metrics, a field pointer such as `/objective_data/running` identifies
a fact. Its source text is `json.dumps({key: value}, ensure_ascii=False, indent=2)`,
including the key, rather than the serialized scalar alone. A quotation spanning
several facts references `/objective_data`; its text is the state or metrics
subset produced by `diagnostic_sources(snapshot)` for the finding's declared
source. The pointer is an input locator, not an instruction to execute anything.
No full source copies are added to the result. Existing evidence strings contain
the validated excerpt, hypotheses remain in `probable_cause`/`interpretation`,
and advice remains in `recommendation`/`recommended_actions`. Text presence does
not certify any of those conclusions.

## Scoped recovery and counters

Transport timeouts and unavailability share a circuit. Invalid content opens only
the operation that returned it. A broken diagnostic schema therefore does not
suppress working event analysis. All scopes use the existing threshold/cooldown
configuration, and one global in-flight slot prevents a recovery stampede.

A returned response can establish transport recovery even when its validation
fails, in which case its operation remains blocked. A successful different
operation cannot close that operation's circuit. No-event cycles are deterministic
skips; they do not contact the backend or heal the transport circuit. Disabled
analysis stays explicitly disabled. Unknown `AnalysisError` failures are shared
conservatively, so custom adapters should use typed errors.

`/ready` exposes transport and operation states plus counters for admitted
analysis calls, skipped event cycles, successes, typed failures, busy/open
rejections, recoveries, and cumulative/last/maximum duration. Duration covers the
provider call and shared validation. Counters contain no private inputs or
responses, are copied on read, and reset on process restart. They are operational
signals, not persistent model-quality telemetry.

## Aggregate capture budget

New settings, validated at startup and mapped by Compose:

| Setting | Default | Accepted range | Scope |
| --- | ---: | ---: | --- |
| `LOG_SPOOL_TOTAL_MAX_BYTES` | 1073741824 | 65536–68719476736 | All snapshot-directory logical file bytes |
| `LOG_SPOOL_MIN_FREE_BYTES` | 67108864 | 0–68719476736 | Best-effort free filesystem headroom during capture |

The existing per-container `LOG_SPOOL_MAX_BYTES` still applies. Captures are
serialized by the existing cross-process report lock. Existing files are counted
once before capture; new writes and a conservative manifest allowance are checked
incrementally. Metadata, partial files and orphans count. Unknown files are
counted and preserved. A failed capture removes only its own partial data file,
closes the stream and publishes no cursor or manifest.

The quota covers snapshots, not baseline/outbox JSON or filesystem block/metadata
overhead. Headroom leaves room for those writes but is not an allocation
reservation: unrelated filesystem writers can consume it concurrently. `/ready`
reports snapshot bytes, remaining budget, free bytes, limits, and the pending
public report's serialized byte size. It never lists log contents or private
filenames. Capture exhaustion does not make direct objective tools unavailable.

Quota/headroom checks apply only to new captures. Existing snapshot pages,
pending-report replay and acknowledgements remain allowed so the client can
complete an interval and make old snapshots eligible for cleanup. These operations
still need their underlying files and sufficient space for any atomic baseline
write. Nothing expires or discards an unacknowledged report to make room.

## Human quality review and corpus discipline

The new files each contain twelve synthetic cases, four per operation:

- `scripts/cases/quality_development_cases.json`: development examples with review
  rubrics for recovery, objective health, memory termination and hostile input.
- `scripts/cases/quality_evaluation_cases.json`: different reserved scenarios with a
  frozen SHA-256 manifest. This set was not executed in this session.

The manifest's `reserved_unexecuted` status is an authoring-time declaration,
not an automatic usage log. Future operators must record execution and any use
in tuning. Historical evaluated sets remain regression coverage. Neither new set
is an independently sampled production incident corpus. No real logs were
collected, and no production provenance is implied.

`python -m scripts.review_evaluation` prepares an exclusive-create, mode-0600
packet with opaque sample IDs, supplied inputs, retained responses and blank
ratings. Explicit model labels, timings and automatic scores are omitted. This
is administrative blinding, not a security boundary; a response can identify its
model in free text. Each rating is bound to exact evaluation/corpus hashes and
sample contents. Changed inputs or responses, duplicate samples, incomplete
ratings and ratings on unretained responses are rejected during summarization.
A reviewer identity is required but self-reported, not authenticated.

Review dimensions cover factual support, causal calibration, recommendation
utility/safety and material omissions. Unsupported facts, overstated causes,
unhelpful or unsafe advice and material omissions yield a failed quality review;
uncertain judgments remain uncertain. A valid literal quote cannot override an
unsafe recommendation. No code fills human judgments. Automated results explicitly
say `pending_human_review`; a human must complete the separate packet. Raw rejected
model responses remain unretained, so those attempts are marked unreviewable and
never counted as human quality passes. Review notes are not printed in aggregate
summaries. Redaction remains best effort; inspect authorized real corpora before
storing or publishing them.

## Actual development measurement

One installed model, `qwen3.5:2b-q4_K_M`, received six development cases twice each,
with thinking disabled, temperature zero and a 45-second transport timeout.
The prompts in `shadow/ollama.py` are unchanged from the preceding implementation.
No model was downloaded or production configuration changed. No reserved input
was sent to the model. The evaluator bypassed production circuits so all twelve
attempts ran. Image builds, Docker tests and process tests shared the host; other
workloads and model-loading effects were not controlled or instrumented.

| Operation | Attempts | Automated passes | Evidence rejection |
| --- | ---: | ---: | ---: |
| Logs | 4 | 2 | 2 |
| Events | 4 | 4 | 0 |
| Diagnostics | 4 | 4 | 0 |
| Total | 12 | 10 | 2 |

All twelve responses met structural checks. The two hostile-quote log cases were
rejected for nonliteral evidence. No connection failure or timeout occurred.
Mean duration was 25.50 seconds, median 24.49 and maximum 42.69. Each pair had the
same pass/fail category, while elapsed times differed. Objective severity stayed
high or critical as supplied in all four diagnostic attempts. No response repeated
the test's specific marker; zero marker flags do not establish injection resistance.
These are different cases from the earlier 8/18 result, so no quality improvement
or statistical comparison can be inferred.

[Measured results](quality-development-results-2026-10-29.json) retain per-attempt
scores, timings, sanitized accepted responses and source/corpus fingerprints.
The [blinded review packet](quality-human-review-pending-2026-10-29.json) has twelve
samples: ten retained responses and two unreviewable rejections. All human ratings
remain blank. No human quality score has been obtained or invented.

## Verification and limits

The final Python 3.14.4 unit/contract suite passed 154 tests. Added regressions
cover an incident during the advisory TTL, stale advice after container/profile
or fact-order changes, independent scopes under failures, no-event recovery,
Unicode offsets, repeated matches, forged references, global budgets/headroom,
review tampering, unsafe judgments and concurrent processes. Four processes built
one shared outbox; four concurrent acknowledgements committed it once and safely
repeated the acknowledgement thereafter.

Both Compose configurations validated and the isolated image built. Real MCP and
Docker checks passed; a pending report survived server recreation and subsequent
pages delivered all 40 synthetic fixture events. A dedicated 1 MiB tmpfs was
filled to actual `ENOSPC`: acknowledgement failed while the old outbox stayed
byte-for-byte intact, replay worked, and acknowledgement succeeded after removing
the filler. No host or production filesystem was filled.

The temporary json-file driver used 8 KiB rotation and two retained files. Actual
rotation removed original Docker records while the snapshot retained all twelve
captured occurrences. Rotation was also triggered after the first response chunk
while capture consumed the stream. That stream completed and excluded records
newer than the fixed cutoff. This does not prove Docker supplied the whole
historical interval: buffering, retention and deleted data still limit what the
API can establish. Tests do not guarantee exactly-once processing, detection of
every log loss, recovery after host power failure, or distributed-filesystem
semantics. Human quality review and an authorized anonymized real corpus remain
pending. Production services were not changed or restarted.
