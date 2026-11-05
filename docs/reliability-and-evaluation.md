# Analysis and report reliability

## Provider contract and selection

The application always calls `ManagedProvider(ValidatedProvider(adapter))`.
`AnalysisProvider` keeps its three existing operation signatures. The wrapper
redacts strings recursively, passes independent copies, and validates every
result. Ollama also uses the same validators against the actual bounded context
sent to the model. An alternative adapter which selects a smaller context must
validate against that context itself; the outer wrapper cannot observe an
adapter's private request construction.

Log results require severity, a nonempty summary, literal evidence, string lists
of hypotheses/recommendations and finite confidence in [0, 1]. Events require
complete findings with evidence from the named container. Diagnostic findings
must quote the declared state, metrics or logs source. State and metrics sources
are indented JSON strings from `diagnostic_sources(snapshot)`; quote the decoded
string, not its JSON escaping. A single pair of wrapping quotation marks may be
accepted if its content is an exact excerpt. Whitespace and invented paraphrases
are not normalized into evidence. Evidence matching proves text presence only;
it does not establish impact, causality or the correctness of advice.

Malformed responses raise `AnalysisFormatError`; unsupported evidence or limited
consistency checks raise `AnalysisValidationError`. Both derive from
`AnalysisError`. There is no silent conversion into a healthy diagnosis. MCP
errors include a category; diagnostics and reports retain objective results
and show model failure in their advisory section. `final_severity` always equals
`objective_severity`, including when the model disagrees or fails. The limited
English contradiction heuristic is not a complete semantic validator.

`ANALYSIS_PROVIDER` accepts `ollama`, `disabled`, or `package.module:factory`.
An installed factory is explicitly trusted Python code, not user log content.
It receives `settings` and `http` keyword arguments and must return an object
implementing the three operations. Missing imports, bad factories and malformed
selectors fail at startup. A factory may supply `availability()` returning a
status string; otherwise readiness reports `unknown`. No remote adapter or
credentials have been added. `disabled` is an operational switch, not an AI
integration. Ollama owns its `/api/tags` readiness check.

Timeouts and unavailable transport share a process-local circuit across operations.
Malformed or unsupported responses open only their operation's circuit. Both use
`ANALYSIS_FAILURE_THRESHOLD` (default 2) and `ANALYSIS_COOLDOWN_SECONDS` (default
30). Busy/open rejections do not increment failure streaks. One analysis may be in
flight, including during recovery. A received but invalid response can establish
transport recovery while leaving that operation blocked. Another operation's
success cannot heal a blocked operation. Empty event cycles skip analysis and do
not establish transport recovery. Unknown base `AnalysisError` failures remain
conservatively shared; adapters should use the typed error classes.

Readiness exposes transport/operation states, aggregate `degraded` status when
only an operation is blocked, and counters for attempts, skips, successes,
timeouts, unavailability, validation failures, rejections, recovery and cumulative,
last and maximum elapsed analysis time. These contain no inputs or model text.
Counters and circuits are per-process and reset on restart. Readiness probes do
not close circuits. Adapters must bound their own I/O; no wrapper can forcibly
cancel arbitrary Python code. Ollama transport timeouts are not an end-to-end
wall-clock deadline across every network phase.

Diagnostic observations are always refreshed. Only the advisory model result is
cached, with a fingerprint of all snapshot fields, container identity, serialized
fact order and the configured model/provider label. Changed inputs, changed
identity or TTL expiry invalidate reuse. Failed responses are never cached.
Copies prevent both adapter and client mutation from corrupting observations or
cached advice. API cache metadata identifies `scope=model_analysis` and
`objective_data_refreshed=true`. Identical inputs can reuse advice while Docker
is still inspected on every call.

The application adds content-derived evidence references and explicitly records
that causal accuracy and recommendation safety remain unverified. See the
[follow-up reference contract](evaluation/reliability-followup-2026-10-29.md) for offsets,
ambiguity and source serialization.

## Snapshot pages

New fixed log intervals are captured once per container through Docker GET logs.
Decoded payload bytes are written incrementally to mode-0600 snapshot files in
`STATE_DIR/log-spool`. Only a complete stream within `LOG_SPOOL_MAX_BYTES` gets a
manifest. Its file is synchronized before the atomic, synchronized manifest is
published. The default per-container interval limit is 268435456 bytes; startup
accepts 65536–4294967296. No checkpoint is advanced on incomplete frames, disk
failure or the size limit. First capture latency is proportional to the whole
retained interval; this design trades initial disk I/O for bounded-memory,
constant-prefix-cost resumes. It does not support indefinitely large intervals:
provision enough disk and log retention, and choose reporting intervals that fit.

