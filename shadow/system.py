"""Server and monitored filesystem status."""
from __future__ import annotations

import shutil
import time
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from formatting import human_bytes, human_duration
from shadow.discovery import container_names


class SystemService:
    def __init__(
        self, *, disk_probe_path: str, server_name: str, get_disk: Callable,
        all_containers: Callable, managed_names: Callable, system: Any,
    ):
        self.disk_probe_path = disk_probe_path
        self.server_name = server_name
        self.get_disk = get_disk
        self.all_containers = all_containers
        self.managed_names = managed_names
        self.system = system

    def disk_snapshot(self) -> dict[str, Any]:
        """Collect usage information for the monitored filesystem."""

        try:
            usage = shutil.disk_usage(self.disk_probe_path)

        except OSError as exception:
            raise ToolError(
                f"Filesystem usage could not be read from {self.disk_probe_path}."
            ) from exception

        if usage.total:
            percent = round((usage.used / usage.total) * 100, 1)

        else:
            percent = 0

        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "total": human_bytes(usage.total),
            "used": human_bytes(usage.used),
            "free": human_bytes(usage.free),
            "usage_percent": percent,
        }

    def server_status(self) -> dict[str, Any]:
        """Return CPU, memory, swap, uptime, storage, and a Docker summary."""

        cpu_percent = self.system.cpu_percent(interval=0.25)
        memory = self.system.virtual_memory()
        swap = self.system.swap_memory()
        uptime_seconds = max(0, time.time() - self.system.boot_time())
        disk = self.get_disk()
        warnings: list[str] = []
        visible_containers = []

        try:
            for container in self.all_containers():
                names = container_names(container)
                authorized_name = next(
                    (name for name in names if name in self.managed_names()), None
                )

                if authorized_name:
                    visible_containers.append(
                        {
                            "name": authorized_name,
                            "state": container.get("State", "unknown"),
                            "detail": container.get("Status", ""),
                        }
                    )

            found_names = {container["name"] for container in visible_containers}
            missing = sorted(self.managed_names() - found_names)
            not_running = [
                container["name"]
                for container in visible_containers
                if container["state"] != "running"
            ]
            docker_summary = {
                "authorized": len(self.managed_names()),
                "found": len(visible_containers),
                "running": sum(
                    container["state"] == "running" for container in visible_containers
                ),
                "not_running": not_running,
                "not_found": missing,
            }

            if not_running:
                warnings.append("Stopped containers: " + ", ".join(not_running))

            if missing:
                warnings.append("Containers not found: " + ", ".join(missing))

        except ToolError as exception:
            docker_summary = {"error": str(exception)}
            warnings.append("Could not query Docker.")

        if cpu_percent >= 90:
            warnings.append(f"High CPU usage: {cpu_percent:.1f} %.")

        if memory.percent >= 90:
            warnings.append(f"High RAM usage: {memory.percent:.1f} %.")

        if disk["usage_percent"] >= 90:
            warnings.append(f"High storage usage: {disk['usage_percent']} %.")

        return {
            "server": self.server_name,
            "summary": ("attention" if warnings else "ok"),
            "warnings": warnings,
            "cpu": {
                "percent": round(cpu_percent, 1),
                "logical_cores": self.system.cpu_count(logical=True),
            },
            "memory": {
                "total": human_bytes(memory.total),
                "used": human_bytes(memory.used),
                "available": human_bytes(memory.available),
                "usage_percent": round(memory.percent, 1),
            },
            "swap": {
                "total": human_bytes(swap.total),
                "used": human_bytes(swap.used),
                "usage_percent": round(swap.percent, 1),
            },
            "uptime": {
                "seconds": int(uptime_seconds),
                "formatted": human_duration(uptime_seconds),
            },
            "storage": disk,
            "containers": docker_summary,
        }
