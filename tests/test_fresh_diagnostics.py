"""Objective observations must not be served from the model cache."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from shadow.diagnostics import DiagnosticService
from test_shared_analysis import SNAPSHOT, DIAGNOSTIC_RESULT


class FreshDiagnosticTests(unittest.TestCase):
    def service(self, snapshots, analyze=None, cache=None, model="test"):
        self.observations = 0
        self.generations = 0
        def collect(*_):
            snapshot = snapshots[min(self.observations, len(snapshots) - 1)]
            self.observations += 1
            return deepcopy(snapshot)
        def generate(snapshot):
            self.generations += 1
            if analyze: return analyze(snapshot)
            return deepcopy(DIAGNOSTIC_RESULT)
        return DiagnosticService(find_container=None, docker_get=None, get_logs=None,
            filter_profile_noise=None, expected_stopped_names=None,
            collect_snapshot=collect, analyze_snapshot=generate, cache=cache if cache is not None else {},
            cache_ttl=120, model=model)

    def test_identical_inputs_reuse_only_advisory_and_collect_every_time(self):
        service = self.service([SNAPSHOT])
        first = service.diagnose("web")
        first["model_analysis"]["summary"] = "Client-side mutation"
        second = service.diagnose("web")
        self.assertEqual((self.observations, self.generations), (2, 1))
        self.assertTrue(second["cache"]["hit"])
        self.assertTrue(second["cache"]["objective_data_refreshed"])
        self.assertEqual(second["cache"]["scope"], "model_analysis")
        self.assertNotEqual(second["model_analysis"]["summary"], "Client-side mutation")
        self.assertNotIn("objective_data", str(service.cache.values()))

    def test_new_incident_during_ttl_is_observed_even_if_model_is_now_down(self):
        healthy = {**SNAPSHOT, "objective_severity": "no_indication"}
        incident = {**SNAPSHOT, "objective_severity": "critical", "objective_data": {"oom_killed": True}}
        def analyze(snapshot):
            if snapshot["objective_severity"] == "critical": raise ToolError("provider offline")
            return deepcopy(DIAGNOSTIC_RESULT)
        service = self.service([healthy, incident], analyze)
        self.assertEqual(service.diagnose("web")["final_severity"], "no_indication")
        result = service.diagnose("web")
        self.assertEqual(result["final_severity"], "critical")
        self.assertTrue(result["objective_data"]["oom_killed"])
        self.assertIsNone(result["model_severity_advisory"])
        self.assertFalse(result["cache"]["hit"])
        self.assertEqual(service.cache, {})

    def test_logs_metrics_container_identity_and_profile_changes_invalidate_advice(self):
        for change in ({"logs": "different"}, {"container_id": "replacement"},
                       {"objective_data": {"cpu_percent": 99}}, {"profile_warnings": ["Profile changed"]}):
            service = self.service([SNAPSHOT, {**SNAPSHOT, **change}])
            service.diagnose("web")
            result = service.diagnose("web")
            with self.subTest(change=change):
                self.assertFalse(result["cache"]["hit"])
                self.assertEqual(self.generations, 2)

    def test_provider_identity_expiry_and_legacy_cache_do_not_reuse_wrong_advice(self):
        cache = {"web:100": (0, {"final_severity": "no_indication"})}
        service = self.service([SNAPSHOT], cache=cache)
        with patch("shadow.diagnostics.time.monotonic", return_value=10): service.diagnose("web")
        service.model = "other-provider"
        with patch("shadow.diagnostics.time.monotonic", return_value=11):
            self.assertFalse(service.diagnose("web")["cache"]["hit"])
        with patch("shadow.diagnostics.time.monotonic", return_value=200):
            self.assertFalse(service.diagnose("web")["cache"]["hit"])
        self.assertEqual(self.generations, 3)

    def test_model_cannot_mutate_the_fresh_objective_snapshot(self):
        def mutate(snapshot):
            snapshot["objective_severity"] = "no_indication"
            snapshot["objective_data"].clear()
            return deepcopy(DIAGNOSTIC_RESULT)
        service = self.service([SNAPSHOT], mutate)
        self.assertEqual(service.diagnose("web")["final_severity"], "high")
        self.assertEqual(service.diagnose("web")["objective_data"], SNAPSHOT["objective_data"])

    def test_changed_json_fact_order_cannot_reuse_cross_fact_quote_offsets(self):
        first = {**SNAPSHOT, "objective_data": {"running": False, "health": "unhealthy"}}
        second = {**SNAPSHOT, "objective_data": {"health": "unhealthy", "running": False}}
        service = self.service([first, second])
        service.diagnose("web")
        self.assertFalse(service.diagnose("web")["cache"]["hit"])
        self.assertEqual(self.generations, 2)
