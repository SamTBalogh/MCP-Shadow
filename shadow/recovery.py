"""Explicit, auditable closure of a log interval with lost coverage."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from mcp.server.fastmcp.exceptions import ToolError
from shadow.events import _redact_sensitive_text


def interval_id(progress):
    if progress is None:
        return None
    if (not isinstance(progress, dict) or not isinstance(progress.get("containers"), dict)
            or any(not isinstance(cursor, dict) for cursor in progress["containers"].values())
            or type(progress.get("since")) is not int or type(progress.get("until")) is not int
            or progress["since"] < 0 or progress["until"] < progress["since"]):
        raise ToolError("Invalid log interval; recovery was not performed.")
    identity = {"since": progress["since"], "until": progress["until"],
                "containers": {name: cursor.get("container_id") for name, cursor in progress["containers"].items()}}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def close_interval(store, identifier, reason):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ToolError("A recovery reason between 1 and 500 characters is required.")
    with store.cycle_lock():
        baseline, _, _ = store.load()
        gaps = baseline.get("log_gaps", [])
        for gap in gaps:
            if gap["interval_id"] == identifier:
                return {"closed": True, "already_closed": True, **public_gap(gap)}
        if baseline.get("delivery") is not None:
            raise ToolError("Process and acknowledge the pending report before closing its interval.")
        progress = baseline.get("log_progress")
        if not identifier or progress is None or identifier != interval_id(progress):
            raise ToolError("The interval_id does not match the current incomplete interval.")
        if len(gaps) >= 128:
            raise ToolError("Recovery history is full; archive state before closing another interval.")
        gap = {"interval_id": identifier, "since": progress["since"], "until": progress["until"],
               "closed_at": datetime.now(timezone.utc).isoformat(), "reason": _redact_sensitive_text(reason.strip()),
               "coverage_lost": True, "containers": sorted(progress["containers"]),
               "progress": deepcopy(progress)}
        baseline["log_gaps"] = [*gaps, gap]
        baseline["last_run"] = datetime.fromtimestamp(progress["until"], timezone.utc).isoformat()
        baseline["log_progress"] = None
        store.save(baseline)
        return {"closed": True, "already_closed": False, **public_gap(gap)}


def public_gap(gap):
    return {key: value for key, value in gap.items() if key != "progress"}
