"""Explicit adapter selection and a process-local, single-flight circuit breaker."""
from __future__ import annotations

import importlib
import threading
import time

from shadow.analysis import AnalysisError, AnalysisTimeout, AnalysisUnavailable, AnalysisValidationError
from shadow.validation import ValidatedProvider, event_sources, redact
from shadow.evidence import attach_references


def failure_kind(error):
    if isinstance(error, CircuitOpen):
        return "circuit_open"
    if isinstance(error, AnalysisTimeout):
        return "timeout"
    if isinstance(error, AnalysisValidationError):
        return "invalid_response"
    if isinstance(error, AnalysisUnavailable):
        return "unavailable"
    return "provider_error"


class CircuitOpen(AnalysisUnavailable):
    """Requests are temporarily suppressed after repeated provider failures."""


class ManagedProvider:
    """Global transport protection and per-operation response protection.

    Only one request is in flight, including during recovery. Invalid output
    opens only its operation's circuit; timeouts/unavailability open transport
    protection shared by all operations. Counters contain no inputs or answers.
    """

    OPERATIONS = ("analyze_logs", "interpret_events", "diagnose")

    def __init__(self, provider, threshold=2, cooldown=30, clock=time.monotonic):
        self.backend = provider
        self.validated = ValidatedProvider(provider)
        self.threshold = threshold
        self.cooldown = cooldown
        self.clock = clock
        self.lock = threading.Lock()
        self.failures = 0
        self.retry_at = 0.0
        self.last_failure = None
        self.active = False
        self.operations = {name: {"failures": 0, "retry_at": 0.0, "last_failure": None}
                           for name in self.OPERATIONS}
        self.metrics = {name: {"attempts": 0, "successes": 0, "timeouts": 0,
            "unavailable": 0, "invalid_response": 0, "provider_error": 0,
            "busy_rejections": 0, "circuit_rejections": 0, "recoveries": 0, "skipped": 0,
            "total_seconds": 0.0, "last_seconds": None, "max_seconds": 0.0}
            for name in self.OPERATIONS}
        self.transport_recoveries = 0

    def _state(self, failures, retry_at, last_failure):
        remaining = max(0.0, retry_at - self.clock())
        return {"state": "open" if remaining else "half_open" if retry_at else "closed",
                "consecutive_failures": failures, "retry_after_seconds": round(remaining, 3),
                "last_failure": last_failure}

    def status(self):
        with self.lock:
            transport = self._state(self.failures, self.retry_at, self.last_failure)
            operations = {name: self._state(item["failures"], item["retry_at"], item["last_failure"])
                          for name, item in self.operations.items()}
            state = transport["state"]
            if state == "closed" and any(item["state"] != "closed" for item in operations.values()):
                state = "degraded"
            last_failure = self.last_failure
            if last_failure is None:
                last_failure = next((item["last_failure"] for item in self.operations.values()
                                     if item["last_failure"] is not None), None)
            return {**transport, "state": state, "in_flight": self.active,
                    "last_failure": last_failure, "transport": transport, "operations": operations,
                    "metrics": {name: dict(values) for name, values in self.metrics.items()},
                    "transport_recoveries": self.transport_recoveries}

    def availability(self):
        if isinstance(self.backend, DisabledProvider):
            return "disabled"
        status = self.status()
        if status["transport"]["state"] == "open":
            return "circuit_open"
        if status["in_flight"]:
            return "busy"
        check = getattr(self.backend, "availability", None)
        if check is None:
            return "unknown"
        try:
            result = check()
            return result if isinstance(result, str) and result else "unavailable"
        except Exception:
            return "unavailable"

    def _call(self, operation, *args):
        with self.lock:
            metric = self.metrics[operation]
            local = self.operations[operation]
            for scope, retry_at in (("transport", self.retry_at), (operation, local["retry_at"])):
                remaining = retry_at - self.clock()
                if remaining > 0:
                    metric["circuit_rejections"] += 1
                    raise CircuitOpen(f"Analysis circuit for {scope} is open; retry in {remaining:.1f} seconds.")
            if self.active:
                metric["busy_rejections"] += 1
                raise AnalysisUnavailable("Analysis provider is busy; one request is already in flight.")
            self.active = True
            metric["attempts"] += 1
            started = self.clock()
            transport_probe = bool(self.retry_at)
            operation_probe = bool(local["retry_at"])
        error = None
        try:
            return getattr(self.validated, operation)(*args)
        except AnalysisError as exception:
            error = exception
            raise
        except Exception as exception:
            error = AnalysisValidationError("The analysis adapter failed to honor its result contract.")
            raise error from exception
        finally:
            with self.lock:
                self.active = False
                elapsed = max(0.0, self.clock() - started)
                metric["total_seconds"] += elapsed
                metric["last_seconds"] = elapsed
                metric["max_seconds"] = max(metric["max_seconds"], elapsed)
                if isinstance(self.backend, DisabledProvider):
                    metric["unavailable"] += 1
                elif error is None or isinstance(error, AnalysisValidationError):
                    # A returned response proves transport recovery even if its
                    # content is invalid. Only a valid response heals an operation.
                    if transport_probe:
                        self.transport_recoveries += 1
                    self.failures, self.retry_at, self.last_failure = 0, 0.0, None
                    if error is None:
                        metric["successes"] += 1
                        if operation_probe:
                            metric["recoveries"] += 1
                        local.update(failures=0, retry_at=0.0, last_failure=None)
                    else:
                        metric["invalid_response"] += 1
                        local["failures"] += 1
                        local["last_failure"] = "invalid_response"
                        if local["failures"] >= self.threshold:
                            local["retry_at"] = self.clock() + self.cooldown
                else:
                    kind = failure_kind(error)
                    metric[kind if kind in metric else "provider_error"] += 1
                    self.failures += 1
                    self.last_failure = kind
                    if self.failures >= self.threshold or transport_probe:
                        self.retry_at = self.clock() + self.cooldown
                    # A transport failure during a local recovery cannot release
                    # many immediate local probes when transport remains closed.
                    if operation_probe:
                        local["retry_at"] = self.clock() + self.cooldown

    def analyze_logs(self, container_name, logs):
        return self._call("analyze_logs", container_name, logs)

    def interpret_events(self, log_activity):
        # A cycle without relevant events must not heal a transport circuit:
        # no model connection has been exercised by this deterministic skip.
        clean = redact(log_activity)
        if not event_sources(clean):
            with self.lock:
                self.metrics["interpret_events"]["skipped"] += 1
            result = {"model": getattr(self.backend, "model", "configured_provider"),
                      "executed": False, "reason": "no_relevant_events",
                      "summary": "No new events were detected that require interpretation.",
                      "findings": [], "limitations": [], "confidence": 1.0,
                      "events_available": 0, "events_included": 0, "ai_input_truncated": False}
            return attach_references(result, "events", clean)
        return self._call("interpret_events", clean)

    def diagnose(self, snapshot):
        return self._call("diagnose", snapshot)


