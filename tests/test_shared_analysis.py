"""Independent adapters, common validation, immutable facts and breaker recovery."""
import importlib
import json
import threading
import types
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from shadow.analysis import AnalysisError, AnalysisTimeout, AnalysisUnavailable, AnalysisValidationError
from shadow.diagnostics import DiagnosticService
from shadow.providers import CircuitOpen, ManagedProvider, create_provider
from shadow.settings import Settings, SettingsError
from shadow.validation import ValidatedProvider, diagnostic_sources
from test_analysis_quality import analyzer_for

LOG_RESULT = {"severity": "low", "summary": "Warning observed.", "evidence": ["warning"],
              "probable_causes": [], "recommended_actions": [], "confidence": 0.5}
SNAPSHOT = {"name": "web", "logs": "warning", "objective_data": {"running": False, "memory": {"percent": 95}},
            "log_indicators": {}, "objective_findings": [], "objective_severity": "high", "logs_truncated": False}
ACTIVITY = {"containers": [{"name": "web", "relevant_event_details": [{
    "message": "warning", "content": "warning", "relevance": {"relevant": True}}]}]}
EVENT_RESULT = {"model": "independent", "executed": True, "reason": "relevant_events", "summary": "Warning observed.",
    "findings": [{"title": "Warning", "container": "web", "evidence": "warning", "interpretation": "Uncertain impact.",
                  "recommendation": "Review context."}], "limitations": [], "confidence": 0.5}
DIAGNOSTIC_RESULT = {"model": "independent", "model_severity": "low", "summary": "Warning observed.", "ai_input_truncated": False,
    "findings": [{"title": "Warning", "source": "logs", "evidence": "warning", "probable_cause": "Unknown cause.",
                  "recommendation": "Review context."}], "limitations": [], "confidence": 0.5}


class IndependentAdapter:
    """Test adapter, deliberately independent of Ollama's response handling."""
    def __init__(self):
        self.result = deepcopy(LOG_RESULT)
        self.event = deepcopy(EVENT_RESULT)
        self.diagnostic = deepcopy(DIAGNOSTIC_RESULT)
        self.error = None
        self.calls = 0

    def _get(self, result):
        self.calls += 1
        if self.error:
            raise self.error
        return deepcopy(result)

    def analyze_logs(self, name, logs): return self._get(self.result), False
    def interpret_events(self, activity): return self._get(self.event)
    def diagnose(self, snapshot): return self._get(self.diagnostic)
    def availability(self): return "ok"


