# Design review for the experimental publication

The existing evaluation and reliability records were reviewed before changing
runtime design: [initial comparison](evaluation/model-evaluation-2026-10-29.md),
[analysis follow-up](evaluation/model-evaluation-improved-2026-10-29.md),
[three-operation results](evaluation/model-evaluation-all-functions-2026-10-29.md), and
[reliability follow-up](evaluation/reliability-followup-2026-10-29.md). They justify a useful
observation tool with fallible advice, not unattended diagnosis. Tests provide
bounded regression evidence; none substitutes for a security audit.

| Mechanism or recommendation | Decision and evidence | Cost or outstanding work |
| --- | --- | --- |
| Objective/advisory separation and common validation | Keep. Recorded incorrect severities, nonliteral evidence and unsupported causal inferences still apply. Objective severity must survive failed analysis. | Text matches cannot certify causes or safe advice. Human ratings remain pending. |
| Provider interface and explicit factory | Keep the existing small boundary. One real Ollama adapter plus disabled mode and independent contract fixtures exercise fallback and validation. | Another production provider has not been demonstrated. Do not add a registry or additional adapters without a use case. Factories are trusted installed Python code. |
| Shared transport and per-operation circuits | Keep. Timeout/unavailable fixtures and malformed diagnostics show different failure scopes; malformed advice should not suppress working event analysis. Single-flight bounds local inference concurrency. | Process-local, not distributed protection. Busy requests fail; callers must handle that. No automatic retry queue. |
| Fresh observations and advisory cache | Keep. Tests cover changes during TTL, container/profile changes and input serialization. Objective observations are refreshed before advice reuse. | Non-atomic Docker sampling and fingerprint/cache complexity; no cache-based freshness promise. |
| Immutable snapshots and quotas | Keep. Repeated multiline occurrences, direct seeking, corruption and real isolated rotation tests justify persistence over refetching prefixes. | Full-interval capture adds initial I/O, latency and sensitive storage. Cannot prove retained Docker logs are complete. |
| Optional acknowledged delivery and locks | Keep optional. Restart, response replay, concurrent processes and full-disk fixtures demonstrate why immediate-save alone is insufficient for a report consumer. | One shared stream; client deduplication and durable processing required. No exactly-once external effects. |
| Human review and reserved corpus discipline | Still required. Existing packet has no judgments; old holdout/evaluated subsets informed changes and are regression coverage. | Complete the supplied review honestly. No real incident collection without separate authorization. |
| Pin images and verify package bytes | Implemented without changing the resolved package versions. Pinned Python digest and hashed wheel install now apply to build/CI. | Deliberate update burden and platform limitations; no identical-build or vulnerability-free claim. |
| Compose names | Removed fixed `container_name` and dated shared application image tag. DNS uses service names. Test project identity is injected instead of hardcoded. | Published ports and aliases on a deliberately shared external network still need coordination. |
| MCP authentication and Docker label boundary | Document visibly; preserve localhost publication and private proxy network. | Authentication is absent. Labels do not constrain a direct proxy client's host-wide permitted reads. Public/untrusted exposure requires a separate authenticated deployment design. |

No runtime framework rewrite or additional provider, background worker, database,
retry queue or agent loop was needed. The remaining complexity belongs to the
optional differential-report and analysis paths. Users needing only discovery,
status or bounded logs can use those direct tools without consuming report state
or loading a model.