class DisabledProvider:
    """Explicitly disable optional analysis while keeping objective tools usable."""

    def availability(self):
        return "disabled"

    def analyze_logs(self, *args):
        raise AnalysisUnavailable("Model analysis is disabled by ANALYSIS_PROVIDER.")

    interpret_events = analyze_logs
    diagnose = analyze_logs


def create_provider(settings, http):
    """A custom module:factory receives settings and http, without server edits.

    The explicitly configured factory is trusted Python code. It must return an
    AnalysisProvider and may implement availability() returning a status string.
    """
    from shadow.analysis import AnalysisProvider
    from shadow.ollama import OllamaAnalyzer
    from shadow.settings import SettingsError

    if settings.analysis_provider == "ollama":
        backend = OllamaAnalyzer(settings.ollama_api_base, settings.ollama_model,
            settings.ollama_timeout_seconds, settings.ollama_max_input_chars, http,
            think=settings.ollama_think, temperature=settings.ollama_temperature)
    elif settings.analysis_provider == "disabled":
        backend = DisabledProvider()
    else:
        module_name, factory_name = settings.analysis_provider.split(":")
        try:
            factory = getattr(importlib.import_module(module_name), factory_name)
            backend = factory(settings=settings, http=http)
        except Exception as exception:
            raise SettingsError("ANALYSIS_PROVIDER factory could not be initialized.") from exception
        if not isinstance(backend, AnalysisProvider):
            raise SettingsError("ANALYSIS_PROVIDER factory must return an AnalysisProvider.")
    return ManagedProvider(backend, settings.analysis_failure_threshold, settings.analysis_cooldown_seconds)
