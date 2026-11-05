"""Load and apply per-service log noise profiles."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from shadow.discovery import container_metadata


class ProfileService:
    def __init__(self, *, path: Path, find_container: Callable):
        self.path = path
        self.find_container = find_container

    def load(self) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
        """
        Load reusable log profiles from configuration.

        Profiles are selected by the
        io.shadowofintent.mcp.log-profile label.
        Invalid configuration never blocks
        read-only observation.
        """

        warnings: list[str] = []

        try:
            raw_data = json.loads(self.path.read_text(encoding="utf-8"))

        except FileNotFoundError:
            return ({}, ["Log profile configuration file was not found."])

        except (OSError, json.JSONDecodeError) as exception:
            return ({}, [f"Log profiles could not be loaded: {exception}"])

        if not isinstance(raw_data, dict):
            return ({}, ["The root of log-profiles.json must be a JSON object."])

        if raw_data.get("version") != 1:
            return ({}, ["Unsupported log-profiles.json version."])

        raw_profiles = raw_data.get("profiles", {})

        if not isinstance(raw_profiles, dict):
            return ({}, ["The profiles field must be a JSON object."])

        profiles: dict[str, dict[str, list[str]]] = {}

        for raw_name, raw_profile in raw_profiles.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                warnings.append("A log profile with an invalid name was ignored.")
                continue

            name = raw_name.strip().lower()

            if not isinstance(raw_profile, dict):
                warnings.append(
                    f"Log profile '{name}' was ignored because it is not an object."
                )
                continue

            raw_patterns = raw_profile.get("ignore_patterns", [])

            if not isinstance(raw_patterns, list):
                warnings.append(
                    f"Log profile '{name}' was ignored because "
                    "ignore_patterns is not a list."
                )
                continue

            valid_patterns: list[str] = []

            for raw_pattern in raw_patterns:
                if not isinstance(raw_pattern, str) or not raw_pattern.strip():
                    warnings.append(
                        f"An invalid pattern from log profile '{name}' was ignored."
                    )
                    continue

                pattern = raw_pattern.strip()

                try:
                    re.compile(pattern)

                except re.error as exception:
                    warnings.append(
                        f"An invalid regular expression from log profile "
                        f"'{name}' was ignored: {exception}"
                    )
                    continue

                valid_patterns.append(pattern)

            profiles[name] = {"ignore_patterns": valid_patterns}

        return (profiles, warnings)

    def resolve(
        self,
        container_name: str, profiles: Any
    ) -> dict[str, Any]:
        """
        Resolve the reusable log profile selected by a container label.

        Unknown profiles fall back to the generic profile and emit a warning.
        """

        normalized_name = container_name.strip().lstrip("/")
        warnings: list[str] = []

        try:
            metadata = container_metadata(self.find_container(normalized_name))

        except ToolError as exception:
            return {
                "requested": "generic",
                "selected": "generic",
                "profile": {"ignore_patterns": []},
                "warnings": [
                    (
                        "Log profile metadata could not be resolved for "
                        f"'{normalized_name}': {exception}"
                    )
                ],
            }

        requested = str(metadata.get("log_profile", "generic")).strip().lower() or "generic"

        if not isinstance(profiles, dict):
            warnings.append(
                "Log profiles are not a valid object; using an empty generic profile."
            )
            return {
                "requested": requested,
                "selected": "generic",
                "profile": {"ignore_patterns": []},
                "warnings": warnings,
            }

        selected = requested
        profile = profiles.get(requested)

        if not isinstance(profile, dict):
            warnings.append(
                (
                    f"Container '{normalized_name}' requests unknown log profile "
                    f"'{requested}'; falling back to 'generic'."
                )
            )
            selected = "generic"
            profile = profiles.get("generic", {})

        if not isinstance(profile, dict):
            profile = {}

        raw_patterns = profile.get("ignore_patterns", [])

        if not isinstance(raw_patterns, list):
            warnings.append(
                (
                    f"Log profile '{selected}' has an invalid ignore_patterns "
                    "value; no profile patterns will be applied."
                )
            )
            raw_patterns = []

        return {
            "requested": requested,
            "selected": selected,
            "profile": {"ignore_patterns": list(raw_patterns)},
            "warnings": warnings,
        }

    def filter_noise(
        self,
        container_name: str, value: str
    ) -> tuple[str, int, list[str]]:
        """
        Apply reusable log exclusions selected through the container label.
        """

        (profiles, load_warnings) = self.load()
        resolution = self.resolve(container_name, profiles)
        warnings = [*load_warnings, *resolution.get("warnings", [])]
        profile = resolution.get("profile", {})

        if not isinstance(profile, dict):
            profile = {}

        raw_patterns = profile.get("ignore_patterns", [])

        if not isinstance(raw_patterns, list):
            raw_patterns = []

        compiled_patterns: list[re.Pattern[str]] = []

        for pattern in raw_patterns:
            try:
                compiled_patterns.append(re.compile(pattern))

            except re.error as exception:
                warnings.append(
                    (
                        "An invalid regular expression from the selected "
                        f"log profile was ignored: {exception}"
                    )
                )

        if not compiled_patterns:
            return (value, 0, warnings)

        filtered: list[str] = []
        omitted = 0

        for line in value.splitlines():
            if any(pattern.search(line) for pattern in compiled_patterns):
                omitted += 1
                continue

            filtered.append(line)

        return ("\n".join(filtered), omitted, warnings)
