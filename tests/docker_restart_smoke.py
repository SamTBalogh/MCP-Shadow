"""Verify a real process restart with an unacknowledged report and paged logs."""
import json
import time
from pathlib import Path

import httpx

with httpx.Client(timeout=60, trust_env=False) as client:
    for _ in range(40):
        try:
            if client.get("http://127.0.0.1:8000/ready").status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.5)
    else:
        raise AssertionError("Restarted server did not become ready")

    def call(name, arguments):
        response = client.post("http://127.0.0.1:8000/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}})
        response.raise_for_status()
        body = response.json()["result"]
        assert not body.get("isError"), "Unexpected MCP tool error"
        return body["structuredContent"]

    expected = json.loads(Path("/app/state/integration-expected-delivery.json").read_text())
    report = call("generate_server_report", {"acknowledged_delivery": True})
    assert report == expected, "An undelivered report changed across restart"
    delivered = []
    for _ in range(100):
        for container in report["log_activity"]["containers"]:
            if "log-fixture" in container["name"]:
                delivered.extend(event["message"] for event in container["events"])
        identifier = report["delivery"]["id"]
        assert call("acknowledge_server_report", {"delivery_id": identifier})["acknowledged"]
        assert call("acknowledge_server_report", {"delivery_id": identifier})["already_acknowledged"]
        if report["baseline"]["pending_pages"] == 0:
            break
        report = call("generate_server_report", {"acknowledged_delivery": True})
        assert report == call("generate_server_report", {"acknowledged_delivery": True})
    else:
        raise AssertionError("Acknowledged pages did not complete")
    assert len(delivered) == 40, len(delivered)
    assert len(set(delivered)) == 40
    print("Real restart, unacknowledged replay, idempotent acknowledgement and complete isolated log delivery passed")