A saved cursor identifies the immutable snapshot and payload byte offset.
Pages seek to the containing 64 KiB block and verify only the blocks read against
manifest SHA-256 hashes. Earlier blocks are not rehashed. Complete multiline
events, including repeated occurrences with identical timestamps/content, stay
on one page; very large individual events fail explicitly. UTF-8 errors are
reported rather than replacing evidence bytes. Block hashes detect accidental
corruption in accessed data; a privileged writer that can rewrite data and its
manifest is outside this integrity model. Previously consumed blocks need not
be read again. Saved legacy offset/hash cursors finish using prefix verification;
they cannot be converted safely into an independently fetched snapshot.

A snapshot with a saved cursor must exist and match the interval and container.
It is never silently fetched again. Opening an interval fixes its managed names
and Docker IDs, including containers whose first capture fails. A missing or
replaced member is reported as a log error and keeps the interval open, even if
that member had already delivered its last page. Newly managed containers join
the next interval. Older pending checkpoints keep their saved identities and
seed missing membership from current discovery on their next collection.
They cannot recover an identity that was never saved and is already gone.
Snapshots with saved cursors remain pageable after Docker rotates the source. Logs
removed before or during capture may leave no observable evidence of a gap.
Docker has no stable retained-log sequence identifier or completeness proof.
The server cannot recover those logs or promise to detect all rotation, deletion,
driver truncation, or byte-for-byte identical replacements. A cleanly terminated
Docker response is assumed to represent the retained interval it advertises.

If the first page fails, the capture is discarded. Before building a new report,
the server reclaims its unreferenced snapshot files under the cycle lock. This
includes captures left by a failed report write or a crash. Replay leaves the
pending report and its proposed checkpoint intact. `LOG_SPOOL_TOTAL_MAX_BYTES`
caps snapshot-directory logical file bytes across containers (default 1 GiB,
range 64 KiB–64 GiB). `LOG_SPOOL_MIN_FREE_BYTES` checks
filesystem headroom during capture (default 64 MiB; 0–64 GiB). Manifests, partial
and orphan files count against the snapshot budget. Baseline/outbox JSON and
filesystem allocation overhead are outside that logical-byte cap. Other writers
can consume space after a check; this is not a filesystem reservation. A quota,
headroom or disk-full error blocks advancement without discarding pending data.
Existing page reads and acknowledgements remain allowed; acknowledgement still
needs enough real free space for atomic replacement. Readiness reports snapshot
usage/headroom and pending report bytes, while direct objective tools remain
available. Back up and restore baseline and snapshots together. Never remove a snapshot referenced by pending
progress. A corrupted current baseline fails closed. Older unsupported baselines
with no pending work retain the previous explicit-warning initialization behavior.

## Optional acknowledged delivery

The default `generate_server_report()` preserves immediate persistence for
existing clients. If the response is lost after saving, those clients may miss
that page. Upgrading the server alone does not fix legacy delivery semantics.

For durable replay, use this sequence:

1. Call `generate_server_report(acknowledged_delivery=true)`.
2. Deduplicate by `delivery.id` and durably save the processed data and delivery ID.
3. Call `acknowledge_server_report(delivery_id=delivery.id)`.
4. Repeat until you have successfully acknowledged a report with
   `baseline.complete_logs=true`.

A failed capture can leave `baseline.pending_pages` at zero. When
`baseline.log_errors` is nonzero, surface the failure and follow the same
processing and acknowledgement steps. When you can retry capture, request the
next report with `acknowledged_delivery=true`. Acknowledging a partial delivery
can save progress while keeping the same interval open.

The baseline file holds one outbox entry containing the complete redacted public
report and proposed next baseline. The report is fully rendered before the
atomic write. Its returned `baseline.saved`, `progress_saved` and `committed`
are false; `checkpoint_staged` says whether collection prepared a checkpoint,
and `awaiting_acknowledgement` is true. `complete_logs`/`pending_pages` describe
collection, not acknowledgement. Model failures cannot prevent staging otherwise
successful objective collection. A failed collection may still produce a report,
but acknowledgement cannot advance a checkpoint that was never staged.

All clients share one baseline stream and pending report. A process/thread file
lock covers build, outbox publication, replay and acknowledgement. Compatible
local-filesystem `flock`, atomic replacement and directory/file `fsync` semantics
are required. Network/distributed filesystems are not assumed safe. Concurrent
clients may receive the same ID; clients must coordinate or deduplicate. A legacy
call while an outbox exists returns an error rather than skipping the delivery.
There is no per-client queue or automatic expiry that would discard an unseen
report. Slow or absent acknowledgement blocks subsequent observations through
the report tool; direct objective status tools remain available.

