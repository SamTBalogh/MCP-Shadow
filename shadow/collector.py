"""Collect Docker observations and differential log events."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from shadow.events import (
    _classify_event_relevance, _event_fingerprint,
    _group_log_lines_into_events, _maximum_severity,
)
from shadow.metrics import (
    _compare_container_with_baseline, _container_baseline_entry,
    _evaluate_sustained_metrics, _update_sustained_metric_entry,
)


class ObservationCollector:
    def __init__(
        self, *, inspect_container: Callable, get_stats: Callable,
        expected_stopped_names: Callable, managed_names: Callable, disk_snapshot: Callable,
        normalize_fingerprints: Callable, load_profiles: Callable,
        baseline_to_since: Callable, get_logs: Callable, resolve_profile: Callable,
        objective_snapshot: Callable, docker_workers: int, baseline_version: int,
        max_log_lines: int, get_log_page: Callable | None = None,
        managed_log_ids: Callable[[], dict[str, str]] | None = None,
    ):
        if get_log_page is not None and managed_log_ids is None:
            raise ValueError("Paged log collection requires managed container identities.")
        self.inspect_container = inspect_container
        self.get_stats = get_stats
        self.expected_stopped_names = expected_stopped_names
        self.managed_names = managed_names
        self.disk_snapshot = disk_snapshot
        self.normalize_fingerprints = normalize_fingerprints
        self.load_profiles = load_profiles
        self.baseline_to_since = baseline_to_since
        self.get_logs = get_logs
        self.resolve_profile = resolve_profile
        self.objective_snapshot = objective_snapshot
        self.docker_workers = docker_workers
        self.baseline_version = baseline_version
        self.max_log_lines = max_log_lines
        self.get_log_page = get_log_page
        self.managed_log_ids = managed_log_ids

    def snapshot(self, container_name: str) -> dict[str, Any]:
        """Collect objective signals for the current container state. This function does not inspect logs or calculate severity."""

        normalized_name = container_name.strip().lstrip("/")
        inspection = self.inspect_container(normalized_name)
        running = bool(inspection.get("running", False))
        metrics: dict[str, Any] = {
            "cpu_percent": 0.0,
            "memory": {"usage": "0.0 B", "limit": "0.0 B", "percent": 0.0},
            "network": {"received": "0.0 B", "sent": "0.0 B"},
            "processes": 0,
        }
        metrics_available = False
        metrics_error = ""

        if running:
            try:
                current_metrics = self.get_stats(normalized_name)

                if isinstance(current_metrics, dict):
                    metrics.update(current_metrics)
                    metrics_available = True

            except (ToolError, ValueError, TypeError) as exception:
                metrics_error = str(exception)

        return {
            "name": normalized_name,
            "state": str(inspection.get("state", "unknown")),
            "running": running,
            "expected_stopped": normalized_name in self.expected_stopped_names(),
            "health": str(inspection.get("health", "no_healthcheck")),
            "oom_killed": bool(inspection.get("oom_killed", False)),
            "restarts": int(inspection.get("restart_count", 0) or 0),
            "exit_code": inspection.get("exit_code"),
            "state_error": str(inspection.get("error", "")),
            "started_at": inspection.get("started_at"),
            "finished_at": inspection.get("finished_at"),
            "metrics_available": metrics_available,
            "metrics_error": metrics_error,
            "cpu_percent": float(metrics.get("cpu_percent", 0.0) or 0.0),
            "memory": {
                "usage": metrics.get("memory", {}).get("usage", "0.0 B"),
                "limit": metrics.get("memory", {}).get("limit", "0.0 B"),
                "percent": metrics.get("memory", {}).get("percent", 0.0),
            },
            "network": {
                "received": metrics.get("network", {}).get("received", "0.0 B"),
                "sent": metrics.get("network", {}).get("sent", "0.0 B"),
            },
            "processes": int(metrics.get("processes", 0) or 0),
        }

    def evaluate(
        self,
        snapshot: dict[str, Any], comparison: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate objective container signals only. Logs and model conclusions do not contribute to this severity."""

        severity = "no_indication"
        findings: list[dict[str, str]] = []
        state = str(snapshot.get("state", "unknown"))
        running = bool(snapshot.get("running", False))
        stopped_expected = bool(snapshot.get("expected_stopped", False))
        health = str(snapshot.get("health", "no_healthcheck"))

        if bool(snapshot.get("oom_killed", False)):
            findings.append(
                {
                    "severity": "critical",
                    "finding": "Docker still reports the OOMKilled flag.",
                }
            )

        if state == "not_found":
            findings.append(
                {"severity": "high", "finding": "The expected container was not found."}
            )

        elif not running and not stopped_expected:
            findings.append(
                {"severity": "high", "finding": "The container is not running."}
            )

        if running and health == "unhealthy":
            findings.append(
                {
                    "severity": "high",
                    "finding": "The Docker healthcheck reports an unhealthy state.",
                }
            )

        try:
            new_restarts = max(0, int(comparison.get("new_restarts", 0) or 0))

        except (TypeError, ValueError):
            new_restarts = 0

        if new_restarts > 0:
            findings.append(
                {
                    "severity": "low",
                    "finding": f"{new_restarts} new restart(s) since the previous report.",
                }
            )

        for finding in findings:
            severity = _maximum_severity(severity, str(finding["severity"]))

        return {"severity": severity, "findings": findings}

    def collect_state(
        self,
        baseline: dict[str, Any], baseline_exists: bool
    ) -> dict[str, Any]:
        """Collect objective state for all managed containers. This function does not inspect logs or invoke Ollama."""

        generated_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        previous_containers = baseline.get("containers", {})

        if not isinstance(previous_containers, dict):
            previous_containers = {}

        previous_metrics = baseline.get("metrics", {})

        if not isinstance(previous_metrics, dict):
            previous_metrics = {}

        summary = {
            "expected_containers": len(self.managed_names()),
            "running": 0,
            "stopped": 0,
            "expected_stopped": 0,
            "unexpectedly_stopped": 0,
            "not_found": 0,
            "healthy": 0,
            "unhealthy": 0,
            "no_healthcheck": 0,
            "new_restarts": 0,
            "new_ooms": 0,
            "metrics_available": 0,
            "sustained_high_cpu": 0,
            "sustained_high_memory": 0,
        }
        containers: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        incidents: list[dict[str, str]] = []
        next_containers: dict[str, dict[str, Any]] = {}
        next_metrics: dict[str, dict[str, Any]] = {}
        general_severity = "no_indication"

        names = sorted(self.managed_names())

        def collect_snapshot(name: str):
            try:
                return self.objective_snapshot(name), None
            except ToolError as exception:
                return None, exception

        with ThreadPoolExecutor(max_workers=self.docker_workers) as executor:
            snapshots = list(executor.map(collect_snapshot, names))

        for name, (snapshot, error) in zip(names, snapshots):
            found = error is None
            if error is not None:
                snapshot = {
                    "name": name,
                    "state": "not_found",
                    "running": False,
                    "expected_stopped": False,
                    "health": "unknown",
                    "oom_killed": False,
                    "restarts": 0,
                    "exit_code": None,
                    "state_error": str(error),
                    "started_at": None,
                    "finished_at": None,
                    "metrics_available": False,
                    "metrics_error": "",
                    "cpu_percent": 0.0,
                    "memory": {"usage": "0.0 B", "limit": "0.0 B", "percent": 0.0},
                    "network": {"received": "0.0 B", "sent": "0.0 B"},
                    "processes": 0,
                }

            previous = previous_containers.get(name)
            comparison = _compare_container_with_baseline(
                snapshot, previous, baseline_exists
            )
            evaluation = self.evaluate(snapshot, comparison)
            previous_metric_entry = previous_metrics.get(name)
            metric_entry = _update_sustained_metric_entry(
                snapshot, previous_metric_entry, generated_at
            )
            metric_evaluation = _evaluate_sustained_metrics(metric_entry)
            next_metrics[name] = metric_entry

            if bool(metric_entry.get("metrics_available", False)):
                summary["metrics_available"] += 1

            required_metric_samples = int(metric_entry.get("required_samples", 3) or 3)

            if (
                int(metric_entry.get("consecutive_high_cpu", 0) or 0)
                >= required_metric_samples
            ):
                summary["sustained_high_cpu"] += 1

            if (
                int(metric_entry.get("consecutive_high_memory", 0) or 0)
                >= required_metric_samples
            ):
                summary["sustained_high_memory"] += 1

            combined_findings = [
                *evaluation.get("findings", []),
                *metric_evaluation.get("findings", []),
            ]
            running = bool(snapshot["running"])
            stopped_expected = bool(snapshot["expected_stopped"])
            health = str(snapshot["health"])

            if not found:
                summary["not_found"] += 1

            elif running:
                summary["running"] += 1

            else:
                summary["stopped"] += 1

                if stopped_expected:
                    summary["expected_stopped"] += 1

                else:
                    summary["unexpectedly_stopped"] += 1

            if found and running:
                if health == "healthy":
                    summary["healthy"] += 1

                elif health == "unhealthy":
                    summary["unhealthy"] += 1

                else:
                    summary["no_healthcheck"] += 1

            elif found:
                summary["no_healthcheck"] += 1

            new_restarts = int(comparison.get("new_restarts", 0) or 0)
            summary["new_restarts"] += new_restarts

            if bool(comparison.get("new_oom", False)):
                summary["new_ooms"] += 1

            changes.extend(comparison.get("changes", []))
            severity = _maximum_severity(
                str(evaluation["severity"]), str(metric_evaluation["severity"])
            )
            general_severity = _maximum_severity(general_severity, severity)

            for finding in combined_findings:
                incidents.append(
                    {
                        "origin": name,
                        "severity": str(finding["severity"]),
                        "description": str(finding["finding"]),
                    }
                )

            containers.append(
                {
                    **snapshot,
                    "objective_severity": severity,
                    "objective_state_severity": evaluation["severity"],
                    "sustained_metrics_severity": metric_evaluation["severity"],
                    "objective_findings": combined_findings,
                    "sustained_metrics": metric_entry,
                    "comparison": comparison,
                }
            )
            next_containers[name] = _container_baseline_entry(snapshot)

        storage = self.disk_snapshot()
        storage_percent = float(storage.get("usage_percent", 0.0) or 0.0)
        storage_severity = "no_indication"

        if storage_percent >= 95:
            storage_severity = "critical"

        elif storage_percent >= 90:
            storage_severity = "high"

        elif storage_percent >= 80:
            storage_severity = "medium"

        if storage_severity != "no_indication":
            incidents.append(
                {
                    "origin": "storage",
                    "severity": storage_severity,
                    "description": f"The monitored storage is at {storage_percent:.1f} %.",
                }
            )

        general_severity = _maximum_severity(general_severity, storage_severity)
        return {
            "generated_at": generated_at,
            "overall_severity": general_severity,
            "summary": summary,
            "storage": storage,
            "containers": containers,
            "changes": changes,
            "objective_incidents": incidents,
            "next_baseline": {
                "version": self.baseline_version,
                "last_run": generated_at,
                "containers": next_containers,
                "metrics": next_metrics,
            },
        }

    def filter_events(
        self,
        container_name: str, events: list[dict[str, Any]], profiles: Any
    ) -> dict[str, Any]:
        """
        Apply the reusable log profile selected through the container label.

        Profile rules only suppress matching events; they never assign severity.
        """

        resolution = self.resolve_profile(container_name, profiles)
        warnings = list(resolution.get("warnings", []))
        profile = resolution.get("profile", {})

        if not isinstance(profile, dict):
            profile = {}

        raw_patterns = profile.get("ignore_patterns", [])

        if not isinstance(raw_patterns, list):
            raw_patterns = []

        compiled_patterns: list[re.Pattern[str]] = []

        for raw_pattern in raw_patterns:
            if not isinstance(raw_pattern, str) or not raw_pattern.strip():
                warnings.append("An invalid log profile pattern was ignored.")
                continue

            try:
                compiled_patterns.append(re.compile(raw_pattern))

            except re.error as exception:
                warnings.append(
                    (
                        "An invalid regular expression from the selected "
                        f"log profile was ignored: {exception}"
                    )
                )

        if not compiled_patterns:
            return {
                "events": list(events),
                "omitted": 0,
                "warnings": warnings,
                "requested_profile": resolution.get("requested", "generic"),
                "selected_profile": resolution.get("selected", "generic"),
            }

        filtered_events: list[dict[str, Any]] = []
        omitted = 0

        for event in events:
            content = event.get("content", "")

            if not isinstance(content, str):
                content = str(content)

            message = event.get("message", "")

            if not isinstance(message, str):
                message = str(message)

            searchable_text = content if content else message

            if any(pattern.search(searchable_text) for pattern in compiled_patterns):
                omitted += 1
                continue

            filtered_events.append(event)

        return {
            "events": filtered_events,
            "omitted": omitted,
            "warnings": warnings,
            "requested_profile": resolution.get("requested", "generic"),
            "selected_profile": resolution.get("selected", "generic"),
        }

    def deduplicate(
        self,
        container_name: str, events: list[dict[str, Any]], known_fingerprints: Any
    ) -> dict[str, Any]:
        """Remove already delivered events and update the bounded fingerprint history without changing event classification or objective severity."""

        normalized_name = container_name.strip().lstrip("/")
        (normalized_store, normalization_warnings) = self.normalize_fingerprints(
            {normalized_name: known_fingerprints}
        )
        previous_fingerprints = normalized_store.get(normalized_name, [])
        seen = set(previous_fingerprints)
        filtered_events: list[dict[str, Any]] = []
        new_fingerprints: list[str] = []
        duplicates_omitted = 0
        events_without_fingerprint = 0

        for event in events:
            fingerprint = _event_fingerprint(normalized_name, event)

            if fingerprint is None:
                events_without_fingerprint += 1
                filtered_events.append(event)
                continue

            if fingerprint in seen:
                duplicates_omitted += 1
                continue

            seen.add(fingerprint)
            new_fingerprints.append(fingerprint)
            filtered_events.append(event)

        combined_fingerprints = previous_fingerprints + new_fingerprints
        (updated_store, limit_warnings) = self.normalize_fingerprints(
            {normalized_name: combined_fingerprints}
        )
        updated_fingerprints = updated_store.get(normalized_name, [])
        return {
            "events": filtered_events,
            "duplicates_skipped": duplicates_omitted,
            "events_without_fingerprint": events_without_fingerprint,
            "new_fingerprints": new_fingerprints,
            "updated_fingerprints": updated_fingerprints,
            "warnings": (normalization_warnings + limit_warnings),
        }

    def collect_logs(
        self,
        baseline: dict[str, Any], baseline_exists: bool
    ) -> dict[str, Any]:
        """Collect events produced after the previous persisted run. Deduplicate them without interpreting events, calculating severity, or invoking a model."""

        warnings: list[str] = []
        (fingerprint_store, fingerprint_warnings) = self.normalize_fingerprints(
            baseline.get("event_fingerprints", {})
        )
        warnings.extend(fingerprint_warnings)
        (profiles, profile_load_warnings) = self.load_profiles()
        warnings.extend(profile_load_warnings)
        result: dict[str, Any] = {
            "active": False,
            "reason": "",
            "since": None,
            "warnings": warnings,
            "updated_event_fingerprints": fingerprint_store,
            "summary": {
                "containers_queried": 0,
                "containers_with_logs": 0,
                "containers_with_detected_events": 0,
                "containers_with_events": 0,
                "containers_with_relevant_events": 0,
                "new_lines": 0,
                "events_detected": 0,
                "new_events": 0,
                "events_evaluated": 0,
                "events_omitted_profile": 0,
                "relevant_events": 0,
                "routine_events": 0,
                "duplicates_skipped": 0,
                "events_without_fingerprint": 0,
                "multiline_events": 0,
                "truncated_queries": 0,
                "query_errors": 0,
            },
            "containers": [],
        }

        if not baseline_exists:
            result["reason"] = "initial_baseline"
            return result

        since, conversion_warnings = self.baseline_to_since(baseline.get("last_run"))
        warnings.extend(conversion_warnings)

        if since is None:
            result["reason"] = "timestamp_unavailable"
            return result

        result["active"] = True
        result["reason"] = "differential_query"
        progress = baseline.get("log_progress")
        if self.get_log_page is not None:
            assert self.managed_log_ids is not None
            managed = self.managed_log_ids()
            if progress is None:
                # Snapshot intervals do not overlap.
                since, _ = self.baseline_to_since(baseline.get("last_run"), overlap_seconds=0)
                progress = {"since": since, "until": int(datetime.now(timezone.utc).timestamp()), "containers": {}}
            if (not isinstance(progress, dict) or not isinstance(progress.get("since"), int)
                    or isinstance(progress.get("since"), bool) or not isinstance(progress.get("until"), int)
                    or isinstance(progress.get("until"), bool) or progress["since"] < 0
                    or progress["until"] < progress["since"]
                    or not isinstance(progress.get("containers"), dict)
                    or not isinstance(progress.get("membership_fixed", False), bool)):
                raise ToolError("The stored log progress is invalid; the baseline was not advanced.")
            cursors = dict(progress["containers"])
            if not progress.get("membership_fixed"):
                # Older checkpoints only recorded successful cursors. Keep
                # those identities and seed the rest from current discovery.
                cursors = {**{name: {"container_id": identifier} for name, identifier in managed.items()},
                           **cursors}
            progress = {"since": progress["since"], "until": progress["until"],
                        "containers": cursors, "membership_fixed": True}
            since = progress["since"]
            result["log_progress"] = progress
            result["summary"]["pending_pages"] = 0
            names = cursors
        else:
            names = self.managed_names()
        result["since"] = since
        summary = result["summary"]
        containers = result["containers"]

        for name in sorted(names):
            summary["containers_queried"] += 1

            try:
                if self.get_log_page:
                    cursor = cursors[name]
                    if not isinstance(cursor, dict) or not isinstance(cursor.get("container_id"), str):
                        raise ToolError("The stored log cursor is invalid.")
                    if name not in managed:
                        raise ToolError("A container with pending log progress is no longer managed.")
                    if managed[name] != cursor["container_id"]:
                        raise ToolError("The container changed while its log interval was being read.")
                    data = self.get_log_page(name, since, progress["until"], cursor)
                    next_cursor = data.get("cursor")
                    cursors[name] = next_cursor
                    if not next_cursor.get("complete"):
                        summary["pending_pages"] += 1
                else:
                    data = self.get_logs(name, self.max_log_lines, since=since)

            except ToolError as exception:
                summary["query_errors"] += 1
                containers.append(
                    {
                        "name": name,
                        "query_successful": False,
                        "error": str(exception),
                        "since": since,
                        "new_lines": 0,
                        "events_detected": 0,
                        "new_events": 0,
                        "events_evaluated": 0,
                        "events_omitted_profile": 0,
                        "relevant_events": 0,
                        "routine_events": 0,
                        "duplicates_skipped": 0,
                        "events_without_fingerprint": 0,
                        "multiline_events": 0,
                        "truncated": False,
                        "relevant_event_details": [],
                        "events": [],
                    }
                )
                continue

            logs = str(data.get("logs", ""))
            new_lines = sum(1 for line in logs.splitlines() if line.strip())
            truncated = bool(data.get("truncated", False))
            detected_events = _group_log_lines_into_events(logs)
            if self.get_log_page:
                # The verified byte cursor has no overlap. Equal events can be
                # distinct occurrences and must not be removed by fingerprints.
                deduplication = {
                    "events": detected_events, "duplicates_skipped": 0,
                    "events_without_fingerprint": 0,
                    "updated_fingerprints": fingerprint_store.get(name, []),
                }
            else:
                deduplication = self.deduplicate(
                    name, detected_events, fingerprint_store.get(name, [])
                )
            events = deduplication["events"]
            detected_count = len(detected_events)
            new_events = len(events)
            duplicates_omitted = int(deduplication.get("duplicates_skipped", 0))
            events_without_fingerprint = int(
                deduplication.get("events_without_fingerprint", 0)
            )
            updated_fingerprints = deduplication.get("updated_fingerprints", [])

            if updated_fingerprints:
                fingerprint_store[name] = updated_fingerprints

            else:
                fingerprint_store.pop(name, None)

            profile_filter = self.filter_events(name, events, profiles)
            evaluated_events = profile_filter.get("events", [])

            if not isinstance(evaluated_events, list):
                evaluated_events = list(events)

            profile_omitted = max(0, int(profile_filter.get("omitted", 0) or 0))
            profile_event_warnings = profile_filter.get("warnings", [])

            if isinstance(profile_event_warnings, list):
                warnings.extend(str(warning) for warning in profile_event_warnings)

            relevant_events: list[dict[str, Any]] = []

            for event in evaluated_events:
                relevance = _classify_event_relevance(event)

                if not bool(relevance.get("relevant", False)):
                    continue

                relevant_event = dict(event)
                relevant_event["relevance"] = relevance
                relevant_events.append(relevant_event)

            evaluated_count = len(evaluated_events)
            relevant_count = len(relevant_events)
            routine_count = max(0, (evaluated_count - relevant_count))
            multiline_events = sum(
                1 for event in events if bool(event.get("multiline", False))
            )

            if new_lines > 0:
                summary["containers_with_logs"] += 1

            if detected_count > 0:
                summary["containers_with_detected_events"] += 1

            if new_events > 0:
                summary["containers_with_events"] += 1

            if relevant_count > 0:
                summary["containers_with_relevant_events"] += 1

            summary["new_lines"] += new_lines
            summary["events_detected"] += detected_count
            summary["new_events"] += new_events
            summary["events_evaluated"] += evaluated_count
            summary["events_omitted_profile"] += profile_omitted
            summary["relevant_events"] += relevant_count
            summary["routine_events"] += routine_count
            summary["duplicates_skipped"] += duplicates_omitted
            summary["events_without_fingerprint"] += events_without_fingerprint
            summary["multiline_events"] += multiline_events

            if truncated:
                summary["truncated_queries"] += 1

            containers.append(
                {
                    "name": name,
                    "query_successful": True,
                    "error": "",
                    "since": since,
                    "new_lines": new_lines,
                    "events_detected": detected_count,
                    "new_events": new_events,
                    "events_evaluated": evaluated_count,
                    "events_omitted_profile": profile_omitted,
                    "relevant_events": relevant_count,
                    "routine_events": routine_count,
                    "duplicates_skipped": duplicates_omitted,
                    "events_without_fingerprint": events_without_fingerprint,
                    "multiline_events": multiline_events,
                    "truncated": truncated,
                    "relevant_event_details": relevant_events,
                    "events": events,
                }
            )

        return result
