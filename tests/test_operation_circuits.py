"""Invalid output is local; transport failures are shared and recovery bounded."""
import threading
import unittest
from copy import deepcopy

from shadow.analysis import AnalysisTimeout, AnalysisValidationError
from shadow.providers import ManagedProvider, CircuitOpen
from test_shared_analysis import IndependentAdapter, SNAPSHOT, ACTIVITY, DIAGNOSTIC_RESULT


class OperationCircuitTests(unittest.TestCase):
    def test_invalid_diagnostics_do_not_block_events_or_logs(self):
        backend = IndependentAdapter()
        backend.diagnostic["confidence"] = 2
        provider = ManagedProvider(backend, threshold=2)
        for _ in range(2):
            with self.assertRaises(AnalysisValidationError): provider.diagnose(SNAPSHOT)
        with self.assertRaises(CircuitOpen): provider.diagnose(SNAPSHOT)
        provider.interpret_events(ACTIVITY)
        provider.analyze_logs("web", "warning")
        status = provider.status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["transport"]["state"], "closed")
        self.assertEqual(status["operations"]["diagnose"]["state"], "open")
        self.assertEqual(status["metrics"]["diagnose"]["attempts"], 2)
        self.assertEqual(status["metrics"]["diagnose"]["circuit_rejections"], 1)
        self.assertEqual(status["metrics"]["diagnose"]["invalid_response"], 2)
        status["metrics"]["diagnose"]["attempts"] = 999
        self.assertEqual(provider.status()["metrics"]["diagnose"]["attempts"], 2)

    def test_invalid_response_heals_transport_but_not_its_own_operation(self):
        now = [0]
        backend = IndependentAdapter()
        backend.error = AnalysisTimeout("timeout")
        provider = ManagedProvider(backend, threshold=1, cooldown=10, clock=lambda: now[0])
        with self.assertRaises(AnalysisTimeout): provider.analyze_logs("web", "warning")
        with self.assertRaises(CircuitOpen): provider.interpret_events(ACTIVITY)
        now[0] = 10
        backend.error = None
        backend.diagnostic["confidence"] = 2
        with self.assertRaises(AnalysisValidationError): provider.diagnose(SNAPSHOT)
        provider.interpret_events(ACTIVITY)
        self.assertEqual(provider.status()["transport"]["state"], "closed")
        self.assertEqual(provider.status()["transport_recoveries"], 1)
        backend.diagnostic = deepcopy(DIAGNOSTIC_RESULT)
        now[0] = 20
        provider.diagnose(SNAPSHOT)
        self.assertEqual(provider.status()["state"], "closed")
        self.assertEqual(provider.status()["metrics"]["diagnose"]["recoveries"], 1)

    def test_metrics_record_elapsed_time_and_never_store_input(self):
        now = [0]
        class Slow(IndependentAdapter):
            def analyze_logs(self, name, logs):
                now[0] += 3.5
                return super().analyze_logs(name, logs)
        provider = ManagedProvider(Slow(), clock=lambda: now[0])
        provider.analyze_logs("web", "warning token=synthetic-secret")
        metrics = provider.status()["metrics"]["analyze_logs"]
        self.assertEqual(metrics["last_seconds"], 3.5)
        self.assertEqual(metrics["total_seconds"], 3.5)
        self.assertEqual(metrics["successes"], 1)
        self.assertNotIn("synthetic-secret", str(provider.status()))

    def test_no_event_cycle_cannot_masquerade_as_transport_recovery(self):
        now = [0]
        backend = IndependentAdapter()
        backend.error = AnalysisTimeout("timeout")
        provider = ManagedProvider(backend, threshold=1, cooldown=10, clock=lambda: now[0])
        with self.assertRaises(AnalysisTimeout): provider.diagnose(SNAPSHOT)
        result = provider.interpret_events({"containers": []})
        self.assertFalse(result["executed"])
        self.assertEqual(provider.status()["state"], "open")
        now[0] = 10
        provider.interpret_events({"containers": []})
        self.assertEqual(provider.status()["state"], "half_open")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(provider.status()["metrics"]["interpret_events"]["skipped"], 2)
        self.assertEqual(provider.status()["transport_recoveries"], 0)