If the process dies before the outbox write, the old checkpoint remains. If it
dies after publication but before response receipt, a later call returns the
same complete report. Restart does not require a model or Docker to replay it.
Acknowledgement commits the proposed checkpoint, removes the outbox and records
the acknowledged ID in one atomic replacement. A lost acknowledgement response
can be retried: the most recently acknowledged ID remains idempotent, even if a
new report is pending. Older IDs beyond that single remembered acknowledgement
are rejected. Unknown IDs cannot advance progress. Failed replacements preserve
the prior file; synchronization failures after a replacement may be ambiguous,
and retrying discovers the durable state.

This is replay-until-acknowledged delivery, allowing duplicates. It is not
exactly-once processing and cannot make a client's external side effects atomic
with the checkpoint. A client that acknowledges before durable processing can
still lose data. Delivery identifiers coordinate the shared stream; they are not
a substitute for MCP endpoint authentication.

## Evaluation workflow

`scripts/cases/analysis_development_cases.json` and
`scripts/cases/analysis_evaluation_cases.json` each contain 18 synthetic cases covering
all three operations. They include objective/advisory disagreement, credentials,
malicious instructions and benign instruction-like text. Related templates mean
the sets are not independent draws. Preserve the reserved set and freeze prompts,
validators and scoring before inspecting its output. Once used for tuning, a set
is regression coverage; create another reserved corpus for an independent claim.
The historical `model_holdout_cases.json` has already informed previous changes.

Use `--suite all-functions --split development|evaluation`, `--repeat 1..20`,
`--case`, `--compare`, `--timeout`, and `--cases-file`. The default remains the
older log-only suite for CLI compatibility. JSON records the corpus SHA-256,
operation, repetition, elapsed wall time, error category and advisory/objective
severity. The all-function path applies the same shared validator as production
but intentionally bypasses the circuit so each repetition is attempted. Unit
transport fixtures additionally cover incomplete/invalid responses, missing or
invented evidence, connection failures and timeouts for all three operations.

`forbidden_output` contains known synthetic secret terms, `instruction_marker`
flags quotation versus appearance outside evidence, and `forbidden_advice`
checks specific unsafe recommendations. An evidence-only marker requires review
and is not proof of obedience or an automatic pass. A marker outside evidence
can also be descriptive, so it requires review rather than a claim about intent.
Explicit prohibited advice fails. These bounded checks cannot detect every
unsafe answer or prove causality. Review model advice before acting.

Custom cases follow the same JSON shape, including `logs`, `allowed_severities`
and `require_evidence`; event cases add `activity`, diagnostic cases add a full
`snapshot`. Only authorized, anonymized real cases belong in a custom corpus.
There is no production corpus in this repository and none was collected for the
new evaluation. Do not infer production provenance from generated process errors
or file names. Evaluation output is redacted on a best-effort basis and known
forbidden synthetic terms are masked before printing; inspect any real corpus
and output before storing or publishing it.

## Initial verification on 2026-10-29

The initial implementation suite passed 127 unit and contract tests under Python 3.14.4.
Both production and integration Compose configurations validated. The final
Python 3.14 image built and its installed dependencies passed `pip check`.
The live temporary `mcp-shadow-test` stack verified MCP requests, readiness,
Docker proxy read restrictions, offline advisory fallback, legacy pages and
acknowledged pages. Restarting its server with an unacknowledged report replayed
the same report; acknowledging subsequent pages delivered all 40 generated test
events without missing or duplicate IDs in the processed fixture sequence.
Stopping the temporary proxy produced HTTP 503 with Docker marked unavailable.
The integration discovery function was restricted to the test Compose project,
and the temporary containers, network and state volume were removed afterwards.
No production stack restart or configuration change was performed.

Deterministic tests additionally cover 1000 identical multiline occurrences with
the same timestamp, direct seek without prefix rereads, corruption in an accessed
snapshot block, missing snapshots, container replacement, incomplete streams,
size/disk failures, concurrent clients, failed outbox/acknowledgement replacement,
corrupt baselines and provider recovery under concurrency. Docker rotation after
a completed capture is simulated in the unit fixture. Actual driver rotation
while capturing, host power loss and distributed filesystem behavior were not
induced in the live stack. These tests do not establish exactly-once delivery or
complete detection of Docker log loss.

The subsequent reliability work adds fresh diagnostic observations, operation
circuits, evidence references, aggregate snapshot budgets, human review tooling,
cross-process tests and real isolated ENOSPC/rotation tests. See the
[follow-up verification record](evaluation/reliability-followup-2026-10-29.md) for current
results and remaining limits.
