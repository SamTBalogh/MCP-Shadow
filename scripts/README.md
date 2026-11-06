# MCP Shadow scripts

Run these commands from the repository root.

- `bash scripts/run_integration.sh --demo` runs the [isolated demo](../docs/demo.md).
  Omit `--demo` for the full Docker integration checks.
- `bash scripts/run_integration.sh --resilience` checks proxy recovery, service
  disappearance/replacement, restart replay and explicit interval closure with
  preserved captures. It uses disabled analysis and an isolated temporary stack.
- `python scripts/mcp_demo.py --help` shows how to query a running MCP endpoint
  with the official SDK.
- `python -m scripts.evaluate_model --help` shows how to evaluate a local model
  with the synthetic inputs in [cases](cases/). The
  [development guide](../docs/development.md) explains which cases are reserved.
- `python -m scripts.generate_failure_cases` adds controlled Python failures to
  the default corpus and prints the combined cases.
- `python -m scripts.review_evaluation --help` prepares a review packet or
  summarizes completed ratings. The [review guide](../docs/evaluation/human-review.md)
  shows how to use the recorded results.
