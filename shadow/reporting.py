"""Human-readable rendering of server reports."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from shadow.events import _redact_sensitive_text


def _severity_label(severity: str) -> str:
    """Convert an internal severity value to a human-readable report label."""

    labels = {
        "no_indication": "NO INDICATION",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
        "critical": "CRITICAL",
    }
    return labels.get(severity, severity.upper())


def _report_count_phrase(value: Any, singular: str, plural: str) -> str:
    """Format a count with the appropriate singular or plural noun."""

    try:
        count = max(0, int(value or 0))

    except (TypeError, ValueError):
        count = 0

    word = singular if count == 1 else plural
    return f"{count} {word}"


def _report_status_label(severity: Any) -> str:
    """Convert objective severity to a compact report status label."""

    labels = {
        "no_indication": "OPERATIONAL",
        "low": "LOW ATTENTION",
        "medium": "ATTENTION",
        "high": "ISSUE",
        "critical": "CRITICAL",
    }
    return labels.get(str(severity), "UNKNOWN")


def _report_markdown(report: dict[str, Any], server_name: str) -> str:
    """Build a compact Telegram-oriented summary from the objective cycle."""

    summary = report.get("summary", {})

    if not isinstance(summary, dict):
        summary = {}

    storage = report.get("storage", {})

    if not isinstance(storage, dict):
        storage = {}

    log_activity = report.get("log_activity", {})

    if not isinstance(log_activity, dict):
        log_activity = {}

    log_summary = log_activity.get("summary", {})

    if not isinstance(log_summary, dict):
        log_summary = {}

    interpretation = report.get("event_interpretation", {})

    if not isinstance(interpretation, dict):
        interpretation = {}

    baseline = report.get("baseline", {})

    if not isinstance(baseline, dict):
        baseline = {}

    changes = report.get("changes", [])

    if not isinstance(changes, list):
        changes = []

    incidents = report.get("incidents", [])

    if not isinstance(incidents, list):
        incidents = []

    server_name = (
        str(report.get("server", server_name))
        .replace("-", " ")
        .title()
        .replace(" Of ", " of ")
    )

    try:
        restart_count = max(0, int(summary.get("new_restarts", 0) or 0))

    except (TypeError, ValueError):
        restart_count = 0

    try:
        relevant_events = max(0, int(log_summary.get("relevant_events", 0) or 0))

    except (TypeError, ValueError):
        relevant_events = 0

    try:
        storage_percent = float(storage.get("usage_percent", 0.0) or 0.0)

    except (TypeError, ValueError):
        storage_percent = 0.0

    storage_text = f"{storage_percent:.1f}".replace(".", ",")
    lines = [
        server_name,
        "",
        "Status:",
        _report_status_label(report.get("overall_severity", "no_indication")),
        "",
        "Changes:",
    ]
    objective_changes = []

    for change in changes:
        if not isinstance(change, dict):
            continue

        if str(change.get("type", "")) == "restarts":
            continue

        description = str(change.get("description", "")).strip()

        if description:
            objective_changes.append(description)

    if objective_changes:
        for description in objective_changes[:4]:
            lines.append(f"• {description}")

        remaining_changes = len(objective_changes) - 4

        if remaining_changes > 0:
            lines.append(
                "• "
                + _report_count_phrase(
                    remaining_changes, "additional change", "additional changes"
                )
            )

    else:
        lines.append("• No state changes")

    lines.append(
        "• " + _report_count_phrase(restart_count, "new restart", "new restarts")
    )

    if relevant_events > 0:
        lines.append(
            "• "
            + _report_count_phrase(
                relevant_events, "new relevant event", "new relevant events"
            )
        )

    else:
        lines.append("• No new relevant events")

    lines.extend(
        [
            "",
            "Services:",
            (
                "• "
                + _report_count_phrase(summary.get("running", 0), "active", "active")
            ),
            (
                "• "
                + _report_count_phrase(
                    summary.get("expected_stopped", 0),
                    "intentionally stopped",
                    "intentionally stopped",
                )
            ),
            (
                "• "
                + _report_count_phrase(
                    summary.get("unhealthy", 0), "unhealthy", "unhealthy"
                )
            ),
        ]
    )
    unexpected_stopped = int(summary.get("unexpectedly_stopped", 0) or 0)

    if unexpected_stopped > 0:
        lines.append(
            "• "
            + _report_count_phrase(
                unexpected_stopped, "unexpectedly stopped", "unexpectedly stopped"
            )
        )

    missing = int(summary.get("not_found", 0) or 0)

    if missing > 0:
        lines.append(
            "• "
            + _report_count_phrase(missing, "service not found", "services not found")
        )

    cpu_sustained = int(summary.get("sustained_high_cpu", 0) or 0)
    memory_sustained = int(summary.get("sustained_high_memory", 0) or 0)
    lines.extend(["", "Resources:", (f"• Disk: {storage_text} %")])

    if cpu_sustained == 0 and memory_sustained == 0:
        lines.append("• No sustained high CPU or memory usage")

    else:
        if cpu_sustained > 0:
            lines.append(
                "• High CPU "
                "usage sustained in "
                + _report_count_phrase(cpu_sustained, "service", "services")
            )

        if memory_sustained > 0:
            lines.append(
                "• High memory "
                "usage sustained in "
                + _report_count_phrase(memory_sustained, "service", "services")
            )

    if incidents:
        lines.extend(["", "Incidents:"])

        for incident in incidents[:5]:
            if not isinstance(incident, dict):
                continue

            lines.append(
                "• "
                f"[{_severity_label(incident.get('severity', 'no_indication'))}] "
                f"{incident.get('origin', 'server')}: "
                f"{incident.get('description', '')}"
            )

    if bool(interpretation.get("executed", False)):
        lines.extend(["", "Interpretation:"])
        model_summary = str(interpretation.get("summary", "")).strip()

        if model_summary:
            lines.append(f"• {model_summary}")

        findings = interpretation.get("findings", [])

        if isinstance(findings, list):
            for finding in findings[:3]:
                if not isinstance(finding, dict):
                    continue

                lines.append(
                    "• "
                    f"{finding.get('container', 'service')}: "
                    f"{finding.get('interpretation', finding.get('title', ''))}"
                )

    elif interpretation.get("reason") == "model_error":
        lines.extend(
            [
                "",
                "Interpretation:",
                ("• Unavailable; the relevant events remain recorded."),
            ]
        )

    if not bool(baseline.get("saved", False)):
        lines.extend(
            [
                "",
                "Note:",
                (
                    "• The baseline was not "
                    "advanced; the next "
                    "report will retry "
                    "collecting this interval."
                ),
            ]
        )

    return "\n".join(lines)


class ReportService:
    """Build the public report from a completed observation cycle."""

    def __init__(self, *, server_name: str, max_log_lines: int, run_cycle, render_report):
        self.server_name = server_name
        self.max_log_lines = max_log_lines
        self.run_cycle = run_cycle
        self.render_report = render_report

    def generate(self, lines_per_container: int = 50) -> dict[str, Any]:
        """Generate a compact differential read-only server report. The lines_per_container parameter is retained for compatibility with existing clients; the current flow uses the persistent baseline instead of a fixed historical log queue."""

        if lines_per_container < 1:
            raise ToolError("The number of log lines must be greater than zero.")

        if lines_per_container > self.max_log_lines:
            raise ToolError(f"The maximum allowed number of log lines is {self.max_log_lines}.")

        cycle = self.run_cycle()
        severity_order = {
            "no_indication": 0,
            "low": 1,
            "medium": 2,
            "high": 3,
            "critical": 4,
        }
        raw_incidents = cycle.get("objective_incidents", [])

        if not isinstance(raw_incidents, list):
            raw_incidents = []

        incidents = [incident for incident in raw_incidents if isinstance(incident, dict)]
        incidents.sort(
            key=lambda incident: severity_order.get(
                str(incident.get("severity", "no_indication")), 0
            ),
            reverse=True,
        )
        raw_changes = cycle.get("changes", [])

        if not isinstance(raw_changes, list):
            raw_changes = []

        changes = [change for change in raw_changes if isinstance(change, dict)]
        raw_log_activity = cycle.get("log_activity", {})

        if not isinstance(raw_log_activity, dict):
            raw_log_activity = {}

        log_summary = raw_log_activity.get("summary", {})

        if not isinstance(log_summary, dict):
            log_summary = {}

        compact_log_activity = {
            "active": bool(raw_log_activity.get("active", False)),
            "reason": raw_log_activity.get("reason"),
            "since": raw_log_activity.get("since"),
            "summary": log_summary,
            "warnings": raw_log_activity.get("warnings", []),
            "containers": [],
        }
        for container in raw_log_activity.get("containers", []):
            if not isinstance(container, dict):
                continue
            item = {key: value for key, value in container.items()
                    if key not in {"events", "relevant_event_details"}}
            item["events"] = [
                {key: _redact_sensitive_text(value) if key in {"message", "content"} and isinstance(value, str) else value
                 for key, value in event.items()}
                for event in container.get("events", []) if isinstance(event, dict)
            ]
            compact_log_activity["containers"].append(item)
        interpretation = cycle.get("event_interpretation", {})

        if not isinstance(interpretation, dict):
            interpretation = {}

        baseline = cycle.get("baseline", {})

        if not isinstance(baseline, dict):
            baseline = {}

        report: dict[str, Any] = {
            "server": self.server_name,
            "generated_at": cycle.get("generated_at"),
            "mode": "read_only_differential",
            "uses_model": bool(interpretation.get("executed", False)),
            "overall_severity": cycle.get("overall_severity", "no_indication"),
            "summary": cycle.get("summary", {}),
            "storage": cycle.get("storage", {}),
            "changes": changes,
            "incidents": incidents,
            "log_activity": compact_log_activity,
            "event_interpretation": interpretation,
            "baseline": baseline,
            "compatibility": {
                "lines_per_container": lines_per_container,
                "parameter_used": False,
                "reason": (
                    "The report uses differential logs from the persistent baseline."
                ),
            },
        }
        report["markdown"] = self.render_report(report, self.server_name)
        return report
