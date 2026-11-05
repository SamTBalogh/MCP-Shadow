"""Reusable behavioral checks for every AnalysisProvider implementation.

Subclass this mixin and implement make_provider(fail=False). The failure mode
must simulate an unavailable provider and raise AnalysisError for requests.
"""
from copy import deepcopy

from shadow.analysis import AnalysisError, AnalysisProvider
from shadow.diagnostics import DiagnosticService
from mcp.server.fastmcp.exceptions import ToolError


class ProviderContract:
    def make_provider(self, fail=False):
        raise NotImplementedError

    def test_analysis_has_the_common_schema(self):
        provider = self.make_provider()
        self.assertIsInstance(provider, AnalysisProvider)
        result, truncated = provider.analyze_logs("web", "warning")
        self.assertIsInstance(truncated, bool)
        self.assertIn(result["severity"], {"no_indication", "low", "medium", "high", "critical"})
        self.assertIsInstance(result["summary"], str)
        for field in ("evidence", "probable_causes", "recommended_actions"):
            self.assertIsInstance(result[field], list)
            self.assertTrue(all(isinstance(value, str) for value in result[field]))
        self.assertIsInstance(result["confidence"], (int, float))
        self.assertFalse(isinstance(result["confidence"], bool))
        self.assertTrue(0 <= result["confidence"] <= 1)

    def test_event_interpretation_has_the_common_schema(self):
        provider = self.make_provider()
        activity = {"containers": [{"name": "web", "relevant_event_details": [{
            "message": "warning", "content": "warning", "relevance": {"relevant": True},
        }]}]}
        before = deepcopy(activity)
        result = provider.interpret_events(activity)
        self.assertEqual(activity, before)
        self.assertIsInstance(result["executed"], bool)
        self.assertIsInstance(result["model"], str)
        self.assertIsInstance(result["reason"], str)
        self.assertIsInstance(result["findings"], list)
        self.assertTrue(all(isinstance(item, dict) for item in result["findings"]))
        self.assertTrue(all(all(isinstance(key, str) and isinstance(value, str)
                                for key, value in item.items()) for item in result["findings"]))
        self.assertIsInstance(result["limitations"], list)
        self.assertTrue(all(isinstance(item, str) for item in result["limitations"]))
        self.assertIsInstance(result["summary"], str)
        self.assertFalse(isinstance(result["confidence"], bool))
        self.assertTrue(0 <= result["confidence"] <= 1)

    def test_diagnostic_is_advisory_and_does_not_modify_snapshot(self):
        provider = self.make_provider()
        snapshot = {"name": "web", "logs": "warning", "objective_data": {},
                    "log_indicators": {}, "objective_findings": [], "objective_severity": "high"}
        before = deepcopy(snapshot)
        result = provider.diagnose(snapshot)
        self.assertEqual(snapshot, before)
        self.assertIn(result["model_severity"], {"no_indication", "low", "medium", "high", "critical"})
        self.assertIsInstance(result["findings"], list)
        self.assertTrue(all(isinstance(item, dict) for item in result["findings"]))
        self.assertTrue(all(all(isinstance(key, str) and isinstance(value, str)
                                for key, value in item.items()) for item in result["findings"]))
        self.assertIsInstance(result["model"], str)
        self.assertIsInstance(result["summary"], str)
        self.assertIsInstance(result["limitations"], list)
        self.assertTrue(all(isinstance(item, str) for item in result["limitations"]))
        self.assertIsInstance(result["ai_input_truncated"], bool)
        self.assertFalse(isinstance(result["confidence"], bool))
        self.assertTrue(0 <= result["confidence"] <= 1)

    def test_unavailable_provider_raises_common_error(self):
        with self.assertRaises(AnalysisError):
            self.make_provider(fail=True).analyze_logs("web", "warning")

        activity = {"containers": [{"name": "web", "relevant_event_details": [{
            "message": "warning", "content": "warning", "relevance": {"relevant": True},
        }]}]}
        with self.assertRaises(AnalysisError):
            self.make_provider(fail=True).interpret_events(activity)

        snapshot = {"name": "web", "logs": "warning", "objective_data": {},
                    "log_indicators": {}, "objective_findings": [], "objective_severity": "high"}
        with self.assertRaises(AnalysisError):
            self.make_provider(fail=True).diagnose(snapshot)

    def test_provider_success_and_failure_cannot_change_objective_severity(self):
        for fail in (False, True):
            provider = self.make_provider(fail=fail)
            def analyze(snapshot):
                try:
                    return provider.diagnose(snapshot)
                except AnalysisError as error:
                    raise ToolError(str(error)) from error
            for severity in ("no_indication", "high", "critical"):
                snapshot = {"name": "web", "logs": "warning", "objective_data": {},
                            "log_indicators": {}, "objective_findings": [],
                            "objective_severity": severity, "logs_truncated": False}
                service = DiagnosticService(find_container=None, docker_get=None, get_logs=None,
                    filter_profile_noise=None, expected_stopped_names=None,
                    collect_snapshot=lambda *_: snapshot, analyze_snapshot=analyze,
                    cache={}, cache_ttl=0, model="test")
                with self.subTest(fail=fail, severity=severity):
                    result = service.diagnose("web")
                    self.assertEqual(result["objective_severity"], severity)
                    self.assertEqual(result["final_severity"], severity)
                    if fail:
                        self.assertIsNone(result["model_severity_advisory"])

    def test_common_boundary_rejects_corrupted_adapter_results(self):
        """Apply the same rejection rules around every adapter under contract."""
        from shadow.analysis import AnalysisValidationError
        from shadow.validation import ValidatedProvider
        activity = {"containers": [{"name": "web", "relevant_event_details": [{
            "message": "warning", "content": "warning", "relevance": {"relevant": True}}]}]}
        snapshot = {"name": "web", "logs": "warning", "objective_data": {},
                    "log_indicators": {}, "objective_findings": [], "objective_severity": "high"}
        provider = self.make_provider()
        class CorruptingAdapter:
            def analyze_logs(self, *args):
                result, truncated = provider.analyze_logs(*args)
                return {**result, "evidence": ["fabricated excerpt"]}, truncated
            def interpret_events(self, *args):
                return {**provider.interpret_events(*args), "summary": " "}
            def diagnose(self, *args):
                return {**provider.diagnose(*args), "model_severity": "high", "findings": []}
        boundary = ValidatedProvider(CorruptingAdapter())
        for operation, args in ((boundary.analyze_logs, ("web", "warning")),
                                (boundary.interpret_events, (activity,)),
                                (boundary.diagnose, (snapshot,))):
            with self.subTest(operation=operation.__name__), self.assertRaises(AnalysisValidationError):
                operation(*args)
