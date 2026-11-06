"""Validate Docker measurements independently, without manufacturing zeroes."""
from datetime import datetime, timezone
import math
import os

from formatting import human_bytes


def parse_statistics(data=None, error="", observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    result = {"cpu_percent": None, "memory": {"usage": None, "limit": None, "percent": None},
              "network": {"received": None, "sent": None}, "processes": None,
              "metrics_observed_at": observed_at, "metric_status": {}}
    if not isinstance(data, dict) or not data:
        error = error or "Docker statistics have an unexpected format."
        data = {}
    if data.get("read"):
        try:
            sampled = datetime.fromisoformat(data["read"].replace("Z", "+00:00"))
            age = (datetime.fromisoformat(observed_at.replace("Z", "+00:00")) - sampled).total_seconds()
            if age < -5 or age > float(os.getenv("METRIC_MAX_AGE_SECONDS", "300")):
                error = "Docker statistics timestamp is stale or in the future."
        except (TypeError, ValueError, AttributeError):
            error = "Docker statistics timestamp is invalid."
    result["metrics_sampled_at"] = data.get("read")

    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid measurement")
        return value

    def cpu():
        current, previous = data["cpu_stats"], data["precpu_stats"]
        delta = number(current["cpu_usage"]["total_usage"]) - number(previous["cpu_usage"]["total_usage"])
        system = number(current["system_cpu_usage"]) - number(previous["system_cpu_usage"])
        cpus = number(current.get("online_cpus") or len(current["cpu_usage"].get("percpu_usage", [])))
        if delta < 0 or system <= 0 or cpus <= 0:
            raise ValueError("CPU deltas unavailable")
        return round(delta / system * cpus * 100, 2)

    def memory():
        raw = data["memory_stats"]
        usage, limit = number(raw["usage"]), number(raw["limit"])
        details = raw.get("stats", {})
        inactive = number(details.get("inactive_file", details.get("total_inactive_file", 0)))
        if limit <= 0:
            raise ValueError("Memory limit unavailable")
        working = max(0, usage - inactive)
        return {"usage": human_bytes(working), "limit": human_bytes(limit), "percent": round(working / limit * 100, 2)}

    def network():
        raw = data["networks"]
        if not isinstance(raw, dict):
            raise ValueError("Invalid networks")
        return {"received": human_bytes(sum(number(n["rx_bytes"]) for n in raw.values())),
                "sent": human_bytes(sum(number(n["tx_bytes"]) for n in raw.values()))}

    def processes():
        value = number(data["pids_stats"]["current"])
        if int(value) != value:
            raise ValueError("Invalid process count")
        return int(value)

    for field, read in (("cpu_percent", cpu), ("memory", memory), ("network", network), ("processes", processes)):
        problem = error
        if not problem:
            try:
                result[field] = read()
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                problem = f"Docker {field} measurement is missing or invalid."
        result["metric_status"][field] = {"available": not bool(problem), "error": problem,
                                          "observed_at": observed_at if not problem else None}
    result["metrics_available"] = all(s["available"] for s in result["metric_status"].values())
    result["metrics_error"] = "; ".join(dict.fromkeys(s["error"] for s in result["metric_status"].values() if s["error"]))
    return result
