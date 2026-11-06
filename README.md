# MCP Shadow

**Experimental project for a Docker homelab. Not production-ready.**

MCP Shadow is a read-only [Model Context Protocol](https://modelcontextprotocol.io/)
server. It gives an MCP client or agent tools to discover labelled Docker services,
query their status and resource usage, read bounded logs, and request differential
reports. Optional local Ollama analysis adds advice alongside objective observations.
It does not implement an autonomous agent or establish reliable incident causes.

**Trust boundary:** the included MCP HTTP endpoint has **no authentication**.
The `Host` and `Origin` checks protect against DNS rebinding. Compose publishes
the port on localhost by default. Labels filter access in the application; they
do **not** restrict direct Docker proxy access to individual containers. Logs,
raw snapshots, baselines and reports can contain sensitive data, even after redaction.
Use a trusted host/network and an authenticated gateway for remote access.

## First query with synthetic data

Requirements: Linux Docker Engine with access to its local Unix socket, Docker
Compose v2, a shell, and network access to fetch the pinned images and packages.
You do not need Python on the host, an MCP desktop app, Ollama, or a model.
Run from a clean copy of this directory:

```sh
bash scripts/run_integration.sh --demo
```

The script uses `.env.example` with `ANALYSIS_PROVIDER=disabled`. It starts a
labelled log fixture, the server and its Docker proxy in a temporary project with
its own internal network and state volume. Discovery is filtered to that project.
The **official MCP Python SDK** client connects over Streamable HTTP, queries the
fixture's state and memory, and observes 40 distinct generated error events across
report pages. It verifies pending report replay and acknowledges the pages. The
script removes the temporary containers, network and volume on exit.

The client prints a JSON summary like this. `memory_percent` depends on the host
and container, and `objective_severity` depends on the signals observed during
the query. A successful run observes 40 events and returns a null model advisory.

```json
{
  "client": "official MCP Python SDK",
  "tools": 12,
  "service": "log-fixture",
  "state": "running",
  "running": true,
  "memory_percent": 0.01,
  "synthetic": true,
  "observed_error_events": 40,
  "objective_severity": "medium",
  "model_severity_advisory": null,
  "delivery_replay_verified": true,
  "checkpoint_acknowledged": true
}
```

See the [demo walkthrough](docs/demo.md) for the MCP calls and the separate full
integration check, which covers restart, disk full, log rotation, proxy outage
and graceful degradation when Ollama is configured and unavailable.

## Why this exists

A homelab operator can ask an MCP client for observations across selected services
without giving that client Docker command execution tools. The project explores
MCP tool contracts, separation of observations from model advice, bounded log
processing and recoverable report delivery. It demonstrates software engineering
around tools an agent can consume; no scheduler, planning loop or autonomous
remediation is included.

## Install for your own homelab

1. Copy `.env.example` to `.env`. For observation without a model, set
   `ANALYSIS_PROVIDER=disabled`. Review the paths and keep the port on localhost.
2. Prepare the default network and bind directories on Linux:

   ```sh
   docker network create homelab-monitoring
   mkdir -p state disk-probe
   sudo chown 10001:10001 state
   ```

   Reuse an existing network if appropriate. The state directory must be writable
   by container UID 10001. Put `disk-probe` on the filesystem to measure; it is
   mounted read-only. Adjust commands if you changed the example paths/network.
3. Start a named project and check readiness:

   ```sh
   docker compose -p shadow-lab up -d --build
   curl --fail http://127.0.0.1:8010/ready
   ```

4. Use an MCP client with Streamable HTTP at `http://127.0.0.1:8010/mcp`. For a
   reproducible query using the supplied client on a Python 3.14 host:

   ```sh
   python3.14 -m venv .venv
   .venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-hashed.txt
   .venv/bin/python scripts/mcp_demo.py --url http://127.0.0.1:8010/mcp \
     --project shadow-lab --service mcp-shadow
   ```

   This lists MCP tools and queries the server container's objective state and
   memory usage. A container client on the shared network can use
   `http://mcp-shadow:8000/mcp`; use unique service aliases/networks if multiple
   stacks share that network. Removing fixed container names avoids host-global
   name conflicts, but shared-network DNS aliases still require care.

Streamable HTTP at `/mcp` accepts `localhost`, `127.0.0.1` and `[::1]` as
`Host` values without a port, with `:8000`, or with the published `MCP_PORT`
(`8010` by default). It also accepts `mcp-shadow:8000` for Compose clients.
The default `Origin` values are those same authorities prefixed by `http://`.
Native clients, including the official Python SDK client, can omit `Origin`.

For a custom DNS name, reverse proxy or Compose alias, add exact authorities to
`MCP_ALLOWED_HOSTS` (e.g. `shadow-alias:8000,shadow.example`) and, if the
client sends `Origin`, add exact origins to `MCP_ALLOWED_ORIGINS` (e.g.
`https://console.example`). Both variables add to the defaults. Include the port
when the client sends it. Origin values require a scheme and authority only.
Wildcards are unsupported. `MCP_BIND_ADDRESS` controls port publication and does
not add a trusted name. See the [Streamable HTTP security requirements](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#security-warning).

To observe another service, add labels to its Compose definition and recreate
that service yourself:

```yaml
labels:
  io.shadowofintent.mcp.enabled: "true"
  io.shadowofintent.mcp.name: "Example web service"
  io.shadowofintent.mcp.description: "Synthetic example"
  io.shadowofintent.mcp.log-profile: "nginx"
  io.shadowofintent.mcp.expected-stopped: "false"
```

Only `enabled` is required. `list_managed_services` returns the actual container
names for subsequent calls. Review ignore patterns in `config/log-profiles.json`;
filtering noise can hide relevant lines. Ollama is optional: configure an existing
trusted endpoint and installed model, then enable `ANALYSIS_PROVIDER=ollama`.
No model download is automatic.

## Interface and report delivery

| Tools | Observations or behavior |
| --- | --- |
| `get_server_status`, `get_storage_usage` | Runtime/system summary and monitored filesystem |
| `list_managed_services`, `list_containers` | Labelled services and container names |
| `inspect_container`, `get_container_stats`, `get_container_logs` | Selected container details, metrics and bounded log prefix |
| `analyze_container_logs`, `diagnose_container` | Objective signals with optional, fallible model advice |
| `generate_server_report`, `acknowledge_server_report` | Differential reports and optional explicit confirmation |
| `close_incomplete_log_interval` | Explicitly accept missing log coverage, retain captured snapshots and resume future reports |

The `server://inventory` resource describes capabilities; `/health` is liveness,
`/ready` checks Docker, writable state and the probe filesystem. Optional analysis
failure does not make objective tools unready. CPU/memory visibility depends on
the container environment; these are not guaranteed host-wide measurements.

Observation snapshots distinguish confirmed absence (`state=not_found`) from failed
inspection (`state=unknown`, `running=null`, `inspection_available=false`, with
`inspection_error`). A container-inspection HTTP 404 confirms absence; timeouts,
authorization errors and proxy failures do not. Failed inspections preserve the
last valid container baseline, so recovery compares against that observation.
`summary.inspection_errors` and `summary.metrics_errors` expose incomplete
coverage; reports include `collection_errors` with `origin`, `operation`
(`inventory`, `inspection` or `metrics`) and `error`. The readable report marks incomplete
coverage as `INCOMPLETE OBSERVATION`. Objective
severity describes confirmed findings, so `no_indication` does not imply that
unavailable observations were healthy.

Diagnostics expose `objective_data.metrics_available` and `metrics_error`.
Unavailable measurements are `null`, including nested values; valid measurement
groups, state and logs remain available. `metric_status` describes availability
and errors separately for CPU, memory, network and processes. Observation
snapshots use the same metric fields and nulls (also when statistics are skipped
for a stopped container). Consumers must not coerce these nulls to zero.

Inspection/statistics responses, observation snapshots and persisted container
and metric entries include `container_id`. Sustained CPU/memory counters continue
only for the same nonempty ID. Replacement containers and legacy metric entries
without an ID start a fresh sequence; missing samples reset the counters. If the
current ID is unavailable, samples cannot accumulate across observations. Existing
baselines remain readable without migration. Statistics collected for a different
ID from the inspection are discarded as unavailable.

Optional `EXPECTED_CONTAINER_NAMES` keeps configured services in reports after
they disappear. Sustained consumption requires three samples spanning at least
120 seconds, with at most 300 seconds between samples. Reports and `/ready`
expose observation coverage and freshness. An explicit recovery tool can close
an incomplete log interval while recording lost coverage and retaining captures.
See the [observation and recovery contract](docs/observation-contract.md) for
configuration, fields, migration behavior and retention limits.

For recoverable reports, request `acknowledged_delivery=true`, durably save/process
and **deduplicate by `delivery.id`**, then acknowledge that ID. Repeat until you
have acknowledged a report with `baseline.complete_logs=true`.
`baseline.pending_pages` counts captured streams with more pages to deliver and
can be zero even when capture failed.

When `baseline.log_errors` is nonzero, surface the failure, save and acknowledge
the delivery as usual, and request the next acknowledged report to retry capture
for the same interval. The acknowledgement can save partial progress while
keeping the interval open. The synthetic demo waits one second between attempts and
exits with an error after three consecutive capture failures. After restoring
capture, resume with the next acknowledged report on the same server state.
The interval keeps the original set of container identities. If a member is
removed or replaced before the interval is committed, the report stays incomplete.
The demo's in-memory storage is sufficient for its fixture. A real consumer
must persist the processed data and delivery IDs across restarts.

All clients share one pending report. A slow consumer blocks further report
intervals. The default immediate-save mode can lose a page if its response is lost.

Snapshots capture the whole retained interval before paging: initial latency and
disk use grow with that interval. Defaults cap snapshots at 256 MiB per container
per interval and 1 GiB across the snapshot directory, with 64 MiB best-effort free
space. Baseline and outbox files need extra space. Snapshots with saved cursors
survive Docker rotation. Records removed before/during capture cannot always be detected or
recovered; choose retention longer than the report interval. Local filesystem
locking and atomic replacement are required. See the [delivery, storage and
recovery contract](docs/reliability-and-evaluation.md) for concurrency and limits.

## Evidence, limitations and project status

The API, schemas and on-disk state are experimental and may change. There is no
stable compatibility promise. See [verified builds and platforms](docs/builds-and-compatibility.md)
for exact versions, dependency maintenance and untested platforms.

The Docker proxy permits selected GET endpoints on the host socket, including
metadata/log access for unlabelled containers by a direct proxy client. It is not
published to the host, but compromise of a container on its network matters. The
read-only socket mount alone does not enforce read-only Docker requests. MCP has
no authentication or per-user authorization. Raw log reads are not a privacy
filter; best-effort model/report redaction cannot identify every secret. Treat
local state and every output as sensitive and review before sharing.

Literal evidence validation cannot certify causes, safe advice or resistance to
prompt injection. Recorded synthetic results include substantial failures: the
[corrected three-operation regression](docs/evaluation/model-evaluation-all-functions-2026-10-29.md)
passed 8/18 narrow automated checks; the later [development run](docs/evaluation/reliability-followup-2026-10-29.md)
passed 10/12 on different cases. Those scores are not comparable or production
quality estimates. **Human quality ratings remain pending** (10 retained responses,
2 unreviewable rejections). [Complete that review](docs/evaluation/human-review.md) without
running a model. No independent review or real incident evaluation is claimed.

The [documentation index](docs/README.md) links to the development and
publication guides, design decisions and evaluation history. The
[scripts index](scripts/README.md) lists the commands and their inputs.
