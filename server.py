from __future__ import annotations

import json
import asyncio
import atexit
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
import psutil

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from shadow.reporting import ReportService, _report_markdown
from shadow.baseline import BaselineStore
from shadow.recovery import close_interval
from shadow.monitoring import monitor_status
from shadow.log_spool import LogSpool
from shadow.delivery import ReportDelivery
from shadow.ollama import OllamaAnalyzer
from shadow.analysis import AnalysisError, AnalysisProvider
from shadow.providers import create_provider, failure_kind
from shadow.docker_client import DockerClient
from shadow.containers import ContainerService
from shadow.discovery import ContainerRegistry, container_metadata, container_names
from shadow.settings import Settings
from shadow.readiness import check_readiness
from shadow.diagnostics import DiagnosticService
from shadow.observation import ObservationService
from shadow.collector import ObservationCollector
from shadow.profiles import ProfileService
from shadow.system import SystemService

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

logging.getLogger("httpx").setLevel(logging.WARNING)
SETTINGS = Settings.from_env()
ANALYSIS_PROVIDER = create_provider(SETTINGS, httpx)
SERVER_NAME = SETTINGS.server_name
DOCKER_API_BASE = SETTINGS.docker_api_base
DOCKER_CLIENT = DockerClient(DOCKER_API_BASE, httpx)
atexit.register(DOCKER_CLIENT.close)
DOCKER_WORKERS = SETTINGS.docker_workers
DISK_PROBE_PATH = SETTINGS.disk_probe_path
MAX_LOG_LINES = SETTINGS.max_log_lines
MAX_LOG_CHARS = SETTINGS.max_log_chars
LOG_PROFILES_PATH = SETTINGS.log_profiles_path
STATE_DIR = SETTINGS.state_dir
BASELINE_PATH = STATE_DIR / "baseline.json"
BASELINE_VERSION = 2
MAX_EVENT_FINGERPRINTS_PER_CONTAINER = SETTINGS.max_event_fingerprints
OLLAMA_API_BASE = SETTINGS.ollama_api_base
OLLAMA_MODEL = SETTINGS.ollama_model
OLLAMA_TIMEOUT_SECONDS = SETTINGS.ollama_timeout_seconds
OLLAMA_MAX_INPUT_CHARS = SETTINGS.ollama_max_input_chars
DIAGNOSTIC_CACHE_TTL_SECONDS = SETTINGS.diagnostic_cache_ttl_seconds
DIAGNOSTIC_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
MCP_ENABLED_LABEL = "io.shadowofintent.mcp.enabled"
MCP_NAME_LABEL = "io.shadowofintent.mcp.name"
MCP_DESCRIPTION_LABEL = "io.shadowofintent.mcp.description"
MCP_LOG_PROFILE_LABEL = "io.shadowofintent.mcp.log-profile"
MCP_EXPECTED_STOPPED_LABEL = "io.shadowofintent.mcp.expected-stopped"
CONTAINER_DISCOVERY_CACHE_TTL_SECONDS = SETTINGS.container_discovery_cache_ttl_seconds
CONTAINER_REGISTRY = ContainerRegistry(lambda: docker_containers(), CONTAINER_DISCOVERY_CACHE_TTL_SECONDS,
                                       SETTINGS.expected_container_names)


