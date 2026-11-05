"""Coordinate an observation cycle with explicitly injected dependencies."""
from __future__ import annotations

from typing import Any, Callable
from datetime import datetime, timezone

from mcp.server.fastmcp.exceptions import ToolError


class ObservationService:
    def __init__(
        self,
        *,
        load_baseline: Callable,
        collect_objective_server_state: Callable,
        collect_new_container_logs: Callable,
        safe_interpret_relevant_events: Callable,
        normalize_event_fingerprint_store: Callable,
        save_baseline: Callable,
    ):
        self.load_baseline = load_baseline
        self.collect_objective_server_state = collect_objective_server_state
        self.collect_new_container_logs = collect_new_container_logs
        self.safe_interpret_relevant_events = safe_interpret_relevant_events
        self.normalize_event_fingerprint_store = normalize_event_fingerprint_store
        self.save_baseline = save_baseline

    def run(self) -> dict[str, Any]:
        """Collect and commit one observation while holding the report lock."""

        (baseline, baseline_warnings, baseline_exists) = self.load_baseline()
        previous_run = baseline.get("last_run")
        result = self.collect_objective_server_state(baseline, baseline_exists)
        next_baseline = result.get("next_baseline")

        if not isinstance(next_baseline, dict):
            raise ToolError("A valid baseline was not generated.")

        next_baseline["last_acknowledged_delivery"] = baseline.get("last_acknowledged_delivery")
        log_activity = self.collect_new_container_logs(baseline, baseline_exists)
        result["log_activity"] = log_activity
        result["event_interpretation"] = self.safe_interpret_relevant_events(log_activity)
        updated_fingerprints, fingerprint_warnings = (
            self.normalize_event_fingerprint_store(
                log_activity.get(
                    "updated_event_fingerprints", baseline.get("event_fingerprints", {})
                )
            )
        )
        baseline_warnings.extend(fingerprint_warnings)
        next_baseline["event_fingerprints"] = updated_fingerprints
        log_summary = log_activity.get("summary", {})

        if not isinstance(log_summary, dict):
            log_summary = {}

        try:
            log_errors = max(0, int(log_summary.get("query_errors", 0) or 0))

        except (TypeError, ValueError):
            log_errors = 0

        try:
            truncated_queries = max(
                0, int(log_summary.get("truncated_queries", 0) or 0)
            )
        except (TypeError, ValueError):
            truncated_queries = 0

        log_reason = str(log_activity.get("reason", ""))
        log_collection_active = bool(log_activity.get("active", False))
        progress = log_activity.get("log_progress")
        pending_pages = int(log_summary.get("pending_pages", 0) or 0)
        logs_complete = (not baseline_exists and log_reason == "initial_baseline") or (
            baseline_exists
            and log_collection_active
            and log_errors == 0
            and truncated_queries == 0
            and pending_pages == 0
        )
        baseline_saved = False
        progress_saved = False
        not_saved_reason = ""

        if logs_complete:
            if isinstance(progress, dict):
                next_baseline["last_run"] = datetime.fromtimestamp(
                    progress["until"], timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
            next_baseline["log_progress"] = None
            self.save_baseline(next_baseline)
            baseline_saved = True

        elif isinstance(progress, dict):
            # Keep the objective baseline unchanged, while atomically saving
            # delivered pages and fingerprints for the fixed log interval.
            checkpoint = dict(baseline)
            checkpoint["log_progress"] = progress
            checkpoint["event_fingerprints"] = updated_fingerprints
            self.save_baseline(checkpoint)
            progress_saved = True
            not_saved_reason = "The log interval is still being processed; the objective baseline was not advanced."

        elif baseline_exists and not log_collection_active:
            not_saved_reason = (
                "Differential "
                "log collection was unavailable: "
                f"{log_reason or 'unknown reason'}."
            )

        elif log_errors > 0:
            not_saved_reason = (
                "The baseline was not advanced "
                "because "
                f"{log_errors} query or queries failed "
                "for logs."
            )

        elif truncated_queries > 0:
            not_saved_reason = (
                f"The baseline was not advanced because {truncated_queries} "
                "log query or queries exceeded the configured limit."
            )

        else:
            not_saved_reason = (
                "The baseline was not advanced "
                "because reading the logs "
                "did not complete successfully."
            )

        result["baseline"] = {
            "existed": baseline_exists,
            "initialized": (not baseline_exists and baseline_saved),
            "saved": baseline_saved,
            "progress_saved": progress_saved,
            "previous_last_run": previous_run,
            "warnings": baseline_warnings,
            "complete_logs": logs_complete,
            "log_errors": log_errors,
            "truncated_queries": truncated_queries,
            "pending_pages": pending_pages,
            "reason_not_saved": not_saved_reason,
        }
        result.pop("next_baseline", None)
        return result
