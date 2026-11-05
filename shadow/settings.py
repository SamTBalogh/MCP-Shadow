"""Configuration for MCP Shadow's server and dependencies."""
from __future__ import annotations

import os
import math
import re
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class SettingsError(ValueError):
    """An environment variable contains an invalid configuration value."""


def _number(values: Mapping[str, str], name: str, default: str, *, minimum: float,
            maximum: float | None = None, integer: bool = False) -> int | float:
    raw = values.get(name, default)
    try:
        number = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as error:
        raise SettingsError(f"{name} must be a valid {'integer' if integer else 'number'}.") from error
    if (not integer and not math.isfinite(number)) or number < minimum or (maximum is not None and number > maximum):
        limit = f" between {minimum:g} and {maximum:g}" if maximum is not None else f" at least {minimum:g}"
        raise SettingsError(f"{name} must be{limit}.")
    return number


def _url(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip()
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        valid = valid and not any(char.isspace() for char in value)
        valid = valid and not parsed.query and not parsed.fragment
        parsed.port
    except ValueError as error:
        raise SettingsError(f"{name} must be a valid HTTP or HTTPS base URL.") from error
    if not valid:
        raise SettingsError(f"{name} must be a valid HTTP or HTTPS base URL without query or fragment.")
    return value.rstrip("/")


def _text(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{name} must not be empty.")
    return value


def _boolean(values: Mapping[str, str], name: str, default: str) -> bool:
    value = values.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise SettingsError(f"{name} must be true or false.")
    return value == "true"


def _header_values(values: Mapping[str, str], name: str, pattern: str) -> tuple[str, ...]:
    entries = tuple(entry for item in values.get(name, "").split(",") if (entry := item.strip()))
    if any(not re.fullmatch(pattern, entry) for entry in entries):
        raise SettingsError(f"{name} must contain exact comma-separated values without wildcards.")
    return entries


@dataclass(frozen=True)
class Settings:
    server_name: str
    mcp_allowed_hosts: tuple[str, ...]
    mcp_allowed_origins: tuple[str, ...]
    docker_api_base: str
    docker_workers: int
    disk_probe_path: str
    max_log_lines: int
    max_log_chars: int
    log_profiles_path: Path
    state_dir: Path
    max_event_fingerprints: int
    ollama_api_base: str
    ollama_model: str
    ollama_timeout_seconds: float
    ollama_max_input_chars: int
    ollama_think: bool
    ollama_temperature: float
    diagnostic_cache_ttl_seconds: float
    container_discovery_cache_ttl_seconds: float

    analysis_provider: str
    analysis_failure_threshold: int
    analysis_cooldown_seconds: float
    log_spool_max_bytes: int
    log_spool_total_max_bytes: int
    log_spool_min_free_bytes: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        _number(values, "AUTO_ANALYSIS_MAX_EVENTS_PER_CONTAINER", "5", minimum=1, integer=True)
        _number(values, "AUTO_ANALYSIS_MAX_EVENTS_TOTAL", "20", minimum=1, integer=True)
        _number(values, "AUTO_ANALYSIS_MAX_CONTEXT_CHARS", "4000", minimum=1000, integer=True)
        _number(values, "SUSTAINED_CPU_THRESHOLD_PERCENT", "90", minimum=1, maximum=10000)
        memory = _number(values, "SUSTAINED_MEMORY_THRESHOLD_PERCENT", "85", minimum=1, maximum=100)
        _number(values, "SUSTAINED_MEMORY_HIGH_THRESHOLD_PERCENT", "95", minimum=memory, maximum=100)
        _number(values, "SUSTAINED_METRIC_SAMPLES", "3", minimum=2, maximum=20, integer=True)
        provider = _text(values, "ANALYSIS_PROVIDER", "ollama").strip()
        if provider not in {"ollama", "disabled"} and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", provider):
            raise SettingsError("ANALYSIS_PROVIDER must be ollama, disabled, or module:factory.")
        published_port = _number(values, "MCP_PORT", "8010", minimum=1, maximum=65535, integer=True)
        port_suffixes = ("", *(f":{port}" for port in sorted({8000, published_port})))
        default_hosts = ("mcp-shadow:8000",) + tuple(
            host + port for host in ("localhost", "127.0.0.1", "[::1]") for port in port_suffixes
        )
        authority = r"(?:[a-zA-Z0-9_.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]{1,5})?"
        return cls(
            analysis_provider=provider,
            analysis_failure_threshold=_number(values, "ANALYSIS_FAILURE_THRESHOLD", "2", minimum=1, maximum=20, integer=True),
            analysis_cooldown_seconds=_number(values, "ANALYSIS_COOLDOWN_SECONDS", "30", minimum=1, maximum=600),
            log_spool_total_max_bytes=_number(values, "LOG_SPOOL_TOTAL_MAX_BYTES", "1073741824", minimum=65536, maximum=68719476736, integer=True),
            log_spool_min_free_bytes=_number(values, "LOG_SPOOL_MIN_FREE_BYTES", "67108864", minimum=0, maximum=68719476736, integer=True),
            log_spool_max_bytes=_number(values, "LOG_SPOOL_MAX_BYTES", "268435456", minimum=65536, maximum=4294967296, integer=True),
            server_name=_text(values, "SERVER_NAME", "mcp-shadow"),
            mcp_allowed_hosts=default_hosts + _header_values(values, "MCP_ALLOWED_HOSTS", authority),
            mcp_allowed_origins=tuple(f"http://{host}" for host in default_hosts)
            + _header_values(values, "MCP_ALLOWED_ORIGINS", rf"https?://{authority}"),
            docker_api_base=_url(values, "DOCKER_API_BASE", "http://docker-proxy:2375"),
            docker_workers=_number(values, "DOCKER_WORKERS", "4", minimum=1, maximum=8, integer=True),
            disk_probe_path=_text(values, "DISK_PROBE_PATH", "/host/disk"),
            max_log_lines=_number(values, "MAX_LOG_LINES", "200", minimum=1, integer=True),
            max_log_chars=_number(values, "MAX_LOG_CHARS", "50000", minimum=1000, integer=True),
            log_profiles_path=Path(_text(values, "LOG_PROFILES_PATH", "/app/config/log-profiles.json")),
            state_dir=Path(_text(values, "STATE_DIR", "/app/state")),
            max_event_fingerprints=_number(values, "MAX_EVENT_FINGERPRINTS_PER_CONTAINER", "256", minimum=16, integer=True),
            ollama_api_base=_url(values, "OLLAMA_API_BASE", "http://ollama:11434"),
            ollama_model=_text(values, "OLLAMA_MODEL", "qwen3.5"),
            ollama_timeout_seconds=_number(values, "OLLAMA_TIMEOUT_SECONDS", "180", minimum=10),
            ollama_max_input_chars=_number(values, "OLLAMA_MAX_INPUT_CHARS", "8000", minimum=1000, integer=True),
            ollama_think=_boolean(values, "OLLAMA_THINK", "false"),
            ollama_temperature=_number(values, "OLLAMA_TEMPERATURE", "0", minimum=0, maximum=2),
            diagnostic_cache_ttl_seconds=_number(values, "DIAGNOSTIC_CACHE_TTL_SECONDS", "120", minimum=0),
            container_discovery_cache_ttl_seconds=_number(values, "CONTAINER_DISCOVERY_CACHE_TTL_SECONDS", "5", minimum=0),
        )
