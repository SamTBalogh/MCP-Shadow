"""Multi-phase MCP checks; only invoked inside the isolated synthetic stack."""
import json
import os
from pathlib import Path
import sys
import time
import httpx

phase = sys.argv[1]
project = os.environ["SHADOW_TEST_PROJECT"]
assert project.startswith("mcp-shadow-resilience-")
assert os.environ["ANALYSIS_PROVIDER"] == "disabled"
root = Path("/app/state")
fixture = project + "-log-fixture-1"

with httpx.Client(timeout=60, trust_env=False) as client:
    for _ in range(60):
        try:
            response = client.get("http://127.0.0.1:8000/health" if phase == "outage" else "http://127.0.0.1:8000/ready")
            if response.status_code == 200: break
        except httpx.RequestError:
            pass
        time.sleep(0.5)
    else:
        raise AssertionError("Temporary server did not become available")

    def call(name, **args):
        response = client.post("http://127.0.0.1:8000/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        response.raise_for_status()
        result = response.json()["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]

    def report():
        return call("generate_server_report", acknowledged_delivery=True)

    def ack(item):
        return call("acknowledge_server_report", delivery_id=item["delivery"]["id"])

    def drain(item=None):
        for _ in range(100):
            item = item or report()
            assert item["baseline"]["log_errors"] == 0, item
            ack(item)
            if item["baseline"]["complete_logs"]: return item
            item = None
        raise AssertionError("Synthetic interval did not finish")

    if phase == "normal":
        item = drain()
        assert item["summary"]["not_found"] == 0
        (root / "resilience-baseline.json").write_text(json.dumps(json.loads((root / "baseline.json").read_text())["containers"]))
    elif phase == "outage":
        item = report()
        assert item["collection_errors"]
        assert item["summary"]["not_found"] == 0
        assert "INCOMPLETE OBSERVATION" in item["markdown"]
        ack(item)
        assert json.loads((root / "baseline.json").read_text())["containers"] == json.loads((root / "resilience-baseline.json").read_text())
        health = client.get("http://127.0.0.1:8000/ready").json()["monitoring"]
        assert health["query_errors"]["logs"] > 0
    elif phase in {"recovery", "returned"}:
        item = drain()
        assert item["summary"]["not_found"] == 0
        assert item["summary"]["new_restarts"] == 0
        assert item["summary"]["new_ooms"] == 0
    elif phase == "pending":
        client.post("http://log-fixture:8001/emit").raise_for_status()
        time.sleep(2)
        item = report()
        assert item["baseline"]["pending_pages"] > 0
        (root / "resilience-pending.json").write_text(json.dumps(item))
    elif phase == "replacement":
        item = report()
        assert item == json.loads((root / "resilience-pending.json").read_text())
        ack(item)
        blocked = report()
        assert blocked["baseline"]["log_errors"] > 0
        assert any(change["type"] == "recreated_service" for change in blocked["changes"])
        ack(blocked)
        identity = blocked["baseline"]["interval_id"]
        result = call("close_incomplete_log_interval", interval_id=identity, reason="Synthetic replacement made the old interval incomplete")
        assert result["coverage_lost"]
        assert call("close_incomplete_log_interval", interval_id=identity, reason="retry")["already_closed"]
        archived = json.loads((root / "baseline.json").read_text())["log_gaps"][0]["progress"]
        item = drain()
        assert item["log_gaps"][0]["interval_id"] == identity
        for cursor in archived["containers"].values():
            if "spool_id" in cursor:
                assert (root / "log-spool" / (cursor["spool_id"] + ".bin")).exists()
    elif phase == "missing":
        item = drain()
        assert item["summary"]["not_found"] == 1
        assert any(incident["origin"] == fixture and incident["severity"] == "high" for incident in item["incidents"])
    else:
        raise AssertionError("Unknown phase")
    print("Synthetic resilience phase passed:", phase)
