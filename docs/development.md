# Development and model evaluation

`server.py` wires the MCP interface to the application services. The `shadow`
package separates configuration, readiness checks, container discovery and
operations, Docker access, diagnostics, observation collection, log profiles,
baseline persistence, event processing, metrics, report rendering, and Ollama
requests. `formatting.py` contains formatting and Docker log helpers. The modules
are included in the container image.

Model-assisted analysis is routed through `AnalysisProvider`, the shared
`ValidatedProvider` boundary, and a process-local circuit breaker. Ollama remains
the default. `ANALYSIS_PROVIDER=disabled` turns off optional analysis;
`ANALYSIS_PROVIDER=package.module:factory` loads an explicitly installed adapter
without changes to the server or diagnostic flow. The trusted factory receives
`settings` and `http` keyword arguments and returns an `AnalysisProvider`.
It may implement `availability()` for a provider-specific readiness status.
No second model service or external credentials are included. The independent
adapter in the tests is a contract fixture, not a complete model integration.

`ANALYSIS_FAILURE_THRESHOLD` (1–20, default 2) consecutive failures open a
circuit for `ANALYSIS_COOLDOWN_SECONDS` (1–600, default 30). Timeouts and
unavailability affect a shared transport circuit. Malformed or unsupported
responses affect only their operation: invalid diagnostics do not block working
event interpretation. Calls in an open scope fail immediately. After the cooldown,
one request probes recovery. Only one analysis request is in flight per process,
across all operations; other callers receive a busy error. A response can establish
transport recovery while still failing the operation's validation. A cycle without
relevant events skips analysis and never establishes transport recovery.

`GET /ready` exposes transport and operation states, aggregate state `degraded`
when an operation is blocked, and counters for admitted calls, errors, rejections,
skips, recovery and elapsed analysis time. Counters contain no logs or answers and
reset with the process. Multiple processes have independent breakers. Readiness
probes do not close a circuit. Objective observations remain available. Adapters
must enforce bounded network timeouts themselves; Ollama retains
`OLLAMA_TIMEOUT_SECONDS` for transport timeouts.

Every diagnostic collects fresh objective state, metrics and logs. Only advisory
model analysis may be reused within `DIAGNOSTIC_CACHE_TTL_SECONDS`, after matching
all snapshot fields (including container identity and serialized fact order) and
provider identity. `cache.scope=model_analysis` and
`cache.objective_data_refreshed=true` make this distinction explicit. Changing an
observation invalidates the advisory cache, including when the provider is down.
Failed model responses are not cached.

To verify an implementation, subclass `ProviderContract` in
`tests/provider_contract.py` and provide working and unavailable scenarios. Always
run alternative adapters through `ValidatedProvider`; its copies protect the
objective snapshot from mutation. Tests also exercise independently implemented
adapters with invalid results, source mismatches and provider failures.

Log analysis includes observed signal counts and a bounded, redacted input.
The shared validator rejects invalid result shapes, unsupported evidence, severity
without evidence, and a limited set of obvious contradictions. Event evidence must
match its container; diagnostic evidence must match its declared state, metrics
or logs source. A single invalid finding rejects the analysis rather than being
silently discarded. Confidence must be finite and within 0–1. Rejected analysis is reported as an
error; diagnostic severity remains determined by objective observations. These
checks cannot prove that every summary or recommendation is correct, or prevent
every prompt injection. The system instruction and JSON encoding of log data
help separate instructions from untrusted content but are not security boundaries.

Successful common validation adds `evidence_context_sha256`,
`evidence_references` and `grounding`. References are calculated by the
application from matching input ranges, event positions or objective fields;
adapter-supplied references are overwritten. Repeated matches are explicitly
ambiguous and capped at 16 candidates per excerpt. Observation IDs identify
content within an input context; they are not globally unique event or delivery
IDs. A matched citation never certifies a cause or recommendation.

`OLLAMA_THINK` (`true` or `false`, default `false`) and
`OLLAMA_TEMPERATURE` (0–2, default 0) configure all Ollama requests. Thinking
must be supported by the chosen model, and can increase latency or exhaust
the generation limit. Compare settings with representative cases before
changing the deployment.

To run a small synthetic model regression set against a local Ollama server:

```sh
python -m scripts.evaluate_model --model YOUR_MODEL --base-url http://localhost:11434
```

The twelve synthetic cases check response format, expected severity ranges,
literal evidence, a redacted credential, and one prompt-injection marker. Use
`--case CASE_ID` to run a subset. Per-case duration and the model's structured
response are included in the result so failures can be inspected.
Use `--compare OTHER_MODEL` to run the same cases against additional models, or
`--cases-file PATH` for a curated JSON corpus following `scripts/cases/model_cases.json`.
To add actual errors generated by isolated local Python processes to the
synthetic cases, run:

