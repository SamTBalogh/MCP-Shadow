"""Read-only operations on explicitly managed Docker containers."""
from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from formatting import decode_docker_logs, decode_docker_logs_prefix, human_bytes
from shadow.log_pages import read_page


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
        details = self.docker_get(f"/containers/{container['Id']}/json").json()
        state = details.get("State", {})
        health = state.get("Health", {})
        networks = details.get("NetworkSettings", {}).get("Networks", {})
        return {
            "name": details.get("Name", "").lstrip("/"),
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
        stats = self.docker_get(
            f"/containers/{container['Id']}/stats", params={"stream": "false"}
        ).json()
        cpu_stats = stats.get("cpu_stats", {})
        previous_cpu_stats = stats.get("precpu_stats", {})
        cpu_usage = cpu_stats.get("cpu_usage", {})
        previous_cpu_usage = previous_cpu_stats.get("cpu_usage", {})
        cpu_delta = cpu_usage.get("total_usage", 0) - previous_cpu_usage.get(
            "total_usage", 0
        )
        system_delta = cpu_stats.get("system_cpu_usage", 0) - previous_cpu_stats.get(
            "system_cpu_usage", 0
        )
        online_cpus = (
            cpu_stats.get("online_cpus") or len(cpu_usage.get("percpu_usage", [])) or 1
        )
        cpu_percent = 0

        if cpu_delta > 0 and system_delta > 0:
            cpu_percent = cpu_delta / system_delta * online_cpus * 100

        memory_stats = stats.get("memory_stats", {})
        memory_usage = int(memory_stats.get("usage", 0))
        memory_details = memory_stats.get("stats", {})
        inactive_file = int(
            memory_details.get(
                "total_inactive_file", memory_details.get("inactive_file", 0)
            )
        )
        memory_working_set = max(0, memory_usage - inactive_file)
        memory_limit = int(memory_stats.get("limit", 0))

        if memory_limit:
            memory_percent = memory_working_set / memory_limit * 100

        else:
            memory_percent = 0

        networks = stats.get("networks", {})
        received_bytes = sum(
            int(network.get("rx_bytes", 0)) for network in networks.values()
        )
        sent_bytes = sum(int(network.get("tx_bytes", 0)) for network in networks.values())
        return {
            "name": container_name.strip().lstrip("/"),
            "cpu_percent": round(cpu_percent, 2),
            "memory": {
                "usage": human_bytes(memory_working_set),
                "limit": human_bytes(memory_limit),
                "percent": round(memory_percent, 2),
            },
            "network": {
                "received": human_bytes(received_bytes),
                "sent": human_bytes(sent_bytes),
            },
            "processes": stats.get("pids_stats", {}).get("current", 0),
        }

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
