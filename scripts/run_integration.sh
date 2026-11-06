#!/usr/bin/env bash
# Run only a new, isolated fixture project. Never source the private .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -eq 1 && ( $1 == --demo || $1 == --resilience ) ]]; then
    mode="${1#--}"
    export ANALYSIS_PROVIDER=disabled
elif [[ $# -eq 0 ]]; then
    mode=check
    export ANALYSIS_PROVIDER=ollama
else
    echo "Usage: bash scripts/run_integration.sh [--demo|--resilience]" >&2
    exit 2
fi

project="mcp-shadow-$mode-$(date +%s)-$$"
compose=(docker --host unix:///var/run/docker.sock compose --env-file .env.example -p "$project" -f tests/integration.compose.yaml)
cleanup() { "${compose[@]}" down --volumes --remove-orphans; }
run_demo() {
    "${compose[@]}" exec -T server python - --project "$project" --service log-fixture --synthetic < scripts/mcp_demo.py
}
trap cleanup EXIT
"${compose[@]}" config --quiet

if [[ $mode == resilience ]]; then
    phase() { "${compose[@]}" exec -T server python - "$1" < tests/docker_resilience_smoke.py; }
    "${compose[@]}" up -d --build server log-fixture
    phase normal
    "${compose[@]}" stop docker-proxy
    phase outage
    "${compose[@]}" start docker-proxy
    phase recovery
    phase pending
    "${compose[@]}" up -d --force-recreate --no-deps log-fixture
    "${compose[@]}" restart server
    phase replacement
    "${compose[@]}" rm -s -f log-fixture
    phase missing
    "${compose[@]}" up -d log-fixture
    phase returned
    exit
fi

if [[ $mode == demo ]]; then
    "${compose[@]}" up -d --build server log-fixture
    "${compose[@]}" exec -T server python - <<'PY'
import time

import httpx

with httpx.Client(timeout=5, trust_env=False) as client:
    for _ in range(60):
        try:
            ready = client.get("http://127.0.0.1:8000/ready")
        except httpx.RequestError:
            pass
        else:
            if ready.status_code == 200:
                if ready.json()["analysis_provider"] != "disabled":
                    raise SystemExit("Expected ANALYSIS_PROVIDER=disabled")
                break
        time.sleep(1)
    else:
        raise SystemExit("Temporary stack did not become ready")
PY
    run_demo
    exit
fi

"${compose[@]}" up -d --build
"${compose[@]}" exec -T server python -m pip check
"${compose[@]}" exec -T server python - < tests/docker_smoke.py
"${compose[@]}" restart server
"${compose[@]}" exec -T server python - < tests/docker_restart_smoke.py
run_demo
"${compose[@]}" exec -T server python - < tests/docker_fault_smoke.py
"${compose[@]}" stop docker-proxy
"${compose[@]}" exec -T server python -c 'import httpx; r = httpx.get("http://127.0.0.1:8000/ready", timeout=15); assert r.status_code == 503 and r.json()["checks"]["docker"] == "unavailable"; print("Readiness reports the temporary proxy outage")'
