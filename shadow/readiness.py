"""Dependency checks for the server readiness endpoint."""
from __future__ import annotations

import shutil
import tempfile
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError


def check_readiness(*, docker_get, state_dir, disk_probe_path, provider, provider_name) -> dict[str, Any]:
    """Check required dependencies and report optional model availability."""
    checks: dict[str, str] = {}
    try:
        docker_get("/_ping")
        checks["docker"] = "ok"
    except ToolError:
        checks["docker"] = "unavailable"

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=state_dir) as probe:
            probe.write(b"ready")
            probe.flush()
        checks["state"] = "ok"
    except OSError:
        checks["state"] = "unavailable"

    try:
        shutil.disk_usage(disk_probe_path)
        checks["storage"] = "ok"
    except OSError:
        checks["storage"] = "unavailable"

    checks["analysis"] = provider.availability()
    if provider_name == "ollama":
        checks["ollama"] = checks["analysis"]  # Compatibility with existing monitors.

    ready = all(checks[key] == "ok" for key in ("docker", "state", "storage"))
    return {"status": "ready" if ready else "not_ready", "checks": checks,
            "analysis_provider": provider_name, "analysis_circuit": provider.status()}
