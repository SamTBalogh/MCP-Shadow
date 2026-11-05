# Model evaluation after analysis changes — 2026-10-29

Publication privacy note: `local-qwen3.5-variant:latest` is a replacement label
for a custom local model alias, not a downloadable model identifier. Measurements
are unchanged. Reproduction requires an equivalent locally supplied variant; its
weights, customization and exact runtime were not archived. Public model names
below are the recorded names, not a recommendation.

The comparison below evaluates the complete application path, including prompt
changes, objective log signals, generation settings, response validation and
safe normalization of a single pair of wrapping quotation marks. It does not
measure changes to the models' weights.

An initial run used all 25 development and held-out cases. Both Qwen 3.5
variants had every response rejected because they wrapped literal evidence in
quotes that were not part of the source. After investigating the returned
responses, the validator was changed to remove a single pair of wrapping quotes
only when the enclosed text is an exact source excerpt. Fabricated evidence is
still rejected. The following results use that corrected validator.

All three installed models were rerun on the same ten-case subset: six from
the original corpus and four held-out cases. It includes normal operation,
explicit failures, synthetic credentials and two prompt-injection markers.
Each case was run once at temperature 0 with thinking disabled.

| Model | Passing cases | Usable structured responses | Mean time per case |
| --- | ---: | ---: | ---: |
| `local-qwen3.5-variant:latest` | 7/10 | 10/10 | 16.82 s |
| `qwen3.5:2b-q4_K_M` | 7/10 | 10/10 | 16.93 s |
| `qwen3:1.7b-q4_K_M` | 2/10 | 10/10 | 13.76 s |

The Qwen 3.5 models detected the explicit database error, timeout and
out-of-memory cases that they missed in the previous run. One rejected a held-out
credential case because of unsupported evidence; the other assigned it
`no_indication`. The prompt-injection cases did not pass the conservative
marker checks. In the held-out injection case, all three quoted the marker in
literal evidence; this may be quotation rather than obedience to the embedded
instruction. In the original injection case, Qwen 3 repeated the marker in the
summary. Marker checks are warning signals, not proof of intent or safety.

For the six original cases present in both evaluations, the previous and
corrected pipelines passed 1/6 and 5/6 for `local-qwen3.5-variant:latest`, 0/6 and
5/6 for `qwen3.5:2b-q4_K_M`, and 1/6 and 1/6 for `qwen3:1.7b-q4_K_M`.
Each is a single run, and prompts, sampling settings and validation all
changed; this is not an isolated measurement of model capability.

These results cover a small synthetic subset and are not directly comparable
with the earlier 15-case scores. They do not include production incidents.
Timings include model loading and other local activity, including Docker
integration checks; they are not a controlled benchmark. The validator catches
some unsupported evidence and contradictions but does not prove summaries,
causes or recommendations correct, and does not guarantee resistance to
prompt injection. Continue to use objective severity independently of the
model and review advice before acting.

## Thinking comparison

For `local-qwen3.5-variant:latest`, the explicit database error and the held-out
injection were each run twice with a 60-second request timeout and temperature
0. With thinking disabled, both error cases passed and both injection cases
were rejected for unsupported evidence; mean time was 16.51 seconds. With
thinking enabled, none of the four attempts returned within 60 seconds. These
observations do not establish whether the model would eventually answer with a
longer timeout. The application continues to default to thinking disabled.

To run the full separate held-out corpus, use:

```sh
python -m scripts.evaluate_model --base-url http://localhost:11434 \
  --model local-qwen3.5-variant:latest --compare qwen3.5:2b-q4_K_M \
  --compare qwen3:1.7b-q4_K_M --cases-file scripts/cases/model_holdout_cases.json
```
