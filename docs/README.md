# MCP Shadow documentation

## Run and maintain

- [Demo](demo.md) walks through the official MCP client and the synthetic fixture.
- [Development](development.md) covers tests, providers and evaluation commands.
- [Builds and compatibility](builds-and-compatibility.md) records tested platforms
  and explains how to update pinned dependencies.
- [Report reliability](reliability-and-evaluation.md) describes report delivery,
  snapshot storage and the evaluation contract.
- [Design decisions](design-decisions.md) explains the tradeoffs behind those features.

## Inspect the evidence

- [Evaluation history](evaluation/) holds dated reports and their recorded results.
  Start with the [three-operation regression](evaluation/model-evaluation-all-functions-2026-10-29.md)
  and the [development follow-up](evaluation/reliability-followup-2026-10-29.md).
- [Human review](evaluation/human-review.md) explains how to review the retained
  synthetic responses without running a model.
- [Publication review](publication.md) explains the first-commit selection in
  [the manifest](publication-files.txt).
