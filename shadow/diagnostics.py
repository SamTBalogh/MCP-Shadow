"""Container snapshots and model-assisted diagnostics."""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import re
import time
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from shadow.statistics import parse_statistics
from shadow.events import _filter_expected_shutdown_noise, _maximum_severity, _redact_sensitive_text


class DiagnosticService:
    def __init__(
        self, *, find_container: Callable, docker_get: Callable, get_logs: Callable,
        filter_profile_noise: Callable, expected_stopped_names: Callable,
        collect_snapshot: Callable, analyze_snapshot: Callable,
        cache: dict, cache_ttl: float, model: str,
    ):
        self.find_container = find_container
        self.docker_get = docker_get
        self.get_logs = get_logs
        self.filter_profile_noise = filter_profile_noise
        self.expected_stopped_names = expected_stopped_names
        self.collect_snapshot = collect_snapshot
        self.analyze_snapshot = analyze_snapshot
        self.cache = cache
        self.cache_ttl = cache_ttl
        self.model = model

    def snapshot(self, container_name: str, lines: int) -> dict[str, Any]:
        """Collect container state, metrics, and logs without modifying the container."""

        container = self.find_container(container_name)
        container_id = str(container["Id"])

        try:
            inspect_data = self.docker_get(f"/containers/{container_id}/json").json()

        except ValueError as exception:
            raise ToolError("Docker returned invalid inspection data.") from exception

        if not isinstance(inspect_data, dict):
            raise ToolError("Docker inspection data has an unexpected format.")

        stats_data: dict[str, Any] = {}
        metrics_error = ""

        try:
            stats_result = self.docker_get(
                f"/containers/{container_id}/stats",
                params={"stream": "false", "one-shot": "true"},
            ).json()

            if not isinstance(stats_result, dict) or not stats_result:
                raise ToolError("Docker statistics have an unexpected format.")
            stats_data = stats_result

        except (ToolError, ValueError, TypeError) as exception:
            metrics_error = str(exception)

        state = inspect_data.get("State", {})

        if not isinstance(state, dict):
            state = {}

        health_data = state.get("Health", {})

        if isinstance(health_data, dict):
            health = str(health_data.get("Status", "unknown"))

        else:
            health = "not_configured"

        metrics = parse_statistics(stats_data, metrics_error)

        logs_data = self.get_logs(container_name, lines)
        raw_logs = str(logs_data.get("logs", ""))
        redacted_logs = _redact_sensitive_text(raw_logs)
        (generic_filtered_logs, shutdown_noise_lines) = _filter_expected_shutdown_noise(
            redacted_logs
        )
        (profile_filtered_logs, profile_noise_lines, profile_warnings) = (
            self.filter_profile_noise(container_name, generic_filtered_logs)
        )
        analysis_logs = profile_filtered_logs
        service_noise_lines = 0
        indicators = {
            "errors": len(
                re.findall(
                    r"(?i)\b"
                    r"(error|err|exception|"
                    r"traceback|failed|"
                    r"failure)"
                    r"\b",
                    analysis_logs,
                )
            ),
            "warnings": len(
                re.findall(
                    r"(?i)\b"
                    r"(warn|warning|wrn)"
                    r"\b",
                    analysis_logs,
                )
            ),
            "timeouts": len(
                re.findall(
                    r"(?i)"
                    r"(?:"
                    r"\btimed\s+out\b|"
                    r"\bdeadline\s+exceeded\b|"
                    r"\btimeout\s+(?:while|"
                    r"waiting|connecting|"
                    r"reading|writing|"
                    r"reached|expired)\b|"
                    r"\b(?:request|connection|"
                    r"operation|upstream|"
                    r"gateway)\s+timed\s+out\b"
                    r")",
                    analysis_logs,
                )
            ),
            "access_denied": len(
                re.findall(
                    r"(?i)"
                    r"(permission denied|"
                    r"access denied|"
                    r"unauthorized|"
                    r"forbidden)",
                    analysis_logs,
                )
            ),
            "critical_failures": len(
                re.findall(
                    r"(?i)"
                    r"(\bpanic\b|"
                    r"\bfatal\b|"
                    r"\bftl\b|"
                    r"segmentation fault|"
                    r"out of memory|"
                    r"oom[- ]kill)",
                    analysis_logs,
                )
            ),
        }
        normalized_name = container_name.strip().lstrip("/")
        stopped_expected = normalized_name in self.expected_stopped_names()
        facts = {
            "expected_stopped": stopped_expected,
            "state": str(state.get("Status", "unknown")),
            "running": bool(state.get("Running", False)),
            "health": health,
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled", False)),
            "state_error": str(state.get("Error", "")),
            "restarts": int(inspect_data.get("RestartCount", 0) or 0),
            "started_at": str(state.get("StartedAt", "")),
            **metrics,
        }
        objective_findings: list[dict[str, str]] = []

        if facts["oom_killed"]:
            objective_findings.append(
                {
                    "severity": "critical",
                    "finding": "Docker reports that the "
                    "container was terminated "
                    "due to insufficient memory.",
                }
            )

        if not facts["running"] and not stopped_expected:
            objective_findings.append(
                {"severity": "high", "finding": "The container is not running."}
            )

        if facts["running"] and health == "unhealthy":
            objective_findings.append(
                {
                    "severity": "high",
                    "finding": "The Docker healthcheck reports an unhealthy state.",
                }
            )

        if metrics["memory"]["percent"] is not None and metrics["memory"]["percent"] >= 95:
            objective_findings.append(
                {"severity": "high", "finding": "Memory usage exceeds 95% of the limit."}
            )

        elif metrics["memory"]["percent"] is not None and metrics["memory"]["percent"] >= 85:
            objective_findings.append(
                {"severity": "medium", "finding": "Memory usage exceeds 85% of the limit."}
            )

        if facts["restarts"] > 0:
            objective_findings.append(
                {
                    "severity": "low",
                    "finding": f"Docker reports {facts['restarts']} total restart(s).",
                }
            )

        incident_indicators = (
            indicators["errors"] + indicators["timeouts"] + indicators["access_denied"]
        )

        if indicators["critical_failures"] > 0:
            objective_findings.append(
                {
                    "severity": "high",
                    "finding": "Logs contain indicators associated with critical failures.",
                }
            )

        elif (
            indicators["errors"] >= 3
            or indicators["timeouts"] >= 3
            or indicators["access_denied"] >= 3
            or incident_indicators >= 5
        ):
            objective_findings.append(
                {
                    "severity": "medium",
                    "finding": "Logs contain several issues that require review.",
                }
            )

        elif incident_indicators > 0 or indicators["warnings"] > 0:
            objective_findings.append(
                {
                    "severity": "low",
                    "finding": "Logs contain "
                    "warnings or "
                    "isolated issues, "
                    "without objective evidence "
                    "of severe degradation.",
                }
            )

        objective_severity = "no_indication"

        for finding in objective_findings:
            objective_severity = _maximum_severity(objective_severity, finding["severity"])

        return {
            "name": container_name.strip().lstrip("/"),
            "container_id": container_id,
            "objective_data": facts,
            "log_indicators": indicators,
            "objective_findings": objective_findings,
            "objective_severity": objective_severity,
            "logs": analysis_logs,
            "generic_noise_lines_omitted": shutdown_noise_lines,
            "profile_noise_lines_omitted": profile_noise_lines,
            "profile_warnings": profile_warnings,
            "service_noise_lines_omitted": service_noise_lines,
            "noise_lines_omitted": (
                shutdown_noise_lines + profile_noise_lines + service_noise_lines
            ),
            "logs_truncated": bool(logs_data.get("truncated", False)),
        }

    def diagnose(self, container_name: str, lines: int = 100) -> dict[str, Any]:
        """Run a local read-only container diagnostic combining state, healthcheck, restarts, CPU, memory, network, processes, and logs. Model conclusions are advisory and their evidence is validated against observed data. Objective data is collected on every call; only advisory analysis of identical inputs may be cached briefly."""

        normalized_name = container_name.strip().lstrip("/")
        snapshot = self.collect_snapshot(normalized_name, lines)
        cache_key = f"{normalized_name}:{lines}"
        fingerprint = hashlib.sha256(json.dumps(
            {"model": self.model, "snapshot": snapshot}, sort_keys=False,
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        started = time.monotonic()
        cached = self.cache.get(cache_key)
        cache_hit = False
        cache_age = 0.0
        model_analysis = None
        if cached is not None and self.cache_ttl > 0:
            cached_at, entry = cached
            cache_age = started - cached_at
            if (0 <= cache_age <= self.cache_ttl and isinstance(entry, dict)
                    and entry.get("input_sha256") == fingerprint):
                model_analysis = deepcopy(entry["analysis"])
                cache_hit = True
            else:
                self.cache.pop(cache_key, None)

        if model_analysis is None:
            try:
                model_analysis = self.analyze_snapshot(deepcopy(snapshot))
                if self.cache_ttl > 0:
                    self.cache[cache_key] = (started, {
                        "input_sha256": fingerprint, "analysis": deepcopy(model_analysis)})
            except ToolError as exception:
                model_analysis = {
                    "model": self.model,
                    "model_severity": None,
                    "summary": "Model analysis is unavailable; objective findings are still available.",
                    "findings": [],
                    "limitations": [str(exception)],
                    "confidence": 0.0,
                    "ai_input_truncated": False,
                }
        final_severity = snapshot["objective_severity"]
        result = {
            "name": snapshot["name"],
            "container_id": snapshot.get("container_id"),
            "mode": "read_only",
            "final_severity": final_severity,
            "objective_severity": snapshot["objective_severity"],
            "model_severity_advisory": model_analysis["model_severity"],
            "objective_data": snapshot["objective_data"],
            "log_indicators": snapshot["log_indicators"],
            "objective_findings": snapshot["objective_findings"],
            "model_analysis": model_analysis,
            "logs_truncated": snapshot["logs_truncated"],
            "cache": {
                "hit": cache_hit,
                "age_seconds": round(cache_age, 2) if cache_hit else 0.0,
                "ttl_seconds": self.cache_ttl,
                "scope": "model_analysis",
                "objective_data_refreshed": True,
            },
        }

        return result
