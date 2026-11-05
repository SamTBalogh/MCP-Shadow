"""Log parsing, event classification and evidence handling."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _redact_sensitive_text(value: str) -> str:
    """Redact common credential patterns before sending text to the local model."""

    result = value
    substitutions = (
        (
            re.compile(
                r"(?i)\bBearer\s+"
                r"[A-Za-z0-9._~+/=-]+"
            ),
            "Bearer [REDACTED]",
        ),
        (
            re.compile(
                r"\beyJ"
                r"[A-Za-z0-9_-]{10,}"
                r"\."
                r"[A-Za-z0-9_-]{10,}"
                r"\."
                r"[A-Za-z0-9_-]{10,}"
                r"\b"
            ),
            "[JWT_REDACTED]",
        ),
    )

    for pattern, replacement in substitutions:
        result = pattern.sub(replacement, result)

    assignment = re.compile(
        r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|authorization)\b"
        r"([\"']?\s*[:=]\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    )

    def redact_assignment(match):
        value = match.group(3)
        quote = value[0] if value[:1] in ("\"", "'") and value[-1:] == value[:1] else ""
        return f"{match.group(1)}{match.group(2)}{quote}[REDACTED]{quote}"

    result = assignment.sub(redact_assignment, result)

    return result


def _filter_expected_shutdown_noise(value: str) -> tuple[str, int]:
    """Remove npm log blocks produced by an expected SIGTERM shutdown from objective analysis. Raw logs remain available through the log tool."""

    lines = value.splitlines()
    filtered: list[str] = []
    omitted = 0
    position = 0

    while position < len(lines):
        line = lines[position]

        if not re.search(r"(?i)\bnpm error\b", line):
            filtered.append(line)
            position += 1
            continue

        block: list[str] = []

        while position < len(lines) and re.search(
            r"(?i)\bnpm error\b", lines[position]
        ):
            block.append(lines[position])
            position += 1

        block_text = "\n".join(block)
        expected_sigterm = bool(
            re.search(
                r"(?i)\b"
                r"signal\s+SIGTERM"
                r"\b",
                block_text,
            )
        )

        if expected_sigterm:
            omitted += len(block)
            continue

        for block_line in block:
            only_log_reference = bool(
                re.search(
                    r"(?i)"
                    r"\bnpm error "
                    r"A complete log of "
                    r"this run can be "
                    r"found in:",
                    block_line,
                )
            )

            if only_log_reference:
                omitted += 1

            else:
                filtered.append(block_line)

    return ("\n".join(filtered), omitted)


def _normalized_evidence(value: str) -> str:
    """Normalize text for model-evidence validation."""

    return " ".join(value.lower().split())


def _maximum_severity(*values: str) -> str:
    """Return the highest severity from the supplied values."""

    ranking = {"no_indication": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    return max(values, key=lambda value: ranking.get(value, 0))


def _split_timestamped_log_line(line: str) -> tuple[str | None, str]:
    """Split a Docker RFC3339 timestamp from the log message."""

    match = re.match(
        (
            r"^(?P<timestamp>"
            r"\d{4}-\d{2}-\d{2}"
            r"T"
            r"\d{2}:\d{2}:\d{2}"
            r"(?:\.\d+)?"
            r"(?:Z|[+-]\d{2}:\d{2})"
            r")"
            r"\s"
            r"(?P<message>.*)$"
        ),
        line,
    )

    if match is None:
        return (None, line.rstrip("\r"))

    return (match.group("timestamp"), match.group("message").rstrip("\r"))


def _is_traceback_header(message: str) -> bool:
    """Return whether a message is a Python traceback header."""

    stripped = message.strip()
    return stripped == ("Traceback (most recent call last):")


def _is_traceback_chain_marker(message: str) -> bool:
    """Return whether a message is a structural Python exception-chain marker."""

    stripped = message.strip()
    return stripped in {
        ("During handling of the above exception, another exception occurred:"),
        ("The above exception was the direct cause of the following exception:"),
    }


def _is_structural_log_continuation(message: str) -> bool:
    """Detect lines that structurally continue the previous log event. This function does not assign severity or interpret meaning."""

    if not message.strip():
        return True

    if message[:1].isspace():
        return True

    stripped = message.lstrip()

    if (
        stripped.startswith("at ")
        or stripped.startswith("Caused by:")
        or stripped.startswith("Suppressed:")
    ):
        return True

    if re.match(r"^\.\.\.\s+\d+\s+more$", stripped):
        return True

    if _is_traceback_chain_marker(stripped):
        return True

    return False


def _group_log_lines_into_events(logs: str) -> list[dict[str, Any]]:
    """Group consecutive physical log lines into multiline events. This function does not classify events or calculate severity."""

    events: list[dict[str, Any]] = []
    current_messages: list[str] = []
    current_start: str | None = None
    current_end: str | None = None
    traceback_open = False
    traceback_chain_pending = False

    def flush_current() -> None:
        if not current_messages:
            return

        content = "\n".join(current_messages).rstrip()

        if not content:
            return

        first_message = next(
            (item.strip() for item in current_messages if item.strip()), ""
        )
        events.append(
            {
                "start_timestamp": current_start,
                "end_timestamp": current_end,
                "physical_lines": len(current_messages),
                "multiline": (len(current_messages) > 1),
                "message": first_message,
                "content": content,
            }
        )

    for raw_line in logs.splitlines():
        (timestamp, message) = _split_timestamped_log_line(raw_line)

        if not message.strip():
            if current_messages:
                current_messages.append(message)

                if timestamp is not None:
                    current_end = timestamp

            continue

        trace_header = _is_traceback_header(message)
        structural_continuation = _is_structural_log_continuation(message)

        if not current_messages:
            current_messages = [message]
            current_start = timestamp
            current_end = timestamp
            traceback_open = trace_header
            traceback_chain_pending = False
            continue

        if trace_header and (traceback_open or traceback_chain_pending):
            current_messages.append(message)

            if timestamp is not None:
                current_end = timestamp

            traceback_open = True
            traceback_chain_pending = False
            continue

        if structural_continuation:
            current_messages.append(message)

            if timestamp is not None:
                current_end = timestamp

            if _is_traceback_chain_marker(message):
                traceback_chain_pending = True

            continue

        if traceback_open:
            current_messages.append(message)

            if timestamp is not None:
                current_end = timestamp

            traceback_open = False
            traceback_chain_pending = False
            continue

        flush_current()
        current_messages = [message]
        current_start = timestamp
        current_end = timestamp
        traceback_open = trace_header
        traceback_chain_pending = False

    flush_current()
    return events


def _normalize_explicit_log_level(value: Any) -> str | None:
    """Normalize an explicitly identified logger level token."""

    if not isinstance(value, str):
        return None

    token = value.strip().lower()
    aliases = {
        "trace": "trace",
        "trc": "trace",
        "debug": "debug",
        "dbg": "debug",
        "info": "info",
        "inf": "info",
        "notice": "info",
        "warn": "warning",
        "warning": "warning",
        "wrn": "warning",
        "error": "error",
        "err": "error",
        "fatal": "fatal",
        "ftl": "fatal",
        "critical": "fatal",
        "crit": "fatal",
        "panic": "fatal",
        "alert": "fatal",
        "emerg": "fatal",
    }
    return aliases.get(token)


def _extract_explicit_log_level(event: dict[str, Any]) -> str | None:
    """Extract a log level only when it uses logger-specific syntax, avoiding free-text keyword interpretation."""

    candidates: list[str] = []

    for key in ("message", "content"):
        value = event.get(key)

        if not isinstance(value, str):
            continue

        first_line = value.splitlines()[0] if value.splitlines() else ""

        if first_line:
            candidates.append(first_line)

    level_tokens = (
        r"trace|trc|"
        r"debug|dbg|"
        r"info|inf|notice|"
        r"warn|warning|wrn|"
        r"error|err|"
        r"fatal|ftl|"
        r"critical|crit|"
        r"panic|alert|emerg"
    )
    patterns = (
        (
            r"(?i)"
            r"(?:^|\s)"
            r"\[(" + level_tokens + r")\]"
            r"(?=\s|:|$)"
        ),
        (
            r"(?i)"
            r"^\s*"
            r"(?:"
            r"\[[^\]\r\n]{1,64}\]"
            r"\s*"
            r")*"
            r"(" + level_tokens + r")"
            r"\s*:"
        ),
        (
            r"(?i)"
            r"^\s*"
            r"(?:"
            r"\[[^\r\n\]]{1,64}\]"
            r"\s*"
            r")+"
            r"(" + level_tokens + r")"
            r"(?=\s|:|$)"
        ),
        (
            r"(?i)"
            r"(?:^|[\s,{])"
            r"[\"']?"
            r"(?:level|lvl|severity)"
            r"[\"']?"
            r"\s*(?:=|:)\s*"
            r"[\"']?"
            r"(" + level_tokens + r")"
            r"\b"
        ),
    )
    ansi_pattern = (
        r"\x1b"
        r"\[[0-?]*"
        r"[ -/]*"
        r"[@-~]"
    )

    for candidate in candidates:
        cleaned = re.sub(ansi_pattern, "", candidate)

        for pattern in patterns:
            match = re.search(pattern, cleaned)

            if match is None:
                continue

            return _normalize_explicit_log_level(match.group(1))

    return None


def _event_has_structural_exception(event: dict[str, Any]) -> bool:
    """Detect tracebacks and exceptions from multiline structure rather than isolated keywords."""

    if not bool(event.get("multiline", False)):
        return False

    content = event.get("content", "")

    if not isinstance(content, str):
        return False

    lines = content.splitlines()

    if any(
        (_is_traceback_header(line) or _is_traceback_chain_marker(line))
        for line in lines
    ):
        return True

    if any(re.search(r"^\s*at\s+\S+", line) for line in lines):
        return True

    if any(
        re.search(
            (
                r"^\s*"
                r"(?:Caused by:\s*)?"
                r"[A-Za-z_$]"
                r"[\w.$]*"
                r"(?:Exception|Error)"
                r"(?::|$)"
            ),
            line,
        )
        for line in lines
    ):
        return True

    if any(re.search(r"(?i)^\s*panic\s*:", line) for line in lines):
        return True

    return False


def _classify_event_relevance(event: dict[str, Any]) -> dict[str, Any]:
    """Decide whether an event belongs in the automatic report. This function does not calculate or modify server severity."""

    explicit_level = _extract_explicit_log_level(event)

    if _event_has_structural_exception(event):
        return {
            "relevant": True,
            "reason": "structural_trace",
            "explicit_level": explicit_level,
        }

    if explicit_level in {"warning", "error", "fatal"}:
        return {
            "relevant": True,
            "reason": "explicit_level",
            "explicit_level": explicit_level,
        }

    if explicit_level is not None:
        return {
            "relevant": False,
            "reason": "irrelevant_explicit_level",
            "explicit_level": explicit_level,
        }

    return {"relevant": False, "reason": "no_generic_signal", "explicit_level": None}


def _compact_model_text(value: Any, limit: int) -> tuple[str, bool]:
    """Bound text for model context while preserving its beginning and end."""

    if value is None:
        text = ""

    elif isinstance(value, str):
        text = value

    else:
        text = str(value)

    text = text.strip()
    safe_limit = max(1, int(limit))

    if len(text) <= safe_limit:
        return (text, False)

    marker = "\n...[content truncated]...\n"
    available = safe_limit - len(marker)

    if available <= 2:
        return (text[:safe_limit], True)

    head_length = max(1, (available * 2 // 3))
    tail_length = max(1, (available - head_length))
    return ((text[:head_length] + marker + text[-tail_length:]), True)


def _event_fingerprint(container_name: str, event: dict[str, Any]) -> str | None:
    """Build a stable fingerprint for a timestamped log event. Events without timestamps are not deduplicated."""

    normalized_name = container_name.strip().lstrip("/")

    if not normalized_name:
        return None

    timestamp_start = event.get("start_timestamp")

    if not isinstance(timestamp_start, str) or not timestamp_start.strip():
        return None

    timestamp_end = event.get("end_timestamp")

    if not isinstance(timestamp_end, str):
        timestamp_end = ""

    content = event.get("content", "")

    if not isinstance(content, str):
        content = str(content)

    payload = {
        "container": normalized_name,
        "start_timestamp": timestamp_start.strip(),
        "end_timestamp": timestamp_end.strip(),
        "content": content,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