```sh
python -m scripts.generate_failure_cases > /tmp/mcp-shadow-cases.json
python -m scripts.evaluate_model --model YOUR_MODEL --compare OTHER_MODEL \
  --base-url http://localhost:11434 --cases-file /tmp/mcp-shadow-cases.json
```

The generated errors are controlled test failures, not production incidents.
Anonymize real logs before putting them in a corpus or saving evaluation output.
This is a basic regression check, not proof of model quality or resistance to
prompt injection. Repeating the injection marker is a signal for manual review;
it can also be a harmless quotation. Results vary by model and generation, so
investigate failures before changing the active model.
Use `--repeat 3` to measure variability across repeated requests. Try
`--think` and `--temperature 0.1` on the same cases to compare settings;
the JSON output records those settings and elapsed time. The separate
`scripts/cases/model_holdout_cases.json` contains historical synthetic evaluation cases.
Because earlier investigations already used them, treat them as regression
coverage rather than an independent held-out test. They are not a substitute for anonymized
production incidents or independent review.
See the [local model evaluation](evaluation/model-evaluation-2026-10-29.md) for one
recorded comparison and the [evaluation after the analysis changes](evaluation/model-evaluation-improved-2026-10-29.md)
for a smaller follow-up and its limitations.

The all-function evaluation suite covers logs, relevant events and complete
diagnostics, including advisory disagreement with objective severity:

```sh
python -m scripts.evaluate_model --suite all-functions --split development \
  --model YOUR_MODEL --repeat 3
# Freeze prompts, validators and scoring before examining reserved cases:
python -m scripts.evaluate_model --suite all-functions --split evaluation \
  --model YOUR_MODEL --repeat 3
```

Both new sets contain 18 synthetic cases. They are separate files with distinct
inputs but use related templates; they are not independent production samples.
Do not tune prompts on the evaluation split and then describe it as independent.
The older holdout has already informed previous changes and is historical
regression coverage. Custom `--cases-file` inputs support anonymized real cases;
no suitable production corpus is included. JSON results include the corpus hash,
per-operation counts, repetitions, timings, validation failures and manual-review
signals. Quoting an injected marker as evidence is distinguished from mentioning
it elsewhere and from recommending a prohibited action. Ambiguous quotations
require review and are not counted as passes. These checks do not prove
instruction obedience, causal accuracy or safety. The evaluator bypasses the
production circuit breaker so each requested repetition is actually attempted.
See the [all-function evaluation record](evaluation/model-evaluation-all-functions-2026-10-29.md)
and [workflow details](reliability-and-evaluation.md).

The newer quality workflow adds twelve development cases with review rubrics
in `scripts/cases/quality_development_cases.json` and twelve separate, unexecuted
reserved cases in `scripts/cases/quality_evaluation_cases.json`. The reserved manifest
records the frozen corpus hash and its authoring-time status; executions must be
recorded by their operator. Historical evaluated sets remain regression coverage.

Use the development corpus to prepare a blinded human review:

```sh
umask 077
python -m scripts.evaluate_model --suite all-functions --split development \
  --cases-file scripts/cases/quality_development_cases.json --model YOUR_MODEL \
  --repeat 2 > /tmp/development-results.json
python -m scripts.review_evaluation --results-file /tmp/development-results.json \
  --cases-file scripts/cases/quality_development_cases.json --prepare /tmp/review.json
```

Once a human has filled the ratings and reviewer field in `/tmp/review.json`,
validate and summarize the packet.

```sh
python -m scripts.review_evaluation --results-file /tmp/development-results.json \
  --cases-file scripts/cases/quality_development_cases.json --summarize /tmp/review.json
```

The packet hides explicit model labels, timings and automated scores. Hashes bind
it to the evaluated inputs and retained responses; edited artifacts, duplicates,
incomplete ratings and judgments on unretained responses are rejected. Human
identity is self-reported. The workflow never fills human judgments itself;
unsafe, unsupported, overstated, unhelpful or materially incomplete advice cannot
receive a quality pass. Automated evaluation reports `pending_human_review`
until a separately completed review is provided. See the
[follow-up implementation and measurements](evaluation/reliability-followup-2026-10-29.md).

The container and CI use Python 3.14. Install the dependencies and run the tests with:

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-hashed.txt
python -m unittest discover -s tests -v
docker compose --env-file .env.example -p mcp-shadow-config config --quiet
```

The contract tests inspect source and configuration; they do not replace a live Docker and MCP integration test.
The container and CI install the hash-verified dependency closure in
`requirements-hashed.txt`. See [build maintenance](builds-and-compatibility.md)
for updating the version constraints, hashes and base-image digest together.
Run `bash scripts/run_integration.sh` for the isolated Docker checks, including
the official MCP SDK client demo. See [human review](evaluation/human-review.md)
to complete the existing pending review without running a model.
