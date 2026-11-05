"""Run inside the integration server container against real MCP and proxy HTTP."""

import time
import json
import os
from pathlib import Path

import httpx


headers = {"Accept": "application/json, text/event-stream"}
with httpx.Client(timeout=180, trust_env=False) as client:
    for _ in range(40):
        try:
            ready = client.get("http://127.0.0.1:8000/ready")
            if ready.status_code == 200:
                break
        except httpx.RequestError:
            pass
        time.sleep(0.5)
    else:
        raise AssertionError("Server dependencies did not become ready")

    assert ready.json()["checks"]["ollama"] == "unavailable"
    assert client.get("http://127.0.0.1:8000/health").json()["status"] == "ok"

    def request(method, params):
        response = client.post(
            "http://127.0.0.1:8000/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
        assert "error" not in body, body
        result = body["result"]
        assert not result.get("isError"), result
        return result.get("structuredContent", result)

    request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "integration-test", "version": "1"}})
    tools = request("tools/list", {})["tools"]
    assert "generate_server_report" in {tool["name"] for tool in tools}
    services = request("tools/call", {"name": "list_managed_services", "arguments": {}})
    services = services.get("result", services) if isinstance(services, dict) else services
    own_service = next(service for service in services
                       if service["compose_project"] == os.environ["SHADOW_TEST_PROJECT"]
                       and service["compose_service"] == "server")
    containers = request("tools/call", {"name": "list_containers", "arguments": {}})
    containers = containers.get("result", containers) if isinstance(containers, dict) else containers
    assert own_service["container_name"] in {item["name"] for item in containers}
    status = request("tools/call", {"name": "get_server_status", "arguments": {}})
    assert "storage" in status and "containers" in status
    for tool in ("inspect_container", "get_container_stats"):
        details = request("tools/call", {"name": tool, "arguments": {
            "container_name": own_service["container_name"],
        }})
        assert details["name"] == own_service["container_name"]
    assert client.get("http://docker-proxy:2375/images/json").status_code == 403
    assert client.post("http://docker-proxy:2375/containers/nonexistent/stop").status_code == 403
    # Use only this temporary project's unlabelled fixture. The proxy has broader
    # read access than the MCP application; labels are not proxy isolation.
    fixtures = client.get("http://docker-proxy:2375/containers/json", params={
        "all": "true", "filters": json.dumps({"label": [
            "com.docker.compose.project=" + os.environ["SHADOW_TEST_PROJECT"]]})
    }).json()
    unlabelled = next(item for item in fixtures
                      if item["Labels"].get("com.docker.compose.service") == "rotation-fixture")
    unlabelled_name = unlabelled["Names"][0].lstrip("/")
    assert unlabelled_name not in {item["name"] for item in containers}
    assert unlabelled_name not in {item["container_name"] for item in services}
    assert client.get(f"http://docker-proxy:2375/containers/{unlabelled['Id']}/json").status_code == 200
    for tool in ("inspect_container", "get_container_stats", "get_container_logs",
                 "analyze_container_logs", "diagnose_container"):
        result = client.post("http://127.0.0.1:8000/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": tool, "arguments": {"container_name": unlabelled_name}}}).json()["result"]
        assert result.get("isError"), tool
    for method, path in (("GET", "/containers/nonexistent/archive"),
                         ("GET", "/events"), ("DELETE", "/containers/nonexistent"),
                         ("POST", "/containers/nonexistent/exec")):
        assert client.request(method, "http://docker-proxy:2375" + path).status_code == 403
    report = request("tools/call", {"name": "generate_server_report", "arguments": {}})
    assert report["baseline"]["saved"]
    assert client.post("http://log-fixture:8001/emit").status_code == 204
    time.sleep(1.1)
    logs = request("tools/call", {"name": "get_container_logs", "arguments": {
        "container_name": own_service["container_name"], "lines": 10,
    }})
    assert isinstance(logs["logs"], str)
    delivered = []
    for _ in range(100):
        second_report = request("tools/call", {"name": "generate_server_report", "arguments": {}})
        assert second_report["baseline"]["existed"]
        for container in second_report["log_activity"]["containers"]:
            if "log-fixture" in container["name"]:
                delivered.extend(event["message"] for event in container["events"])
        if second_report["baseline"]["saved"]:
            break
        assert second_report["baseline"]["progress_saved"], second_report["baseline"]
    else:
        raise AssertionError("Log pages did not finish")
    assert len(delivered) == 40, len(delivered)
    assert len(delivered) == len(set(delivered)), "Duplicate fixture events"
    diagnostic = request("tools/call", {"name": "diagnose_container", "arguments": {
        "container_name": own_service["container_name"], "lines": 10,
    }})
    assert diagnostic["model_severity_advisory"] is None
    assert diagnostic["objective_data"]
    assert client.post("http://log-fixture:8001/emit").status_code == 204
    time.sleep(1.1)
    pending = request("tools/call", {"name": "generate_server_report", "arguments": {"acknowledged_delivery": True}})
    replay = request("tools/call", {"name": "generate_server_report", "arguments": {"acknowledged_delivery": True}})
    assert pending == replay
    assert not pending["baseline"]["saved"]
    assert pending["delivery"]["acknowledgement_required"]
    expected = Path("/app/state/integration-expected-delivery.json")
    expected.write_text(json.dumps(pending))
    os.chmod(expected, 0o600)
    print("MCP, proxy restrictions, isolated paged logs, offline diagnostics and durable replay passed; restart test prepared")
