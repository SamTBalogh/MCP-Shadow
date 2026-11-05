"""Resume bounded Docker log pages by verifying the previously read prefix."""
from __future__ import annotations

import hashlib
import hmac
import re

from mcp.server.fastmcp.exceptions import ToolError

from shadow.events import _group_log_lines_into_events, _split_timestamped_log_line


def _payload_chunks(chunks):
    """Decode multiplexed Docker frames, or pass through a TTY byte stream."""
    buffer = bytearray()
    mode = None
    remaining = 0
    for chunk in chunks:
        buffer.extend(chunk)
        if mode is None:
            if len(buffer) < 8:
                continue
            mode = "frames" if buffer[0] in (0, 1, 2) and buffer[1:4] == b"\0\0\0" else "tty"
        if mode == "tty":
            yield bytes(buffer)
            buffer.clear()
            continue
        while buffer:
            if not remaining:
                if len(buffer) < 8:
                    break
                if buffer[0] not in (0, 1, 2) or buffer[1:4] != b"\0\0\0":
                    raise ToolError("The Docker log stream has an invalid frame header.")
                remaining = int.from_bytes(buffer[4:8], "big")
                del buffer[:8]
                if not remaining:
                    continue
            take = min(remaining, len(buffer))
            if take:
                yield bytes(buffer[:take])
                del buffer[:take]
                remaining -= take
    if mode is None and buffer:
        if buffer[0] in (0, 1, 2) and buffer[1:min(4, len(buffer))] == b"\0" * min(3, len(buffer) - 1):
            raise ToolError("The Docker log stream ended in an incomplete frame header.")
        yield bytes(buffer)
    elif mode == "frames" and (buffer or remaining):
        raise ToolError("The Docker log stream ended in an incomplete frame.")


def _decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolError("The paged log stream contains invalid UTF-8.") from error


def read_page(chunks, cursor, max_chars):
    """Read one complete-event page, replaying and verifying any prior prefix.

    The cursor counts decoded payload bytes, so frame boundaries can change. A
    digest mismatch is an explicit error rather than silently skipping data.
    """
    offset = cursor.get("offset", 0)
    expected = cursor.get("sha256", hashlib.sha256(b"").hexdigest())
    if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
        raise ToolError("The stored log cursor is invalid.")
    digest = hashlib.sha256()
    pending = bytearray()
    skipped = 0
    verified = False
    chosen = None
    hard_limit = max_chars * 4 + 8192

    def check_prefix():
        nonlocal verified
        if not verified and skipped == offset:
            if not hmac.compare_digest(digest.hexdigest(), expected):
                raise ToolError("The log prefix changed; logs may have rotated or the container was replaced.")
            verified = True

    try:
        for chunk in _payload_chunks(chunks):
            if skipped < offset:
                take = min(offset - skipped, len(chunk))
                digest.update(chunk[:take])
                skipped += take
                chunk = chunk[take:]
            check_prefix()
            pending.extend(chunk)
            if len(pending) >= max_chars and b"\n" in pending:
                complete_end = pending.rfind(b"\n") + 1
                complete = bytes(pending[:complete_end])
                lines = complete.splitlines(keepends=True)
                try:
                    decoded = complete.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ToolError("The paged log stream contains invalid UTF-8.") from error
                if len(decoded.splitlines(keepends=True)) != len(lines):
                    raise ToolError("The paged log stream contains unsupported line separators.")
                events = _group_log_lines_into_events(decoded)
                if len(events) > 1:
                    count = 0
                    size = 0
                    while count < len(lines):
                        _, message = _split_timestamped_log_line(lines[count].decode("utf-8").rstrip("\r\n"))
                        if message.strip():
                            break
                        size += len(lines[count])
                        count += 1
                    for event in events[:-1]:
                        end = count + event["physical_lines"]
                        size += sum(len(line) for line in lines[count:end])
                        count = end
                        if size >= max_chars:
                            break
                    chosen = b"".join(lines[:count])
                    break
            if len(pending) > hard_limit:
                raise ToolError("A single log event exceeds the safe page limit; increase MAX_LOG_CHARS.")
        else:
            check_prefix()
            if not verified:
                raise ToolError("The log prefix disappeared; logs may have rotated.")
            chosen = bytes(pending)
            digest.update(chosen)
            return _decode(chosen), {
                "offset": offset + len(chosen), "sha256": digest.hexdigest(), "complete": True,
            }
        if len(chosen) > hard_limit:
            raise ToolError("A single log event exceeds the safe page limit; increase MAX_LOG_CHARS.")
        digest.update(chosen)
        return _decode(chosen), {
            "offset": offset + len(chosen), "sha256": digest.hexdigest(),
        }
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
