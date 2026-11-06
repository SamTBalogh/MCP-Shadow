"""Read-only operations on explicitly managed Docker containers."""
from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from formatting import decode_docker_logs, decode_docker_logs_prefix
from shadow.log_pages import read_page
from shadow.statistics import parse_statistics
from shadow.docker_client import ContainerNotFoundError, DockerHTTPError


class ContainerService:
    def __init__(
        self, *, find_container: Callable, docker_get: Callable,
        get_bounded: Callable, all_containers: Callable,
        managed_container_names: Callable, container_names: Callable,
        max_lines: int, max_chars: int,
    ):
        self.find_container = find_container
        self.docker_get = docker_get
        self.get_bounded = get_bounded
        self.all_containers = all_containers
        self.managed_container_names = managed_container_names
        self.container_names = container_names
        self.max_lines = max_lines
        self.max_chars = max_chars

    def list_containers(self) -> list[dict[str, Any]]:
        """List only containers explicitly enabled for MCP management."""

        result = []

        for container in self.all_containers():
            names = self.container_names(container)
            authorized_name = next(
                (name for name in names if name in self.managed_container_names()), None
            )

            if not authorized_name:
                continue

            result.append(
                {
                    "name": authorized_name,
                    "image": container.get("Image", ""),
                    "state": container.get("State", "unknown"),
                    "detail": container.get("Status", ""),
                }
            )

        found_names = {container["name"] for container in result}

        for missing in sorted(self.managed_container_names() - found_names):
            result.append(
                {
                    "name": missing,
                    "image": None,
                    "state": "not_found",
                    "detail": ("Authorized, but not found."),
                }
            )

        return sorted(result, key=lambda container: container["name"])

    def inspect(self, container_name: str) -> dict[str, Any]:
        """Return detailed information for a managed container. Environment variables are never returned."""

        container = self.find_container(container_name)
        try:
            details = self.docker_get(f"/containers/{container['Id']}/json").json()
        except DockerHTTPError as exception:
            if exception.status_code == 404:
                raise ContainerNotFoundError("The inspected container was not found.") from exception
            raise
        except ValueError as exception:
            raise ToolError("Docker returned invalid inspection data.") from exception
        if not isinstance(details, dict) or not isinstance(details.get("State"), dict):
            raise ToolError("Docker inspection data has an unexpected format.")
        state = details.get("State", {})
        health = state.get("Health", {})
        networks = details.get("NetworkSettings", {}).get("Networks", {})
        return {
            "name": details.get("Name", "").lstrip("/"),
            "container_id": container["Id"],
            "image": details.get("Config", {}).get("Image", ""),
            "state": state.get("Status", "unknown"),
            "running": state.get("Running", False),
            "paused": state.get("Paused", False),
            "restarting": state.get("Restarting", False),
            "oom_killed": state.get("OOMKilled", False),
            "exit_code": state.get("ExitCode"),
            "error": state.get("Error", ""),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "health": health.get("Status", "no_healthcheck"),
            "restart_count": details.get("RestartCount", 0),
            "networks": {
                network_name: {
                    "ip": network_data.get("IPAddress", ""),
                    "gateway": network_data.get("Gateway", ""),
                }
                for (network_name, network_data) in networks.items()
            },
        }

    def stats(self, container_name: str) -> dict[str, Any]:
        """Return CPU, memory, network traffic, and process statistics for a managed container."""

        container = self.find_container(container_name)
        try:
            stats = self.docker_get(
                f"/containers/{container['Id']}/stats", params={"stream": "false"}
            ).json()
            metrics = parse_statistics(stats)
        except (ToolError, ValueError, TypeError) as exception:
            metrics = parse_statistics(error=str(exception))
        return {"name": container_name.strip().lstrip("/"), "container_id": container["Id"], **metrics}

    def logs(self, container_name: str, lines: int, since: int | None = None) -> dict[str, Any]:
        """Fetch and validate logs for a managed container."""

        if lines < 1:
            raise ToolError("The number of log lines must be greater than zero.")

        if lines > self.max_lines:
            raise ToolError(f"The maximum allowed number of log lines is {self.max_lines}.")

        if since is not None and since < 0:
            raise ToolError("The since timestamp cannot be negative.")

        container = self.find_container(container_name)
        params = {
            "stdout": "true",
            "stderr": "true",
            "timestamps": "true",
            "tail": str(lines),
        }

        if since is not None:
            params["since"] = str(since)
            params["tail"] = "all"

        # Bound wire bytes too, including Docker's multiplexing headers and UTF-8.
        data, wire_truncated = self.get_bounded(
            f"/containers/{container['Id']}/logs", params, self.max_chars * 4 + 8192
        )
        output = decode_docker_logs_prefix(data) if wire_truncated else decode_docker_logs(data)
        truncated = wire_truncated or len(output) > self.max_chars

        if truncated:
            output = output[:self.max_chars]

        return {
            "container_name": container_name.strip().lstrip("/"),
            "requested_lines": lines,
            "since": since,
            "mode": ("differential" if since is not None else "latest_lines"),
            "truncated": truncated,
            "logs": output,
        }

    def log_page(self, container_name: str, since: int, until: int, cursor: dict, stream: Callable, spool=None) -> dict[str, Any]:
        """Read a page from a fixed interval and verify any persisted cursor."""
        container = self.find_container(container_name)
        container_id = container["Id"]
        if cursor.get("container_id", container_id) != container_id:
            raise ToolError("The container changed while its log interval was being read.")
        params = {"stdout": "true", "stderr": "true", "timestamps": "true",
                  "tail": "all", "since": str(since), "until": str(until)}
        if spool is not None and (cursor.keys() <= {"container_id"} or cursor.get("spool_id") is not None):
            logs, next_cursor = spool.page(
                lambda: stream(f"/containers/{container_id}/logs", params),
                container_id, since, until, cursor, self.max_chars)
        else:
            # Finish previously persisted prefix-hash cursors without migration.
            logs, next_cursor = read_page(stream(f"/containers/{container_id}/logs", params), cursor, self.max_chars)
        if cursor.get("complete") is True and (logs or not next_cursor.get("complete")):
            raise ToolError("Previously completed logs changed while the interval was being read.")
        next_cursor["container_id"] = container_id
        return {"logs": logs, "cursor": next_cursor, "container_id": container_id}