class SharedValidationTests(unittest.TestCase):
    def test_independent_adapter_rejects_invalid_results_for_all_operations(self):
        for operation, args, attribute in [("analyze_logs", ("web", "warning"), "result"),
                ("interpret_events", (ACTIVITY,), "event"), ("diagnose", (SNAPSHOT,), "diagnostic")]:
            for change in ({"summary": " "}, {"confidence": 2}, {"confidence": True}, {"confidence": float("nan")},
                           {"summary": None}):
                backend = IndependentAdapter()
                getattr(backend, attribute).update(change)
                with self.subTest(operation=operation, change=change), self.assertRaises(AnalysisValidationError):
                    getattr(ValidatedProvider(backend), operation)(*deepcopy(args))

    def test_invented_or_incomplete_findings_are_errors_even_when_severity_is_clear(self):
        for attribute, operation, args in [("event", "interpret_events", (ACTIVITY,)),
                                         ("diagnostic", "diagnose", (SNAPSHOT,))]:
            for finding in ({"evidence": "warning"}, {**DIAGNOSTIC_RESULT["findings"][0], "evidence": "invented"}):
                backend = IndependentAdapter()
                getattr(backend, attribute)["findings"] = [finding]
                if attribute == "diagnostic": backend.diagnostic["model_severity"] = "no_indication"
                with self.subTest(attribute=attribute, finding=finding), self.assertRaises(AnalysisValidationError):
                    getattr(ValidatedProvider(backend), operation)(*deepcopy(args))

    def test_event_container_and_diagnostic_source_must_match(self):
        backend = IndependentAdapter()
        backend.event["findings"][0]["container"] = "invented"
        with self.assertRaises(AnalysisValidationError): ValidatedProvider(backend).interpret_events(ACTIVITY)
        backend.diagnostic["findings"][0]["source"] = "state"
        with self.assertRaises(AnalysisValidationError): ValidatedProvider(backend).diagnose(SNAPSHOT)
        backend.diagnostic["findings"][0].update(source="state", evidence='"running": false')
        self.assertEqual(ValidatedProvider(backend).diagnose(SNAPSHOT)["model_severity"], "low")

    def test_ollama_and_independent_adapter_share_finding_rejection(self):
        for answer, operation, args in [(EVENT_RESULT, "interpret_events", (ACTIVITY,)),
                                        (DIAGNOSTIC_RESULT, "diagnose", (SNAPSHOT,))]:
            invalid = deepcopy(answer)
            invalid["findings"][0]["evidence"] = "fabricated"
            with self.subTest(operation=operation), self.assertRaises(AnalysisValidationError):
                getattr(analyzer_for(invalid), operation)(*args)

    def test_adapter_cannot_mutate_facts_and_all_input_strings_are_redacted(self):
        seen = []
        class Mutating(IndependentAdapter):
            def diagnose(self, snapshot):
                seen.append(deepcopy(snapshot))
                snapshot["objective_severity"] = "no_indication"
                snapshot["objective_data"].clear()
                return super().diagnose(snapshot)
        snapshot = deepcopy(SNAPSHOT)
        snapshot["objective_data"]["state_error"] = "token=synthetic-secret"
        before = deepcopy(snapshot)
        ValidatedProvider(Mutating()).diagnose(snapshot)
        self.assertEqual(snapshot, before)
        self.assertNotIn("synthetic-secret", json.dumps(seen))

    def test_every_provider_outcome_keeps_objective_severity(self):
        for objective in ("no_indication", "low", "high", "critical"):
            for model in ("no_indication", "low", "critical", "invalid", "timeout", "unavailable"):
                backend = IndependentAdapter()
                backend.diagnostic["model_severity"] = model
                if model == "timeout": backend.error = AnalysisTimeout("timed out")
                if model == "unavailable": backend.error = AnalysisUnavailable("offline")
                provider = ManagedProvider(backend, threshold=1)
                snapshot = {**SNAPSHOT, "objective_severity": objective}
                def analyze(value):
                    try: return provider.diagnose(value)
                    except AnalysisError as error: raise ToolError(str(error)) from error
                from mcp.server.fastmcp.exceptions import ToolError
                service = DiagnosticService(find_container=None, docker_get=None, get_logs=None,
                    filter_profile_noise=None, expected_stopped_names=None, collect_snapshot=lambda *_: deepcopy(snapshot),
                    analyze_snapshot=analyze, cache={}, cache_ttl=0, model="independent")
                with self.subTest(objective=objective, model=model):
                    for _ in range(2):
                        result = service.diagnose("web")
                        self.assertEqual(result["final_severity"], objective)
                        self.assertEqual(result["objective_severity"], objective)


