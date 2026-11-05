"""Baseline comparisons and sustained resource usage evaluation."""

from __future__ import annotations

import os
from typing import Any

from shadow.events import _maximum_severity


def _sustained_metric_settings() -> dict[str, Any]:
    """Return conservative thresholds for sustained resource-usage detection."""

    def read_float(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))

        except (TypeError, ValueError):
            value = default

        return max(minimum, min(maximum, value))

    def read_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))

        except (TypeError, ValueError):
            value = default

        return max(minimum, min(maximum, value))

    cpu_threshold = read_float("SUSTAINED_CPU_THRESHOLD_PERCENT", 90.0, 1.0, 10000.0)
    memory_threshold = read_float(
        "SUSTAINED_MEMORY_THRESHOLD_PERCENT", 85.0, 1.0, 100.0
    )
    memory_high_threshold = read_float(
        "SUSTAINED_MEMORY_HIGH_THRESHOLD_PERCENT", 95.0, memory_threshold, 100.0
    )
    required_samples = read_int("SUSTAINED_METRIC_SAMPLES", 3, 2, 20)
    return {
        "cpu_threshold_percent": cpu_threshold,
        "memory_threshold_percent": memory_threshold,
        "high_memory_threshold_percent": memory_high_threshold,
        "required_samples": required_samples,
    }


def _safe_metric_percentage(value: Any) -> float:
    """Normalize a metric percentage to a non-negative float."""

    try:
        result = float(value or 0.0)

    except (TypeError, ValueError):
        result = 0.0

    return round(max(0.0, result), 2)


def _update_sustained_metric_entry(
    snapshot: dict[str, Any], previous: Any, observed_at: str
) -> dict[str, Any]:
    """Update consecutive CPU and memory threshold counters. A missing or sub-threshold sample resets consecutiveness."""

    settings = _sustained_metric_settings()

    if not isinstance(previous, dict):
        previous = {}

    metrics_available = bool(snapshot.get("metrics_available", False))
    running = bool(snapshot.get("running", False))
    valid_sample = metrics_available and running
    cpu_percent = _safe_metric_percentage(snapshot.get("cpu_percent", 0.0))
    memory = snapshot.get("memory", {})

    if not isinstance(memory, dict):
        memory = {}

    memory_percent = _safe_metric_percentage(memory.get("percent", 0.0))

    try:
        previous_cpu_count = max(0, int(previous.get("consecutive_high_cpu", 0) or 0))

    except (TypeError, ValueError):
        previous_cpu_count = 0

    try:
        previous_memory_count = max(
            0, int(previous.get("consecutive_high_memory", 0) or 0)
        )

    except (TypeError, ValueError):
        previous_memory_count = 0

    cpu_high = valid_sample and cpu_percent >= float(settings["cpu_threshold_percent"])
    memory_high = valid_sample and memory_percent >= float(
        settings["memory_threshold_percent"]
    )
    cpu_count = previous_cpu_count + 1 if cpu_high else 0
    memory_count = previous_memory_count + 1 if memory_high else 0
    return {
        "metrics_available": valid_sample,
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "high_cpu": cpu_high,
        "high_memory": memory_high,
        "consecutive_high_cpu": cpu_count,
        "consecutive_high_memory": memory_count,
        "required_samples": settings["required_samples"],
        "cpu_threshold_percent": settings["cpu_threshold_percent"],
        "memory_threshold_percent": settings["memory_threshold_percent"],
        "high_memory_threshold_percent": settings["high_memory_threshold_percent"],
        "observed_at": observed_at,
    }


