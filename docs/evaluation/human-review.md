# Complete the pending human quality review

No model or production logs are needed to review the already recorded synthetic
outputs. This is a review procedure, not evidence that a person has performed it.
The publication preparation reproduced the stored packet exactly and confirmed
that all ratings and the reviewer field remain blank.

From a Python 3.14 environment installed as described in the README:

```sh
umask 077
mkdir -p evaluation-output
.venv/bin/python -m scripts.review_evaluation \
  --results-file docs/evaluation/quality-development-results-2026-10-29.json \
  --cases-file scripts/cases/quality_development_cases.json \
  --prepare evaluation-output/review.json
```

Preparation uses exclusive creation: choose a new filename if it already exists.
The packet contains 12 attempts, 10 retained responses and two rejections whose
raw responses were not retained. Ask a human to read each retained response next
to its synthetic input and rubric. Fill the self-reported `reviewer` field and
all five `ratings` dimensions using the packet's `rating_choices`; use `uncertain`
when evidence cannot support a judgment. Add explanatory `notes`, especially for
unsafe advice, unsupported facts, overstated causes or material omissions. Leave
the two unreviewable samples' ratings null. Do not edit inputs, responses, IDs,
rubrics or hashes. Literal quotation does not establish a cause or justify advice.

If the author performs the review, describe it as author review. Only describe a
review as independent if a different person actually did it. Model labels,
timings and automatic scores are hidden administratively, but model identity can
still leak through free text. Reviewer identity is self-reported, not verified.

Validate and summarize the completed packet:

```sh
.venv/bin/python -m scripts.review_evaluation \
  --results-file docs/evaluation/quality-development-results-2026-10-29.json \
  --cases-file scripts/cases/quality_development_cases.json \
  --summarize evaluation-output/review.json > evaluation-output/summary.json
```

An incomplete packet must fail summarization. Report pass/fail/uncertain and
unreviewable counts with the denominator and exact artifact hashes. Retain
negative judgments; do not infer a production quality rate. Notes are omitted
from the aggregate, but review files may contain private reviewer data. The
ignored `evaluation-output/` directory is for local work; manually inspect and
obtain permission before proposing any new public artifact.

Development uses `quality_development_cases.json`. Historical `model_cases`,
`model_holdout`, and the investigated portions of `analysis_*` are regression
coverage, even when their original filenames say evaluation/holdout. The separate
`quality_evaluation_cases.json` remains reserved and unexecuted in this work.
Its manifest is a declaration, not automatic usage tracking. Freeze and record
prompts, validation/scoring code and generation settings before any future run;
record execution and retire the set from reserved status if used to tune changes.
Do not read reserved outputs to improve prompts and then call the test unseen.
Privacy inspection or corpus-hash verification is not a model-quality execution.

For new authorized synthetic measurements, see [the evaluation commands](../development.md).
No real logs or incidents were collected during publication preparation. A real
incident corpus requires separate specific authorization and privacy review.
