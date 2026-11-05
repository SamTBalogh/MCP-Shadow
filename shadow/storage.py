"""Snapshot budgets and disk headroom, without reading private log contents."""
from __future__ import annotations

import shutil
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError


class SnapshotBudget:
    def __init__(self, directory, total_max_bytes, min_free_bytes):
        self.directory = Path(directory)
        self.total_max_bytes = total_max_bytes
        self.min_free_bytes = min_free_bytes

    def used_bytes(self):
        if not self.directory.exists():
            return 0
        return sum(path.stat().st_size for path in self.directory.iterdir()
                   if path.is_file() and not path.is_symlink())

    def check_capture(self, existing_bytes, written_bytes, next_bytes, manifest_bytes):
        if existing_bytes + written_bytes + next_bytes + manifest_bytes > self.total_max_bytes:
            raise ToolError("The global LOG_SPOOL_TOTAL_MAX_BYTES budget is exhausted; no log checkpoint was advanced.")
        free = shutil.disk_usage(self.directory).free
        if free - next_bytes - manifest_bytes < self.min_free_bytes:
            raise ToolError("Capturing logs would violate LOG_SPOOL_MIN_FREE_BYTES; no log checkpoint was advanced.")

    def status(self):
        try:
            root = self.directory if self.directory.exists() else self.directory.parent
            used = self.used_bytes()
            free = shutil.disk_usage(root).free
            return {"status": "capture_blocked" if used + 2048 > self.total_max_bytes or free <= self.min_free_bytes else "ok",
                    "snapshot_bytes": used, "remaining_bytes": max(0, self.total_max_bytes - used), "total_max_bytes": self.total_max_bytes,
                    "filesystem_free_bytes": free, "min_free_bytes": self.min_free_bytes}
        except OSError:
            return {"status": "unavailable"}