class CircuitTests(unittest.TestCase):
    def test_threshold_cooldown_failed_probe_and_successful_recovery(self):
        now = [100.0]
        backend = IndependentAdapter()
        backend.error = AnalysisTimeout("timed out")
        provider = ManagedProvider(backend, threshold=2, cooldown=10, clock=lambda: now[0])
        with self.assertRaises(AnalysisTimeout): provider.analyze_logs("web", "warning")
        with self.assertRaises(AnalysisTimeout): provider.diagnose(SNAPSHOT)
        self.assertEqual(provider.status()["state"], "open")
        for _ in range(3):
            with self.assertRaises(CircuitOpen): provider.interpret_events(ACTIVITY)
        self.assertEqual(backend.calls, 2)
        now[0] += 10
        self.assertEqual(provider.status()["state"], "half_open")
        with self.assertRaises(AnalysisTimeout): provider.interpret_events(ACTIVITY)
        self.assertEqual(provider.status()["state"], "open")
        now[0] += 10
        backend.error = None
        provider.interpret_events(ACTIVITY)
        self.assertEqual(provider.status()["state"], "closed")
        self.assertEqual(provider.status()["consecutive_failures"], 0)

    def test_only_one_recovery_probe_is_admitted_under_concurrency(self):
        entered, release = threading.Event(), threading.Event()
        backend = IndependentAdapter()
        provider = ManagedProvider(backend, threshold=1, cooldown=1, clock=lambda: 10)
        provider.failures, provider.retry_at = 1, 9
        def slow(name, logs):
            entered.set()
            release.wait(5)
            return deepcopy(LOG_RESULT), False
        backend.analyze_logs = slow
        results = []
        thread = threading.Thread(target=lambda: results.append(provider.analyze_logs("web", "warning")))
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            for _ in range(10):
                with self.assertRaisesRegex(AnalysisUnavailable, "in flight"):
                    provider.diagnose(SNAPSHOT)
            self.assertEqual(provider.status()["consecutive_failures"], 1)
        finally:
            release.set(); thread.join(5)
        self.assertEqual(len(results), 1)
        self.assertEqual(provider.status()["state"], "closed")

    def test_invalid_response_opens_circuit_and_success_resets_failures(self):
        backend = IndependentAdapter()
        provider = ManagedProvider(backend, threshold=2)
        backend.result["evidence"] = ["invented"]
        with self.assertRaises(AnalysisValidationError): provider.analyze_logs("web", "warning")
        self.assertEqual(provider.status()["last_failure"], "invalid_response")
        backend.result = deepcopy(LOG_RESULT)
        provider.analyze_logs("web", "warning")
        self.assertEqual(provider.status()["consecutive_failures"], 0)

    def test_custom_factory_and_disabled_provider_need_no_server_changes(self):
        module = types.ModuleType("test_custom_adapter")
        module.make = lambda **kwargs: IndependentAdapter()
        with patch.dict("sys.modules", {"test_custom_adapter": module}):
            provider = create_provider(Settings.from_env({"ANALYSIS_PROVIDER": "test_custom_adapter:make"}), httpx)
            self.assertEqual(provider.availability(), "ok")
            provider.analyze_logs("web", "warning")
        disabled = create_provider(Settings.from_env({"ANALYSIS_PROVIDER": "disabled"}), httpx)
        self.assertEqual(disabled.availability(), "disabled")
        with self.assertRaises(AnalysisUnavailable): disabled.diagnose(SNAPSHOT)
        with self.assertRaises(SettingsError): create_provider(Settings.from_env({"ANALYSIS_PROVIDER": "missing_module:make"}), httpx)

    def test_new_configuration_is_validated_at_startup(self):
        for name, value in [("ANALYSIS_PROVIDER", "unknown"), ("ANALYSIS_PROVIDER", "x:y:z"),
                ("ANALYSIS_FAILURE_THRESHOLD", "0"), ("ANALYSIS_FAILURE_THRESHOLD", "1.2"),
                ("ANALYSIS_COOLDOWN_SECONDS", "nan"), ("ANALYSIS_COOLDOWN_SECONDS", "601"),
                ("LOG_SPOOL_MAX_BYTES", "0")]:
            with self.subTest(name=name, value=value), self.assertRaisesRegex(SettingsError, name):
                Settings.from_env({name: value})


class EvidenceNormalizationTests(unittest.TestCase):
    def test_wrapped_event_and_diagnostic_quotes_are_normalized_before_delivery(self):
        for operation in ("interpret_events", "diagnose"):
            backend = IndependentAdapter()
            result = backend.event if operation == "interpret_events" else backend.diagnostic
            result["findings"][0]["evidence"] = '"warning"'
            args = (ACTIVITY,) if operation == "interpret_events" else (SNAPSHOT,)
            normalized = getattr(ValidatedProvider(backend), operation)(*args)
            self.assertEqual(normalized["findings"][0]["evidence"], "warning")
            result["findings"][0]["evidence"] = '"fabricated"'
            with self.assertRaises(AnalysisValidationError): getattr(ValidatedProvider(backend), operation)(*args)
