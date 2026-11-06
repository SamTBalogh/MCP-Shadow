"""Persistent observation baselines and event fingerprint validation."""

from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from shadow.persistence import atomic_json


class BaselineStore:
    """Read and atomically write one configured baseline file."""

    def __init__(self, path: Path, version: int, max_fingerprints: int):
        self.path = path
        self.version = version
        self.max_fingerprints = max_fingerprints

    @contextmanager
    def cycle_lock(self):
        """Serialize complete report cycles across threads and processes."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = self.path.with_name(f".{self.path.name}.lock").open("a")
        except OSError as exception:
            raise ToolError("The report lock could not be acquired.") from exception
        with lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except OSError as exception:
                raise ToolError("The report lock could not be acquired.") from exception
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def normalize_fingerprints(
        self, value: Any
    ) -> tuple[dict[str, list[str]], list[str]]:
        """Validate and bound the persistent event fingerprint store. Invalid or missing fields do not invalidate the rest of the baseline."""

        warnings: list[str] = []

        if value is None:
            return ({}, warnings)

        if not isinstance(value, dict):
            warnings.append(
                "event_fingerprints must be a JSON object; it will be ignored."
            )
            return ({}, warnings)

        normalized: dict[str, list[str]] = {}

        for raw_name, raw_fingerprints in value.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                warnings.append(
                    "An entry was ignored because its fingerprint name is invalid."
                )
                continue

            name = raw_name.strip().lstrip("/")

            if not isinstance(raw_fingerprints, list):
                warnings.append(
                    f"Fingerprints were ignored for {name}: expected a list."
                )
                continue

            valid_fingerprints: list[str] = []
            invalid_count = 0

            for raw_fingerprint in raw_fingerprints:
                if not isinstance(raw_fingerprint, str):
                    invalid_count += 1
                    continue

                fingerprint = raw_fingerprint.strip().lower()

                if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
                    invalid_count += 1
                    continue

                valid_fingerprints.append(fingerprint)

            if invalid_count > 0:
                warnings.append(
                    f"Ignored {invalid_count} fingerprint(s) invalid for {name}."
                )

            deduplicated_reversed: list[str] = []
            seen: set[str] = set()

            for fingerprint in reversed(valid_fingerprints):
                if fingerprint in seen:
                    continue

                seen.add(fingerprint)
                deduplicated_reversed.append(fingerprint)

            deduplicated = list(reversed(deduplicated_reversed))

            if len(deduplicated) > self.max_fingerprints:
                warnings.append(
                    "Limited the fingerprints "
                    f"persisted for {name} to "
                    f"{self.max_fingerprints}."
                )
                deduplicated = deduplicated[-self.max_fingerprints :]

            if deduplicated:
                normalized[name] = deduplicated

        return (normalized, warnings)

    def empty(self) -> dict[str, Any]:
        """Return an empty baseline compatible with the current schema version."""

        return {
            "version": self.version,
            "last_run": None,
            "containers": {},
            "metrics": {},
            "event_fingerprints": {},
            "log_progress": None,
            "delivery": None,
            "last_acknowledged_delivery": None,
            "log_gaps": [],
            "monitoring": {},
        }

    def load(self) -> tuple[dict[str, Any], list[str], bool]:
        """Load the baseline; damaged current state fails closed to preserve progress."""

        try:
            raw_data = json.loads(self.path.read_text(encoding="utf-8"))

        except FileNotFoundError:
            return (self.empty(), [], False)

        except (OSError, json.JSONDecodeError) as exception:
            raise ToolError("The persistent baseline could not be read; restore it before collecting another interval.") from exception

        if not isinstance(raw_data, dict):
            raise ToolError("The baseline root must be a JSON object; progress was not advanced.")

        if raw_data.get("version") != self.version:
            if raw_data.get("delivery") is not None or raw_data.get("log_progress") is not None:
                raise ToolError("The baseline version has pending progress and cannot be reset automatically.")
            return (self.empty(), ["The baseline version is not supported."], False)

        last_run = raw_data.get("last_run")

        if last_run is not None and not isinstance(last_run, str):
            raise ToolError("last_run must be a string or null; progress was not advanced.")

        containers = raw_data.get("containers", {})

        if not isinstance(containers, dict):
            raise ToolError("containers must be a JSON object; progress was not advanced.")

        metrics = raw_data.get("metrics", {})

        if not isinstance(metrics, dict):
            raise ToolError("metrics must be a JSON object; progress was not advanced.")

        (event_fingerprints, fingerprint_warnings) = self.normalize_fingerprints(
            raw_data.get("event_fingerprints", {})
        )
        gaps = raw_data.get("log_gaps", [])
        if not isinstance(gaps, list) or any(not isinstance(gap, dict) or not isinstance(gap.get("progress"), dict)
                                             or not isinstance(gap.get("interval_id"), str) for gap in gaps):
            raise ToolError("Invalid recovery history; snapshots must be preserved.")
        monitoring = raw_data.get("monitoring", {})
        if (not isinstance(monitoring, dict) or not isinstance(monitoring.get("services", {}), dict)
                or any(not isinstance(item, dict) for item in monitoring.get("services", {}).values())):
            raise ToolError("Invalid monitoring state; the baseline was not advanced.")
        return (
            {
                "version": self.version,
                "last_run": last_run,
                "containers": containers,
                "metrics": metrics,
                "event_fingerprints": event_fingerprints,
                "log_progress": raw_data.get("log_progress"),
                "delivery": raw_data.get("delivery"),
                "last_acknowledged_delivery": raw_data.get("last_acknowledged_delivery"),
                "log_gaps": gaps,
                "monitoring": monitoring,
            },
            fingerprint_warnings,
            True,
        )

    def save(self, baseline: dict[str, Any]) -> None:
        """Persist the baseline using an atomic replacement."""

        containers = baseline.get("containers", {})

        if not isinstance(containers, dict):
            containers = {}

        metrics = baseline.get("metrics", {})

        if not isinstance(metrics, dict):
            metrics = {}

        (event_fingerprints, _) = self.normalize_fingerprints(
            baseline.get("event_fingerprints", {})
        )
        last_run = baseline.get("last_run")

        if last_run is not None and not isinstance(last_run, str):
            last_run = None

        payload = {
            "version": self.version,
            "last_run": last_run,
            "containers": containers,
            "metrics": metrics,
            "event_fingerprints": event_fingerprints,
            "log_progress": baseline.get("log_progress"),
            "delivery": baseline.get("delivery"),
            "last_acknowledged_delivery": baseline.get("last_acknowledged_delivery"),
            "log_gaps": baseline.get("log_gaps", []),
            "monitoring": baseline.get("monitoring", {}),
        }
        try:
            atomic_json(self.path, payload)
        except OSError as exception:
            raise ToolError("The persistent baseline could not be saved.") from exception
