# First experimental publication review

Status: prepared for a local review and an experimental portfolio publication on
the checked Linux setup, subject to the limits below. No Git repository, commits,
PRs, remote uploads or publication were created. The visible `.git` directory is
empty environment scaffolding, not history. MIT is retained unchanged.

## Explicit first-commit selection

[publication-files.txt](publication-files.txt) is the exhaustive proposed list of
repository-relative file paths, one per line. Use that reviewed list instead of
adding the whole workspace blindly. It includes:

- Runtime: `server.py`, `formatting.py`, every listed `shadow/*.py` module.
- Deployment: `Dockerfile`, `compose.yaml`, the three requirements files,
  `.env.example`, both ignore files, both listed config files and
  `disk-probe/.gitkeep`.
- Documentation: `README.md`, `LICENSE`, all listed English Markdown records,
  and the four curated historical synthetic evaluation/review JSON artifacts.
- Reproduction: all listed scripts/corpora, tests/fixtures and the CI workflow.

The detailed manifest is the selection; these categories are only an explanation.
Reserved synthetic inputs are included for reproducibility, not claimed to be
secret test cases. Do not tune on their model outputs and still call them unseen.
After any further edit, review the manifest and repeat privacy checks. No history
exists to scan yet. Review any future staged diff and history before pushing.

## Privacy findings and exclusions

A filename/location-only pattern scan covered source, examples, tests and public
evaluation artifacts for common credential assignments, key formats, private-key
headers, emails, personal paths and private/Tailscale IPs. Suspicious locations
were inspected without printing credential values. This is a bounded inspection,
not a guarantee that every secret or identifying string has been detected.

- `.env` exists and contains local deployment configuration, including a private
  path. It is preserved untouched, not copied into a clean publication rehearsal,
  and excluded by both the proposed selection and build context.
- `.agents/`, `.codex/`, `.aws/`, caches, virtual environments, state, logs,
  backups, local reports and `evaluation-output/` are excluded. Only `.gitkeep`
  inside `disk-probe/` is proposed. No production logs were collected or read.
- A private deployment path/address in a negative test was replaced with generic
  pattern-based checks. A custom model alias in two historical Markdown records
  was replaced with an explicit publication label. Its measurements and failures
  remain, and the missing model customization is disclosed.
- Credential-shaped strings in test/development corpora are deliberately
  synthetic. Public evaluation responses contain redaction placeholders rather
  than recovered credentials. Historical JSON artifacts and their bound pending
  review packet are unchanged; all human ratings remain blank. These artifacts
  are curated exceptions, not a precedent for committing arbitrary run output.
- Future reports, snapshots, review identities and notes may be sensitive. Store
  them in ignored local directories and review/authorize any public inclusion.
  `.gitignore` is a convenience, not a data-loss-prevention boundary.

`.dockerignore` uses an allowlist for runtime build inputs. `.gitignore` excludes
local environment/secrets, local agent configuration, runtime output and caches.
The explicit manifest prevents unknown local files from silently joining this
first-publication proposal. Actual Git index behavior has not been exercised,
because Git initialization was explicitly out of scope.

## Verification record

See [builds and compatibility](builds-and-compatibility.md) for exact versions and
[design decisions](design-decisions.md) for the review of previous recommendations.
The publication checks are recorded below; historical model results remain in
their original documents and were not rerun or upgraded into new quality claims.

- A fresh Python 3.14.4 virtual environment installed all 37 locked packages
  using required hashes and wheels. The normalized version closure matches the
  existing constraints exactly; `pip check` passed on the host and in both images.
- All 154 unit/contract tests passed (6.326 seconds). The server import and Bash/
  Python syntax checks passed. Relative Markdown file links resolve.
- Both Compose configurations validated using `.env.example`, never `.env`.
  The pinned runtime image built with the hashed install.
- `bash scripts/run_integration.sh` passed using a unique project. Allowed MCP
  discovery/inspect/stats/log/report calls worked; five container tools rejected
  the unlabelled fixture, and representative image/archive/events/exec/delete/stop
  proxy routes were denied. Direct proxy inspection of that same unlabelled
  synthetic fixture succeeded, confirming the documented label boundary.
- The official SDK client discovered 11 tools, observed all 40 distinct generated
  error messages, checked pending-report replay and acknowledged all pages with
  model advice absent. Objective severity was `medium` in this run. A first demo
  attempt used an incorrect response field name; after correction the full
  integration script was rerun and passed.
- Restart replay/idempotent acknowledgement, actual ENOSPC in a dedicated 1 MiB
  tmpfs, retained snapshot reads after actual Docker rotation and readiness HTTP
  503 after stopping only the temporary proxy passed. Rotation during capture
  completed but cannot establish historical source completeness.
- A clean copy containing only the 94 manifest files built and started the main
  Compose stack. Test-only overrides used its own networks, an empty named state
  volume, an ephemeral localhost port, disabled analysis and project-filtered
  discovery. The official client queried the `mcp-shadow` service successfully.
  This checks the main topology but does not reproduce the manual bind-directory
  ownership command, external-network provisioning or an arbitrary user's host.
- The stored human packet regenerated exactly from its bound results/corpus;
  all 12 samples still have null ratings, and 10 have retained responses.
- Temporary containers, networks and state volumes were removed. No production
  stack was modified, restarted or used as a log source. Build images/cache can
  remain locally; they contain the allowlisted runtime sources and dependencies.
- The manifest contains only existing unique paths and excludes private `.env`,
  local configuration and generated state. MIT and the four historical JSON
  artifacts remain byte-for-byte unchanged. No Git metadata/history was created.

## Remaining limits and release work

- Human review is prepared and reproducible, not completed. Follow
  [the review procedure](evaluation/human-review.md); no independent reviewer is claimed.
- No new live-model evaluation or real incident corpus was used. Existing negative
  results remain public. Real incident collection needs separate authorization.
- No external review, comprehensive security audit, vulnerability scan or GitHub
  Actions run occurred. Representative allowed/denied calls are regression checks.
- Other operating systems, CPU architectures, clients, Docker log drivers,
  rootless setups, power failure and distributed filesystems remain unverified.
- MCP authentication is absent. Docker labels do not restrict direct proxy reads
  by container. Best-effort redaction and evidence matching do not prove privacy,
  prompt-injection resistance, causal accuracy or recommendation safety.
- Pinning reduces mutable inputs but needs deliberate maintenance and is not a
  guarantee of identical builds or safe dependencies.

A public GitHub repository can be created only after the owner explicitly asks.
Choose a repository name, review the exact selected files and any later diff,
and keep the experimental status and limitations visible when publishing.

## Portfolio wording

GitHub description:

> Experimental read-only MCP server for Docker homelab observability, with
> recoverable reports and optional local model-assisted log analysis.

Interview/CV description:

> Built an experimental Python MCP tool server for labelled Docker services,
> separating objective observations from fallible local-model advice. Implemented
> bounded log snapshots, replay with explicit acknowledgement, and reproducible
> tests using an official MCP client and isolated Docker fixtures. Documented
> security boundaries, synthetic evaluation failures and pending human review.

Discuss the tradeoffs: one shared report stream, client deduplication, initial
snapshot I/O, Docker retention limits and the cost of retaining provider/circuit
logic for optional inference. Describe the actual personal contribution honestly;
these lines do not imply autonomous agents, production reliability or independent
validation.
