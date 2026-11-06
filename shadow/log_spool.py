"""Capture each fixed Docker interval once, then page a verified local snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import suppress
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from shadow.log_pages import _payload_chunks, read_page
from shadow.persistence import atomic_json, sync_directory
from shadow.storage import SnapshotBudget

BLOCK_SIZE = 65536


class LogSpool:
    """Used under the report cycle lock. Snapshot files contain private raw logs.

    A manifest is published only after the complete Docker stream is captured.
    Block hashes verify only the blocks being read, avoiding prefix hashing.
    A missing or damaged snapshot with a saved cursor is never fetched again.
    """

    def __init__(self, directory, max_bytes, total_max_bytes=1073741824, min_free_bytes=67108864):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.budget = SnapshotBudget(self.directory, total_max_bytes, min_free_bytes)

    def _capture(self, chunks, container_id, since, until):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool_id = uuid.uuid4().hex
        path = self.directory / f"{spool_id}.bin"
        hashes = []
        size = 0
        pending = bytearray()
        try:
            existing_bytes = self.budget.used_bytes()
            self.budget.check_capture(existing_bytes, 0, 0, 2048)
            with path.open("xb") as file:
                os.chmod(path, 0o600)
                for chunk in _payload_chunks(chunks):
                    # Include a conservative manifest allowance in the global
                    # quota. All captures are serialized by the report lock.
                    manifest_bytes = 2048 + 70 * ((size + len(chunk) + BLOCK_SIZE - 1) // BLOCK_SIZE)
                    self.budget.check_capture(existing_bytes, size, len(chunk), manifest_bytes)
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ToolError("The log interval exceeds LOG_SPOOL_MAX_BYTES; the baseline was not advanced.")
                    file.write(chunk)
                    pending.extend(chunk)
                    while len(pending) >= BLOCK_SIZE:
                        hashes.append(hashlib.sha256(pending[:BLOCK_SIZE]).hexdigest())
                        del pending[:BLOCK_SIZE]
                if pending:
                    hashes.append(hashlib.sha256(pending).hexdigest())
                file.flush()
                os.fsync(file.fileno())
            manifest = {"version": 1, "spool_id": spool_id, "container_id": container_id,
                        "since": since, "until": until, "size": size, "hashes": hashes}
            atomic_json(self.directory / f"{spool_id}.json", manifest)
            return spool_id
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            close = getattr(chunks, "close", None)
            if close:
                close()

    def page(self, stream, container_id, since, until, cursor, max_chars):
        new_snapshot = None
        try:
            spool_id = cursor.get("spool_id")
            if spool_id is None:
                if cursor.get("offset", 0):
                    raise ToolError("A legacy cursor cannot be resumed as a local log snapshot.")
                spool_id = new_snapshot = self._capture(stream(), container_id, since, until)
            if not isinstance(spool_id, str) or re.fullmatch(r"[0-9a-f]{32}", spool_id) is None:
                raise ToolError("The stored log snapshot identifier is invalid.")
            manifest = json.loads((self.directory / f"{spool_id}.json").read_text())
            if (manifest.get("version") != 1 or manifest.get("spool_id") != spool_id
                    or manifest.get("container_id") != container_id
                    or manifest.get("since") != since or manifest.get("until") != until):
                raise ToolError("The log snapshot does not match the requested interval or container.")
            offset = cursor.get("offset", 0)
            size = manifest["size"]
            hashes = manifest["hashes"]
            if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                    or not isinstance(hashes, list) or len(hashes) != (size + BLOCK_SIZE - 1) // BLOCK_SIZE
                    or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= size):
                raise ToolError("The stored log snapshot cursor or manifest is invalid.")
            path = self.directory / f"{spool_id}.bin"
            if path.stat().st_size != size:
                raise ToolError("The local log snapshot size changed; the baseline was not advanced.")

            def blocks():
                with path.open("rb") as file:
                    index = offset // BLOCK_SIZE
                    file.seek(index * BLOCK_SIZE)
                    first = True
                    while data := file.read(BLOCK_SIZE):
                        if index >= len(hashes) or hashlib.sha256(data).hexdigest() != hashes[index]:
                            raise ToolError("The local log snapshot failed its integrity check.")
                        yield data[offset % BLOCK_SIZE:] if first else data
                        first = False
                        index += 1

            logs, next_cursor = read_page(blocks(), {}, max_chars)
            next_cursor["offset"] += offset
            next_cursor["spool_id"] = spool_id
            next_cursor.pop("sha256", None)
            new_snapshot = None
            return logs, next_cursor
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise ToolError("The local log snapshot is missing or invalid; the baseline was not advanced.") from error
        finally:
            if new_snapshot is not None:
                # First page failed, so no cursor can reference this capture.
                for suffix in ("json", "bin"):
                    with suppress(OSError):
                        (self.directory / f"{new_snapshot}.{suffix}").unlink(missing_ok=True)

    def status(self):
        return self.budget.status()

    def cleanup(self, progress, archived_progress=()):
        """Reclaim unused files before collecting, under the lock with no pending report."""
        if not self.directory.exists():
            return
        if progress is None:
            progress = {"containers": {}}
        if archived_progress:
            retained = dict(progress.get("containers", {}))
            for index, archived in enumerate(archived_progress):
                if not isinstance(archived, dict) or not isinstance(archived.get("containers"), dict):
                    raise ToolError("Invalid recovery snapshots; no files were reclaimed.")
                retained.update({f"archived/{index}/{name}": cursor for name, cursor in archived["containers"].items()})
            progress = {"containers": retained}
        cursors = progress.get("containers") if isinstance(progress, dict) else None
        if not isinstance(cursors, dict) or any(not isinstance(cursor, dict) for cursor in cursors.values()):
            raise ToolError("The stored log progress is invalid; no snapshots were reclaimed.")
        referenced = set()
        for cursor in cursors.values():
            identifier = cursor.get("spool_id")
            if identifier is not None:
                if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
                    raise ToolError("The stored log snapshot identifier is invalid; no snapshots were reclaimed.")
                referenced.add(identifier)

        try:
            removed = False
            for path in self.directory.iterdir():
                # A temporary manifest is never a committed cursor target.
                match = re.fullmatch(r"([0-9a-f]{32})\.(?:bin|json)|\.[0-9a-f]{32}\.json\.[a-z0-9_]+", path.name)
                if match and match[1] not in referenced and not path.is_dir():
                    path.unlink(missing_ok=True)
                    removed = True
            if removed:
                sync_directory(self.directory)
        except OSError as error:
            raise ToolError("Unreferenced log snapshots could not be reclaimed.") from error
