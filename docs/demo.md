# A short demo using a real MCP client

From the repository root on Linux with Docker Compose v2 and access to the local
Docker socket, run:

```sh
bash scripts/run_integration.sh --demo
```

The script uses `.env.example`, sets `ANALYSIS_PROVIDER=disabled`, and waits for
`/ready` before running `scripts/mcp_demo.py` inside the server container. No host
ports are published. An exit trap removes the temporary project, its internal
network and named state volume.

The official MCP Python SDK client follows this sequence:

1. Initialize MCP and discover the labelled fixture. The application is a tool
   server that a client/agent can consume; it does not plan or act autonomously.
2. Query container state and metrics. Emit 40 controlled error lines, then read a
   bounded log prefix and request diagnostics. Running state and error signals
   are observations; they do not prove a cause. The fixture remains running.
3. Read differential report pages, request the same pending page again, compare
   replay, then acknowledge it. Verify 40 distinct generated messages across the
   fixed interval. The demo's sink is memory. A real consumer must durably save
   results and deduplicate delivery IDs before acknowledgement.

The printed JSON is a small projection; changing metrics and objective severity
are expected. Reports are allowed to include multiple pages. Snapshot capture
uses disk and reads the whole interval first. The demonstration does not measure
production incident quality or prove exhaustive access control.

## Full integration check

```sh
bash scripts/run_integration.sh
```

This starts a separate temporary project with `ANALYSIS_PROVIDER=ollama` and no
Ollama service. It checks that readiness reports Ollama as unavailable while
objective diagnostics remain usable and the model advisory stays null. It also
checks proxy restrictions, unlabelled fixture rejection, report replay after
restart, a full filesystem, Docker log rotation and readiness after a proxy
outage. It runs the same SDK client against the synthetic fixture.

The historical evaluations retain [negative model results](evaluation/model-evaluation-all-functions-2026-10-29.md)
and [pending human ratings](evaluation/human-review.md).
