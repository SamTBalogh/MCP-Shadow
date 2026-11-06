"""Baseline comparisons and sustained resource usage evaluation."""

from __future__ import annotations

import os
import math
from datetime import datetime
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
        "min_duration_seconds": read_float("SUSTAINED_MIN_DURATION_SECONDS", 120, 1, 86400),
        "max_sample_gap_seconds": read_float("SUSTAINED_MAX_SAMPLE_GAP_SECONDS", 300, 1, 86400),
    }


def _safe_metric_percentage(value: Any) -> float:
    """Normalize a metric percentage to a non-negative float."""

    try:
        result = float(value or 0.0)

    except (TypeError, ValueError):
        result = 0.0

    return round(max(0.0, result), 2)


def _metric_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _update_sustained_metric_entry(
    snapshot: dict[str, Any], previous: Any, observed_at: str
) -> dict[str, Any]:
    """Require consecutive identified samples spanning a bounded time window."""

    settings = _sustained_metric_settings()

    if not isinstance(previous, dict):
        previous = {}

    container_id = snapshot.get("container_id")
    if not container_id or previous.get("container_id") != container_id:
        # Legacy references cannot establish identity. Start a fresh sequence.
        previous = {}

    now = _metric_timestamp(observed_at)
    last = _metric_timestamp(previous.get("observed_at"))
    gap = (now - last).total_seconds() if now is not None and last is not None else None
    if gap is None or gap < 0 or gap > settings["max_sample_gap_seconds"]:
        previous = {}
    if any(previous.get(key) != settings[key] for key in
           ("cpu_threshold_percent", "memory_threshold_percent")):
        previous = {}

    metrics_available = bool(snapshot.get("metrics_available", False))
    running = bool(snapshot.get("running", False))
    cpu_percent = snapshot.get("cpu_percent")
    memory = snapshot.get("memory", {})

    if not isinstance(memory, dict):
        memory = {}

    memory_percent = memory.get("percent")
    def available(field, value):
        status = snapshot.get("metric_status", {}).get(field, {})
        return (running and status.get("available", metrics_available)
                and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    cpu_valid = available("cpu_percent", cpu_percent)
    memory_valid = available("memory", memory_percent)
    valid_sample = cpu_valid or memory_valid

    cpu_high = cpu_valid and cpu_percent >= float(settings["cpu_threshold_percent"])
    memory_high = memory_valid and memory_percent >= float(
        settings["memory_threshold_percent"]
    )
    def sequence(kind, high):
        if not high or now is None:
            return 0, None, 0.0
        start_text = previous.get(f"high_{kind}_since")
        start = _metric_timestamp(start_text)
        try:
            count = max(0, int(previous.get(f"consecutive_high_{kind}", 0)))
        except (TypeError, ValueError):
            count = 0
        if not count or start is None or last is None or start > last:
            return 1, observed_at, 0.0
        return count + int(gap != 0), start_text, (now - start).total_seconds()

    cpu_count, cpu_since, cpu_duration = sequence("cpu", cpu_high)
    memory_count, memory_since, memory_duration = sequence("memory", memory_high)
    return {
        "container_id": container_id,
        "metrics_available": valid_sample,
        "cpu_percent": cpu_percent if cpu_valid else None,
        "memory_percent": memory_percent if memory_valid else None,
        "high_cpu": cpu_high,
        "high_memory": memory_high,
        "consecutive_high_cpu": cpu_count,
        "consecutive_high_memory": memory_count,
        "high_cpu_since": cpu_since,
        "high_memory_since": memory_since,
        "high_cpu_duration_seconds": cpu_duration,
        "high_memory_duration_seconds": memory_duration,
        "min_duration_seconds": settings["min_duration_seconds"],
        "max_sample_gap_seconds": settings["max_sample_gap_seconds"],
        "sustained_high_cpu": cpu_count >= settings["required_samples"] and cpu_duration >= settings["min_duration_seconds"],
        "sustained_high_memory": memory_count >= settings["required_samples"] and memory_duration >= settings["min_duration_seconds"],
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

    if cpu_count >= required_samples and metric_entry.get("sustained_high_cpu"):
        findings.append(
            {
                "severity": "low",
                "finding": "High CPU "
                "usage sustained: "
                f"{cpu_percent:.1f} % "
                "in the current sample and "
                f"{cpu_count} observations "
                "consecutive above the "
                f"threshold over {metric_entry['high_cpu_duration_seconds']:g} seconds.",
            }
        )

    if memory_count >= required_samples and metric_entry.get("sustained_high_memory"):
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
                f"threshold over {metric_entry['high_memory_duration_seconds']:g} seconds.",
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
        "container_id": snapshot.get("container_id"),
        "expected": bool(snapshot.get("expected", False)),
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

    if not baseline_exists or snapshot.get("inspection_available") is False:
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
    if (previous.get("container_id") and current_entry.get("container_id")
            and previous["container_id"] != current_entry["container_id"]):
        result["changes"].append({"type": "recreated_service", "origin": name,
                                  "description": "Container identity changed; a new instance is being observed."})
        return result
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
