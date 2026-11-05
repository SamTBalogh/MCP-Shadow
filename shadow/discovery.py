"""Discovery and metadata for containers explicitly enabled through labels."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError

MCP_ENABLED_LABEL = "io.shadowofintent.mcp.enabled"
MCP_NAME_LABEL = "io.shadowofintent.mcp.name"
MCP_DESCRIPTION_LABEL = "io.shadowofintent.mcp.description"
MCP_LOG_PROFILE_LABEL = "io.shadowofintent.mcp.log-profile"
MCP_EXPECTED_STOPPED_LABEL = "io.shadowofintent.mcp.expected-stopped"


def label_is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def container_labels(container: dict[str, Any]) -> dict[str, str]:
    labels = container.get("Labels", {}) or {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def container_names(container: dict[str, Any]) -> list[str]:
    return [str(name).lstrip("/") for name in container.get("Names", [])]


def container_metadata(container: dict[str, Any]) -> dict[str, Any]:
    labels = container_labels(container)
    names = container_names(container)
    default_name = names[0] if names else str(container.get("Id", ""))[:12]
    return {
        "name": labels.get(MCP_NAME_LABEL, "").strip() or default_name,
        "description": labels.get(MCP_DESCRIPTION_LABEL, "").strip(),
        "log_profile": labels.get(MCP_LOG_PROFILE_LABEL, "generic").strip().lower() or "generic",
        "expected_stopped": label_is_true(labels.get(MCP_EXPECTED_STOPPED_LABEL)),
        "compose_project": labels.get("com.docker.compose.project", "").strip(),
        "compose_service": labels.get("com.docker.compose.service", "").strip(),
    }


class ContainerRegistry:
    """Discover and cache containers without exposing unmanaged containers."""

    def __init__(self, list_containers: Callable, ttl_seconds: float):
        self.list_containers = list_containers
        self.ttl_seconds = ttl_seconds
        self._cache: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = threading.Lock()

    def managed(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            if (not force_refresh and self._cache is not None
                    and now - self._cache[0] < self.ttl_seconds):
                return [dict(container) for container in self._cache[1]]
            containers = [container for container in self.list_containers()
                          if label_is_true(container_labels(container).get(MCP_ENABLED_LABEL))]
            self._cache = (time.monotonic(), containers)
            return [dict(container) for container in containers]

    def names(self) -> set[str]:
        return {name for container in self.managed() for name in container_names(container)}

    def expected_stopped_names(self) -> set[str]:
        return {name for container in self.managed()
                if label_is_true(container_labels(container).get(MCP_EXPECTED_STOPPED_LABEL))
                for name in container_names(container)}

    def find(self, name: str) -> dict[str, Any]:
        normalized = name.strip().lstrip("/")
        if normalized not in self.names():
            raise ToolError(f"Container '{normalized}' is not managed by MCP.")
        for container in self.managed():
            if normalized in container_names(container):
                return container
        raise ToolError(f"Container '{normalized}' was not found.")

    def list_services(self, load_profiles: Callable, resolve_profile: Callable) -> list[dict[str, Any]]:
        """List services currently managed through MCP Docker labels. The response includes human-readable metadata, Compose identity, log-profile resolution, and current Docker state."""

        (profiles, profile_warnings) = load_profiles()
        services: list[dict[str, Any]] = []

        for container in self.managed(force_refresh=True):
            names = container_names(container)

            if not names:
                continue

            container_name = names[0]
            metadata = container_metadata(container)
            resolution = resolve_profile(container_name, profiles)
            warnings = [*profile_warnings, *resolution.get("warnings", [])]
            services.append(
                {
                    "container_name": container_name,
                    "display_name": metadata.get("name", container_name),
                    "description": metadata.get("description", ""),
                    "compose_project": metadata.get("compose_project", ""),
                    "compose_service": metadata.get("compose_service", ""),
                    "requested_log_profile": resolution.get("requested", "generic"),
                    "effective_log_profile": resolution.get("selected", "generic"),
                    "expected_stopped": bool(metadata.get("expected_stopped", False)),
                    "image": container.get("Image", ""),
                    "state": container.get("State", "unknown"),
                    "status": container.get("Status", ""),
                    "warnings": warnings,
                }
            )

        return sorted(services, key=lambda service: str(service.get("container_name", "")))