def _evaluate_sustained_metrics(metric_entry: dict[str, Any]) -> dict[str, Any]:
    """Evaluate only metrics confirmed across consecutive observations."""

    severity = "no_indication"
    findings: list[dict[str, str]] = []

    if not bool(metric_entry.get("metrics_available", False)):
        return {"severity": severity, "findings": findings}

    try:
        required_samples = max(2, int(metric_entry.get("required_samples", 3) or 3))

    except (TypeError, ValueError):
        required_samples = 3

    try:
        cpu_count = max(0, int(metric_entry.get("consecutive_high_cpu", 0) or 0))

    except (TypeError, ValueError):
        cpu_count = 0

    try:
        memory_count = max(0, int(metric_entry.get("consecutive_high_memory", 0) or 0))

    except (TypeError, ValueError):
        memory_count = 0

    cpu_percent = _safe_metric_percentage(metric_entry.get("cpu_percent", 0.0))
    memory_percent = _safe_metric_percentage(metric_entry.get("memory_percent", 0.0))
    memory_high_threshold = _safe_metric_percentage(
        metric_entry.get("high_memory_threshold_percent", 95.0)
    )

    if cpu_count >= required_samples:
        findings.append(
            {
                "severity": "low",
                "finding": "High CPU "
                "usage sustained: "
                f"{cpu_percent:.1f} % "
                "in the current sample and "
                f"{cpu_count} observations "
                "consecutive above the "
                "threshold.",
            }
        )

    if memory_count >= required_samples:
        memory_severity = (
            "high" if (memory_percent >= memory_high_threshold) else "medium"
        )
        findings.append(
            {
                "severity": memory_severity,
                "finding": "High memory "
                "usage sustained: "
                f"{memory_percent:.1f} % "
                "in the current sample and "
                f"{memory_count} observations "
                "consecutive above the "
                "threshold.",
            }
        )

    for finding in findings:
        severity = _maximum_severity(severity, str(finding["severity"]))

    return {"severity": severity, "findings": findings}


def _container_baseline_entry(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract the minimal container state persisted between reports."""

    try:
        restarts = max(0, int(snapshot.get("restarts", 0) or 0))

    except (TypeError, ValueError):
        restarts = 0

    return {
        "state": str(snapshot.get("state", "unknown")),
        "running": bool(snapshot.get("running", False)),
        "health": str(snapshot.get("health", "no_healthcheck")),
        "oom_killed": bool(snapshot.get("oom_killed", False)),
        "restarts": restarts,
        "started_at": snapshot.get("started_at"),
    }


def _compare_container_with_baseline(
    snapshot: dict[str, Any], previous: Any, baseline_exists: bool
) -> dict[str, Any]:
    """Compare current container signals with the previous persisted state. This function does not calculate severity or interpret logs."""

    name = str(snapshot.get("name", "unknown"))
    result: dict[str, Any] = {
        "changes": [],
        "new_restarts": 0,
        "new_oom": False,
        "state_changed": False,
        "health_changed": False,
    }

    if not baseline_exists:
        return result

    if not isinstance(previous, dict):
        result["changes"].append(
            {
                "type": "new_service",
                "origin": name,
                "description": "Service added to the observed inventory.",
            }
        )
        return result

    current_entry = _container_baseline_entry(snapshot)
    previous_state = str(previous.get("state", "unknown"))
    current_state = str(current_entry["state"])
    previous_running = bool(previous.get("running", (previous_state == "running")))
    current_running = bool(current_entry["running"])

    if previous_state != current_state or previous_running != current_running:
        result["state_changed"] = True
        result["changes"].append(
            {
                "type": "state",
                "origin": name,
                "description": "State changed from "
                f"{previous_state} "
                f"to {current_state}.",
            }
        )

    previous_health = str(previous.get("health", "no_healthcheck"))
    current_health = str(current_entry["health"])

    if previous_running and current_running and previous_health != current_health:
        result["health_changed"] = True
        result["changes"].append(
            {
                "type": "health",
                "origin": name,
                "description": "Healthcheck changed from "
                f"{previous_health} "
                f"to {current_health}.",
            }
        )

    try:
        previous_restarts = max(0, int(previous.get("restarts", 0) or 0))

    except (TypeError, ValueError):
        previous_restarts = 0

    current_restarts = int(current_entry["restarts"])

    if current_restarts > previous_restarts:
        restart_delta = current_restarts - previous_restarts
        result["new_restarts"] = restart_delta
        result["changes"].append(
            {
                "type": "restarts",
                "origin": name,
                "count": restart_delta,
                "description": f"{restart_delta} new restart(s).",
            }
        )

    elif current_restarts < previous_restarts:
        result["changes"].append(
            {
                "type": "restart_count",
                "origin": name,
                "description": "The restart counter "
                "was reset, "
                "probably due to a "
                "container recreation.",
            }
        )

    previous_oom = bool(previous.get("oom_killed", False))
    current_oom = bool(current_entry["oom_killed"])

    if current_oom and not previous_oom:
        result["new_oom"] = True
        result["changes"].append(
            {
                "type": "oom",
                "origin": name,
                "description": "Docker reports a new OOMKilled.",
            }
        )

    elif previous_oom and not current_oom:
        result["changes"].append(
            {
                "type": "oom_cleared",
                "origin": name,
                "description": "The OOMKilled flag is no longer active.",
            }
        )

    return result
