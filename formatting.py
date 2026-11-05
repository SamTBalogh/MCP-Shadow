"""Formatting and Docker log stream helpers."""

from datetime import timedelta


def human_bytes(value: int | float) -> str:
    """Convert a byte count to a human-readable representation."""

    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(value)

    for unit in units:
        if abs(number) < 1024:
            return f"{number:.1f} {unit}"

        if unit == units[-1]:
            return f"{number:.1f} {unit}"

        number /= 1024

    return f"{number:.1f} TiB"


def human_duration(seconds: float) -> str:
    """Convert seconds to a human-readable duration."""

    delta = timedelta(seconds=max(0, int(seconds)))
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []

    if days:
        parts.append(f"{days} d")

    if hours or days:
        parts.append(f"{hours} h")

    if minutes or hours or days:
        parts.append(f"{minutes} min")

    parts.append(f"{seconds} s")
    return " ".join(parts)


def decode_docker_logs(data: bytes) -> str:
    """Remove Docker multiplexed stream headers when present."""

    chunks: list[bytes] = []
    position = 0

    while position + 8 <= len(data):
        header = data[position : position + 8]
        valid_stream = header[0] in (0, 1, 2)
        valid_header = header[1:4] == b"\x00\x00\x00"

        if not (valid_stream and valid_header):
            break

        length = int.from_bytes(header[4:8], byteorder="big")
        end = position + 8 + length

        if end > len(data):
            break

        chunks.append(data[position + 8 : end])
        position = end

    if chunks and position == len(data):
        data = b"".join(chunks)

    return data.decode("utf-8", errors="replace")


def decode_docker_logs_prefix(data: bytes) -> str:
    """Decode a bounded Docker stream prefix, including a partial final frame."""
    if len(data) < 8 or data[0] not in (0, 1, 2) or data[1:4] != b"\0\0\0":
        return data.decode("utf-8", errors="replace")

    chunks = []
    position = 0
    while position + 8 <= len(data):
        header = data[position : position + 8]
        if header[0] not in (0, 1, 2) or header[1:4] != b"\0\0\0":
            return data.decode("utf-8", errors="replace")
        size = int.from_bytes(header[4:8], "big")
        start = position + 8
        end = start + size
        chunks.append(data[start : min(end, len(data))])
        if end > len(data):
            break
        position = end
    return b"".join(chunks).decode("utf-8", errors="replace")