class UnambiguousMCPHeaders:
    """Reject header forms the SDK would treat as a single or absent value."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        if len(headers.getlist("host")) != 1:
            await Response("Invalid Host header", status_code=421)(scope, receive, send)
            return
        origins = headers.getlist("origin")
        if origins and (len(origins) != 1 or not origins[0]):
            await Response("Invalid Origin header", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class ShadowFastMCP(FastMCP):
    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        # Use the same route matching for these checks and for MCP dispatch.
        route = next(
            route for route in app.routes
            if isinstance(route, Route) and route.path == self.settings.streamable_http_path
        )
        route.app = UnambiguousMCPHeaders(route.app)
        return app


mcp = ShadowFastMCP(
    name="mcp-shadow",
    instructions=(
        "Read-only MCP server for homelab observability. "
        "It does not allow command execution or Docker "
        "container modifications."
    ),
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(SETTINGS.mcp_allowed_hosts),
        allowed_origins=list(SETTINGS.mcp_allowed_origins),
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """HTTP health endpoint for service monitors and availability checks."""

    return JSONResponse(
        {
            "status": "ok",
            "server": SERVER_NAME,
            "service": "mcp-shadow",
            "mode": "read-only",
            "transport": "streamable-http",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def _log_spool() -> LogSpool:
    return LogSpool(STATE_DIR / "log-spool", SETTINGS.log_spool_max_bytes,
                    SETTINGS.log_spool_total_max_bytes, SETTINGS.log_spool_min_free_bytes)


def _check_readiness() -> dict[str, Any]:
    result = check_readiness(
        docker_get=docker_get, state_dir=STATE_DIR, disk_probe_path=DISK_PROBE_PATH,
        provider=ANALYSIS_PROVIDER, provider_name=SETTINGS.analysis_provider,
    )
    result["log_storage"] = _log_spool().status()
    try:
        baseline, _, _ = _baseline_store().load()
        pending = baseline.get("delivery")
        result["monitoring"] = monitor_status(baseline)
        result["report_delivery"] = {
            "awaiting_acknowledgement": pending is not None,
            "pending_report_bytes": len(json.dumps(pending.get("report", {}), ensure_ascii=False).encode()) if isinstance(pending, dict) else 0,
        }
    except ToolError:
        result["report_delivery"] = {"status": "state_unavailable"}
        result["monitoring"] = {"status": "state_unavailable"}
    return result


@mcp.custom_route("/ready", methods=["GET"])
async def readiness(request: Request) -> JSONResponse:
    """Report required dependency readiness and optional analysis availability."""
    result = await asyncio.to_thread(_check_readiness)
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


def _baseline_store() -> BaselineStore:
    return BaselineStore(
        BASELINE_PATH, BASELINE_VERSION, MAX_EVENT_FINGERPRINTS_PER_CONTAINER
    )


def _normalize_event_fingerprint_store(
    value: Any,
) -> tuple[dict[str, list[str]], list[str]]:
    return _baseline_store().normalize_fingerprints(value)


def _empty_baseline() -> dict[str, Any]:
    return _baseline_store().empty()


def _load_baseline() -> tuple[dict[str, Any], list[str], bool]:
    return _baseline_store().load()


def _save_baseline(baseline: dict[str, Any]) -> None:
    _baseline_store().save(baseline)


def _profile_service() -> ProfileService:
    return ProfileService(path=LOG_PROFILES_PATH, find_container=find_container)


def _load_log_profiles() -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    return _profile_service().load()


def docker_get(path: str, params: dict[str, str] | None = None) -> httpx.Response:
    """Perform a GET request through the read-only Docker proxy."""
    return DOCKER_CLIENT.get(path, params)


def docker_containers() -> list[dict[str, Any]]:
    """
    Return every container exposed by the Docker proxy.
    """

    try:
        result = docker_get("/containers/json", params={"all": "true"}).json()

    except ValueError as exception:
        raise ToolError("Docker returned an invalid JSON response.") from exception

    if not isinstance(result, list):
        raise ToolError("Docker returned an unexpected response.")

    return result








def managed_containers(force_refresh: bool = False) -> list[dict[str, Any]]:
    return CONTAINER_REGISTRY.managed(force_refresh)


def all_containers() -> list[dict[str, Any]]:
    return managed_containers()


def managed_container_names() -> set[str]:
    return CONTAINER_REGISTRY.names()


def expected_stopped_container_names() -> set[str]:
    return CONTAINER_REGISTRY.expected_stopped_names()






def find_container(name: str) -> dict[str, Any]:
    return CONTAINER_REGISTRY.find(name)


def _resolve_container_log_profile(container_name: str, profiles: Any) -> dict[str, Any]:
    return _profile_service().resolve(container_name, profiles)


def _system_service() -> SystemService:
    return SystemService(
        disk_probe_path=DISK_PROBE_PATH, server_name=SERVER_NAME,
        get_disk=disk_snapshot, all_containers=all_containers,
        managed_names=managed_container_names, system=psutil,
    )


def disk_snapshot() -> dict[str, Any]:
    return _system_service().disk_snapshot()


def _get_server_status() -> dict[str, Any]:
    return _system_service().server_status()


@mcp.tool()
def get_server_status() -> dict[str, Any]:
    """Return system and Docker status."""
    return _get_server_status()


def _get_storage_usage() -> dict[str, Any]:
    """Return filesystem usage for the monitored MCP stack. This tool does not read or list files."""

    return disk_snapshot()


@mcp.tool()
def get_storage_usage() -> dict[str, Any]:
    """Return monitored filesystem usage."""
    return _get_storage_usage()


@mcp.tool()
def list_managed_services() -> list[dict[str, Any]]:
    """List managed services with metadata, log profiles, and Docker state."""
    return CONTAINER_REGISTRY.list_services(_load_log_profiles, _resolve_container_log_profile)


def _container_service() -> ContainerService:
    return ContainerService(
        find_container=find_container, docker_get=docker_get,
        get_bounded=DOCKER_CLIENT.get_bounded, all_containers=all_containers,
        managed_container_names=managed_container_names, container_names=container_names,
        max_lines=MAX_LOG_LINES, max_chars=MAX_LOG_CHARS,
    )


def _list_containers() -> list[dict[str, Any]]:
    return _container_service().list_containers()


@mcp.tool()
def list_containers() -> list[dict[str, Any]]:
    """List managed containers."""
    return _list_containers()


def _inspect_container(container_name: str) -> dict[str, Any]:
    return _container_service().inspect(container_name)


@mcp.tool()
def inspect_container(container_name: str) -> dict[str, Any]:
    """Inspect a managed container."""
    return _inspect_container(container_name)


def _get_container_stats(container_name: str) -> dict[str, Any]:
    return _container_service().stats(container_name)


@mcp.tool()
def get_container_stats(container_name: str) -> dict[str, Any]:
    """Return managed container statistics."""
    return _get_container_stats(container_name)


def _baseline_timestamp_to_since(
    value: Any, overlap_seconds: int = 2
) -> tuple[int | None, list[str]]:
    """Convert the stored ISO 8601 baseline timestamp to a Unix timestamp. Apply a small overlap to reduce boundary-event loss."""

    warnings: list[str] = []

    if (
        isinstance(overlap_seconds, bool)
        or not isinstance(overlap_seconds, int)
        or overlap_seconds < 0
    ):
        raise ToolError("Log overlap must be a non-negative integer.")

    if value is None:
        return (None, warnings)

    if not isinstance(value, str):
        warnings.append("The baseline date is not a valid ISO 8601 string.")
        return (None, warnings)

    normalized = value.strip()

    if not normalized:
        warnings.append("The baseline date is empty.")
        return (None, warnings)

    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)

    except ValueError:
        warnings.append("Could not interpret last_run as ISO 8601.")
        return (None, warnings)

    if parsed.tzinfo is None:
        warnings.append(
            "The baseline date has no "
            "timezone; it will not be used for "
            "differential log queries."
        )
        return (None, warnings)

    timestamp = int(parsed.timestamp())
    since = max(0, (timestamp - overlap_seconds))
    return (since, warnings)


def _get_container_logs_data(container_name: str, lines: int, since: int | None = None) -> dict[str, Any]:
    return _container_service().logs(container_name, lines, since)


@mcp.tool()
def get_container_logs(container_name: str, lines: int = 100) -> dict[str, Any]:
    """Return bounded logs for a managed container."""
    return _get_container_logs_data(container_name, lines)


def _ollama_analyzer() -> OllamaAnalyzer:
    return OllamaAnalyzer(
        api_base=OLLAMA_API_BASE,
        model=_provider_model(),
        timeout_seconds=OLLAMA_TIMEOUT_SECONDS,
        max_input_chars=OLLAMA_MAX_INPUT_CHARS,
        http=httpx,
        think=SETTINGS.ollama_think,
        temperature=SETTINGS.ollama_temperature,
    )


def _provider_model() -> str:
    return OLLAMA_MODEL if SETTINGS.analysis_provider == "ollama" else SETTINGS.analysis_provider


def _analysis_provider() -> AnalysisProvider:
    """Select the configured analysis implementation."""
    return ANALYSIS_PROVIDER


def _analyze_logs_with_ollama(
    container_name: str, logs: str
) -> tuple[dict[str, Any], bool]:
    try:
        return _analysis_provider().analyze_logs(container_name, logs)
    except AnalysisError as exception:
        raise ToolError(f"[{failure_kind(exception)}] {exception}") from exception


def _interpret_relevant_events_with_ollama(
    log_activity: dict[str, Any],
) -> dict[str, Any]:
    try:
        return _analysis_provider().interpret_events(log_activity)
    except AnalysisError as exception:
        raise ToolError(f"[{failure_kind(exception)}] {exception}") from exception


def _diagnose_with_ollama(snapshot: dict[str, Any]) -> dict[str, Any]:
    try:
        return _analysis_provider().diagnose(snapshot)
    except AnalysisError as exception:
        raise ToolError(f"[{failure_kind(exception)}] {exception}") from exception


def _analyze_container_logs(container_name: str, lines: int = 100) -> dict[str, Any]:
    """Analyze logs from a managed container with the configured analysis provider. Returns severity, summary, evidence, probable causes, recommended actions, and confidence. This tool does not execute commands or modify containers."""

    logs_data = _get_container_logs_data(container_name, lines)
    logs = str(logs_data["logs"])

    if not logs.strip():
        return {
            "name": logs_data["container_name"],
            "model": _provider_model(),
            "requested_lines": lines,
            "logs_truncated": logs_data["truncated"],
            "input_truncated": False,
            "severity": "no_indication",
            "summary": "The container did not return logs.",
            "evidence": [],
            "probable_causes": [],
            "recommended_actions": [],
            "confidence": 1.0,
        }

    analysis, input_truncated = _analyze_logs_with_ollama(
        logs_data["container_name"], logs
    )
    return {
        "name": logs_data["container_name"],
        "model": _provider_model(),
        "requested_lines": lines,
        "logs_truncated": logs_data["truncated"],
        "input_truncated": input_truncated,
        **analysis,
    }


@mcp.tool()
def analyze_container_logs(container_name: str, lines: int = 100) -> dict[str, Any]:
    """Analyze container logs using the configured provider."""
    return _analyze_container_logs(container_name, lines)


def _filter_profile_noise(container_name: str, value: str) -> tuple[str, int, list[str]]:
    return _profile_service().filter_noise(container_name, value)


def _observation_collector() -> ObservationCollector:
    return ObservationCollector(
        inspect_container=_inspect_container, get_stats=_get_container_stats,
        expected_stopped_names=expected_stopped_container_names,
        managed_names=managed_container_names, disk_snapshot=disk_snapshot,
        expected_names=lambda: set(SETTINGS.expected_container_names),
        normalize_fingerprints=_normalize_event_fingerprint_store,
        load_profiles=_load_log_profiles, baseline_to_since=_baseline_timestamp_to_since,
        get_logs=_get_container_logs_data, resolve_profile=_resolve_container_log_profile,
        get_log_page=lambda name, since, until, cursor: _container_service().log_page(
            name, since, until, cursor, DOCKER_CLIENT.iter_bytes,
            spool=_log_spool()
        ),
        managed_log_ids=lambda: {
            name: container["Id"]
            for container in managed_containers(force_refresh=True)
            for name in container_names(container)
        },
        objective_snapshot=_objective_container_snapshot, docker_workers=DOCKER_WORKERS,
        baseline_version=BASELINE_VERSION, max_log_lines=MAX_LOG_LINES,
    )


def _objective_container_snapshot(container_name: str) -> dict[str, Any]:
    return _observation_collector().snapshot(container_name)




def _collect_objective_server_state(baseline: dict[str, Any], baseline_exists: bool) -> dict[str, Any]:
    return _observation_collector().collect_state(baseline, baseline_exists)




def _safe_interpret_relevant_events(log_activity: dict[str, Any]) -> dict[str, Any]:
    """Run auxiliary model interpretation without allowing provider failures to affect objective state or baseline persistence."""

    try:
        return _interpret_relevant_events_with_ollama(log_activity)

    except ToolError as exception:
        return {
            "model": _provider_model(),
            "executed": False,
            "reason": "model_error",
            "error": str(exception),
            "summary": (
                "Relevant events were collected but could not be interpreted by the analysis provider."
            ),
            "findings": [],
            "discarded_findings": 0,
            "limitations": [("Automatic interpretation was unavailable.")],
            "confidence": 0.0,
        }




def _collect_new_container_logs(baseline: dict[str, Any], baseline_exists: bool) -> dict[str, Any]:
    # A pending report can reference snapshots outside the committed checkpoint.
    if baseline.get("delivery") is None:
        _log_spool().cleanup(baseline.get("log_progress"),
                             [gap["progress"] for gap in baseline.get("log_gaps", [])])
    return _observation_collector().collect_logs(baseline, baseline_exists)


def _run_objective_server_cycle() -> dict[str, Any]:
    """Run a complete observation cycle: load the previous baseline, collect objective state and new events, then persist the next baseline only after successful collection."""

    with _baseline_store().cycle_lock():
        return _run_locked_objective_server_cycle()


def _run_locked_objective_server_cycle(save_baseline=None) -> dict[str, Any]:
    return ObservationService(
        load_baseline=_load_baseline,
        collect_objective_server_state=_collect_objective_server_state,
        collect_new_container_logs=_collect_new_container_logs,
        safe_interpret_relevant_events=_safe_interpret_relevant_events,
        normalize_event_fingerprint_store=_normalize_event_fingerprint_store,
        save_baseline=save_baseline or _save_baseline,
    ).run()


def _diagnostic_service() -> DiagnosticService:
    return DiagnosticService(
        find_container=find_container, docker_get=docker_get,
        get_logs=_get_container_logs_data, filter_profile_noise=_filter_profile_noise,
        expected_stopped_names=expected_stopped_container_names,
        collect_snapshot=_container_snapshot, analyze_snapshot=_diagnose_with_ollama,
        cache=DIAGNOSTIC_CACHE, cache_ttl=DIAGNOSTIC_CACHE_TTL_SECONDS, model=_provider_model(),
    )


def _container_snapshot(container_name: str, lines: int) -> dict[str, Any]:
    return _diagnostic_service().snapshot(container_name, lines)


def _diagnose_container(container_name: str, lines: int = 100) -> dict[str, Any]:
    return _diagnostic_service().diagnose(container_name, lines)


@mcp.tool()
def diagnose_container(container_name: str, lines: int = 100) -> dict[str, Any]:
    """Diagnose a managed container."""
    return _diagnose_container(container_name, lines)


def _generate_server_report(lines_per_container: int = 50, acknowledged_delivery: bool = False) -> dict[str, Any]:
    def build(save_baseline):
        return ReportService(
            server_name=SERVER_NAME, max_log_lines=MAX_LOG_LINES,
            run_cycle=lambda: _run_locked_objective_server_cycle(save_baseline),
            render_report=_report_markdown,
        ).generate(lines_per_container)
    return ReportDelivery(_baseline_store()).generate(build, acknowledged_delivery)


@mcp.tool()
def generate_server_report(lines_per_container: int = 50, acknowledged_delivery: bool = False) -> dict[str, Any]:
    """Generate a differential report. Opt into durable replay until acknowledge_server_report confirms delivery.id. One pending report is shared by all clients; process it before acknowledging. Legacy delivery saves before returning and may lose a response."""
    return _generate_server_report(lines_per_container, acknowledged_delivery)


@mcp.tool()
def acknowledge_server_report(delivery_id: str) -> dict[str, Any]:
    """Confirm processing of a durable report and commit its checkpoint. Retry of the most recent acknowledgement is idempotent. This does not modify Docker."""
    return ReportDelivery(_baseline_store()).acknowledge(delivery_id)


@mcp.tool()
def close_incomplete_log_interval(interval_id: str, reason: str) -> dict[str, Any]:
    """Explicitly accept lost log coverage for the current interval and resume future collection. Process and acknowledge pending delivery first. Requires baseline.interval_id and a reason. Retains captured snapshots and an audit record. Retry of the same interval is idempotent. Does not modify Docker."""
    return close_interval(_baseline_store(), interval_id, reason)


def _get_server_inventory() -> str:
    """Describe MCP capabilities and limitations."""
    return json.dumps(
        {
            "server": SERVER_NAME,
            "mode": "read_only",
            "managed_containers": sorted(managed_container_names()),
            "capabilities": [
                "system status",
                "storage usage",
                "container listing",
                "container inspection",
                "container statistics",
                "bounded log access",
                "model log analysis",
                "container diagnostics",
                "server reports",
            ],
            "blocked_capabilities": [
                "command execution",
                "starting containers",
                "stopping containers",
                "restarting containers",
                "creating Docker resources",
                "deleting Docker resources",
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("server://inventory")
def server_inventory() -> str:
    """Describe server capabilities."""
    return _get_server_inventory()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
