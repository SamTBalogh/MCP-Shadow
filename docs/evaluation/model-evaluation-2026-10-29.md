# Local model evaluation — 2026-10-29

Publication privacy note: `local-qwen3.5-variant:latest` is a replacement label
for a custom local model alias, not a downloadable model identifier. Measurements
are unchanged. Reproduction requires an equivalent locally supplied variant; its
weights, customization and exact runtime were not archived. Public model names
below are the recorded names, not a recommendation.

This is one exploratory run against the three models installed in the local
Ollama instance. Each model received the same 15 cases: 12 synthetic cases
and three exception messages produced by intentionally failing Python
processes. The set includes normal operation, errors, a synthetic credential,
a prompt-injection instruction and empty input. It does not include production
incidents. Malformed and incomplete provider responses are tested separately
with a simulated HTTP transport in the automated test suite.

| Model | Passing cases | Usable structured responses | Mean time per case | Maximum time |
| --- | ---: | ---: | ---: | ---: |
| `local-qwen3.5-variant:latest` | 5/15 | 15/15 | 16.71 s | 39.24 s |
| `qwen3.5:2b-q4_K_M` | 3/15 | 15/15 | 12.40 s | 15.10 s |
| `qwen3:1.7b-q4_K_M` | 6/15 | 15/15 | 9.75 s | 24.80 s |

All three assigned `no_indication` to the explicit database error, timeout,
out-of-memory message, Python traceback and the authentication error. All
three placed the injection marker in the summary. Some responses also included
non-literal evidence. Passing the structured-output check does not establish
that a diagnosis is correct or safe. The evaluation is heuristic; quoting an
injection marker can have benign explanations and requires manual inspection.
These results do not establish that any of these models is suitable for
unattended decisions.

Timing includes request and inference time, model loading and concurrent system
activity. The first model also ran while Docker integration tests were active;
the timing figures are not a controlled performance comparison. Model output
can vary between runs. To reproduce the test with current code:

```sh
python -m scripts.generate_failure_cases > /tmp/mcp-shadow-cases.json
python -m scripts.evaluate_model --base-url http://localhost:11434 \
  --model local-qwen3.5-variant:latest --compare qwen3.5:2b-q4_K_M \
  --compare qwen3:1.7b-q4_K_M --cases-file /tmp/mcp-shadow-cases.json
```

Before selecting a model, repeat the evaluation with anonymized, representative
incidents and review outputs manually. Objective diagnostic severity is
calculated independently of model advice.
