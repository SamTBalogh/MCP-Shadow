"""Observation coverage and freshness, separate from service severity."""
from datetime import datetime, timezone


def observation_health(result, previous):
    previous = previous if isinstance(previous, dict) else {}
    observed_at = result.get("generated_at") or datetime.now(timezone.utc).isoformat()
    services = {}
    for item in result.get("containers", []):
        name = item["name"]
        valid = item.get("inspection_available", True)
        services[name] = {"last_valid_at": observed_at if valid else
                          previous.get("services", {}).get(name, {}).get("last_valid_at"),
                          "inspection_available": valid,
                          "metrics_available": item.get("metrics_available", False)}
    summary = result.get("summary", {})
    errors = {key: summary.get(key + "_errors", 0) for key in ("inventory", "inspection", "metrics")}
    errors["logs"] = result.get("log_activity", {}).get("summary", {}).get("query_errors", 0)
    complete = bool(services) and not any(errors.values())
    covered = sum(s["inspection_available"] for s in services.values())
    return {"last_attempt_at": observed_at,
            "last_complete_observation_at": observed_at if complete else previous.get("last_complete_observation_at"),
            "inspection_coverage_percent": round(100 * covered / len(services), 2) if services else None,
            "query_errors": errors, "services": services}


def monitor_status(baseline, now=None):
    now = now or datetime.now(timezone.utc)
    pending = baseline.get("delivery")
    source = pending.get("next_baseline", baseline) if isinstance(pending, dict) else baseline
    health = source.get("monitoring", {})
    def age(text):
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            seconds = (now - value).total_seconds()
            return round(seconds, 2) if seconds >= 0 else None
        except (ValueError, TypeError, AttributeError):
            return None
    return {**health,
            "last_complete_observation_age_seconds": age(health.get("last_complete_observation_at")),
            "last_attempt_age_seconds": age(health.get("last_attempt_at")),
            "services": {name: {**item, "last_valid_age_seconds": age(item.get("last_valid_at"))}
                         for name, item in health.get("services", {}).items()},
            "pending_report_age_seconds": age(pending.get("created_at", pending.get("report", {}).get("generated_at")))
                if isinstance(pending, dict) else None,
            "closed_incomplete_intervals": len(baseline.get("log_gaps", []))}
