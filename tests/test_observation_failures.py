"""Synthetic regressions for uncertain observations and container replacement."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import count
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from shadow.baseline import BaselineStore
from shadow.analysis import AnalysisValidationError
from shadow.collector import ObservationCollector
from shadow.containers import ContainerService
from shadow.diagnostics import DiagnosticService
from shadow.docker_client import DockerClient
from shadow.observation import ObservationService
from shadow.reporting import ReportService, _report_markdown
from shadow.validation import diagnostic_sources
from test_analysis_quality import analyzer_for
from test_shared_analysis import DIAGNOSTIC_RESULT


def snapshot():
    return {"name": "web", "container_id": "old", "state": "running",
            "running": True, "health": "healthy", "oom_killed": False,
            "restarts": 0, "expected_stopped": False, "started_at": "2026-01-01T00:00:00Z",
            "metrics_available": True, "cpu_percent": 96, "memory": {"percent": 96}}


def collector(source):
    return ObservationCollector(inspect_container=None, get_stats=None,
        expected_stopped_names=lambda: set(), managed_names=lambda: {"web"},
        disk_snapshot=lambda: {"usage_percent": 1}, normalize_fingerprints=None,
        load_profiles=None, baseline_to_since=None, get_logs=None, resolve_profile=None,
        objective_snapshot=source, docker_workers=1, baseline_version=2, max_log_lines=200)


class ObservationFailureTests(unittest.TestCase):
    def setUp(self):
        clock = patch("shadow.collector.datetime")
        mock_clock = clock.start()
        ticks = count()
        mock_clock.now.side_effect = lambda *_: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=60 * next(ticks))
        self.addCleanup(clock.stop)
        self.settings = patch.dict("os.environ", {"SUSTAINED_METRIC_SAMPLES": "3",
            "SUSTAINED_MIN_DURATION_SECONDS": "120", "SUSTAINED_MAX_SAMPLE_GAP_SECONDS": "300",
            "SUSTAINED_CPU_THRESHOLD_PERCENT": "90", "SUSTAINED_MEMORY_THRESHOLD_PERCENT": "85",
            "SUSTAINED_MEMORY_HIGH_THRESHOLD_PERCENT": "95"})
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_transient_inspection_failure_preserves_saved_state_and_recovers(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
                current = snapshot()
                current.update(restarts=4, oom_killed=True)
                source = Mock(return_value=current)
                service = collector(source)
                baseline = service.collect_state(store.empty(), False)["next_baseline"]
                if legacy:
                    baseline["containers"]["web"].pop("container_id", None)
                store.save(baseline)
                observation = ObservationService(load_baseline=store.load,
                    collect_objective_server_state=service.collect_state,
                    collect_new_container_logs=lambda *_: {"active": True,
                        "reason": "differential_query", "summary": {"query_errors": 0}},
                    safe_interpret_relevant_events=lambda *_: {},
                    normalize_event_fingerprint_store=store.normalize_fingerprints,
                    save_baseline=store.save)
                source.side_effect = ToolError("Synthetic proxy HTTP 503")
                for _ in range(2):
                    failed = observation.run()
                    self.assertTrue(failed["baseline"]["saved"])
                    self.assertEqual(failed["summary"]["not_found"], 0)
                    self.assertEqual(failed["summary"]["inspection_errors"], 1)
                    self.assertEqual(failed["summary"]["stopped"], 0)
                    self.assertEqual(failed["overall_severity"], "no_indication")
                    self.assertEqual(failed["changes"], [])
                    item = failed["containers"][0]
                    self.assertEqual(item["state"], "unknown")
                    self.assertIsNone(item["running"])
                    self.assertFalse(item["inspection_available"])
                    self.assertIn("503", item["inspection_error"])
                    self.assertEqual(store.load()[0]["containers"], baseline["containers"])
                    self.assertNotIn("OPERATIONAL", _report_markdown(failed, "test"))
                source.side_effect = None
                recovered = observation.run()
                self.assertEqual(recovered["changes"], [])
                self.assertEqual(recovered["summary"]["new_restarts"], 0)
                self.assertEqual(recovered["summary"]["new_ooms"], 0)
                self.assertEqual(recovered["containers"][0]["sustained_metrics"]["consecutive_high_memory"], 1)
                current["restarts"] = 5
                self.assertEqual(observation.run()["summary"]["new_restarts"], 1)

    def test_initial_query_failure_does_not_create_a_false_baseline(self):
        service = collector(Mock(side_effect=ToolError("timeout")))
        failed = service.collect_state({}, False)
        self.assertNotIn("web", failed["next_baseline"]["containers"])
        self.assertEqual(failed["changes"], [])

    def test_public_report_retains_query_errors_without_container_incidents(self):
        service = collector(Mock(side_effect=ToolError("Synthetic proxy HTTP 503")))
        cycle = service.collect_state({}, False)
        report = ReportService(server_name="test", max_log_lines=200,
            run_cycle=lambda: cycle, render_report=_report_markdown).generate()
        self.assertEqual(report["collection_errors"], [{"origin": "web", "operation": "inspection",
            "error": "Synthetic proxy HTTP 503"}])
        self.assertEqual(report["incidents"], [])
        self.assertIn("INCOMPLETE OBSERVATION", report["markdown"])

    def test_snapshot_propagates_identity_and_stats_failure_is_not_zero(self):
        containers = ContainerService(find_container=lambda _: {"Id": "old"},
            docker_get=lambda *_: SimpleNamespace(json=lambda: {
                "Name": "/web", "State": {"Running": True, "Status": "running"}}),
            get_bounded=None, all_containers=None, managed_container_names=None,
            container_names=None, max_lines=20, max_chars=1000)
        service = collector(None)
        service.inspect_container = containers.inspect
        service.get_stats = Mock(side_effect=ToolError("timeout"))
        failed = service.snapshot("web")
        self.assertEqual(failed["container_id"], "old")
        self.assertTrue(failed["inspection_available"])
        self.assertFalse(failed["metrics_available"])
        self.assertIsNone(failed["cpu_percent"])
        self.assertIsNone(failed["memory"]["percent"])
        self.assertEqual(failed["metrics_error"], "timeout")
        service.get_stats.side_effect = None
        service.get_stats.return_value = snapshot()
        self.assertTrue(service.snapshot("web")["metrics_available"])
        service.get_stats.return_value["container_id"] = "replacement"
        changed = service.snapshot("web")
        self.assertFalse(changed["metrics_available"])
        self.assertIsNone(changed["cpu_percent"])

    def test_failed_statistics_break_consecutive_samples_and_report_incompleteness(self):
        current = snapshot()
        service = collector(lambda _: deepcopy(current))
        baseline = {}
        for _ in range(2):
            baseline = service.collect_state(baseline, True)["next_baseline"]
        current.update(metrics_available=False, metrics_error="timeout", cpu_percent=None,
                       memory={"percent": None})
        failed = service.collect_state(baseline, True)
        self.assertEqual(failed["summary"]["metrics_errors"], 1)
        self.assertEqual(failed["summary"]["running"], 1)
        self.assertEqual(failed["overall_severity"], "no_indication")
        metrics = failed["containers"][0]["sustained_metrics"]
        self.assertEqual(metrics["consecutive_high_memory"], 0)
        self.assertIsNone(metrics["memory_percent"])
        report = _report_markdown(failed, "test")
        self.assertIn("INCOMPLETE OBSERVATION", report)
        self.assertIn("Statistics unavailable", report)
        current.update(snapshot())
        recovered = service.collect_state(failed["next_baseline"], True)
        self.assertEqual(recovered["containers"][0]["sustained_metrics"]["consecutive_high_memory"], 1)

    def test_network_timeout_does_not_confirm_absence(self):
        def timeout(request):
            raise httpx.ReadTimeout("synthetic", request=request)
        http = SimpleNamespace(Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(timeout), **kw),
            HTTPStatusError=httpx.HTTPStatusError, RequestError=httpx.RequestError)
        client = DockerClient("http://synthetic", http)
        self.addCleanup(client.close)
        service = collector(lambda _: client.get("/containers/old/json"))
        result = service.collect_state({}, False)
        self.assertEqual(result["summary"]["not_found"], 0)
        self.assertEqual(result["summary"]["inspection_errors"], 1)

    def test_http_errors_are_classified_by_status_and_endpoint(self):
        for status, endpoint, expected in ((404, "inspect", "not_found"),
                (503, "inspect", "unknown"), (403, "inspect", "unknown"),
                (404, "inventory", "unknown")):
            with self.subTest(status=status, endpoint=endpoint):
                transport = httpx.MockTransport(lambda _: httpx.Response(status))
                http = SimpleNamespace(Client=lambda **kw: httpx.Client(transport=transport, **kw),
                    HTTPStatusError=httpx.HTTPStatusError, RequestError=httpx.RequestError)
                client = DockerClient("http://synthetic", http)
                self.addCleanup(client.close)
                find = (lambda _: client.get("/containers/json")) if endpoint == "inventory" else lambda _: {"Id": "old"}
                containers = ContainerService(find_container=find, docker_get=client.get,
                    get_bounded=None, all_containers=None, managed_container_names=None,
                    container_names=None, max_lines=20, max_chars=1000)
                service = collector(None)
                service.inspect_container = containers.inspect
                service.objective_snapshot = service.snapshot
                result = service.collect_state({}, False)
                self.assertEqual(result["containers"][0]["state"], expected)
                self.assertEqual(result["summary"]["not_found"], int(expected == "not_found"))
                self.assertEqual(result["overall_severity"], "high" if expected == "not_found" else "no_indication")

    def test_replacement_resets_counters_and_same_identity_accumulates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
            current = snapshot()
            service = collector(lambda _: deepcopy(current))
            baseline = store.empty()
            for _ in range(2):
                store.save(service.collect_state(baseline, True)["next_baseline"])
                baseline = store.load()[0]
            self.assertEqual(baseline["metrics"]["web"]["consecutive_high_memory"], 2)
            self.assertEqual(baseline["containers"]["web"]["container_id"], "old")
            current["container_id"] = "replacement"
            for count in range(1, 4):
                result = service.collect_state(baseline, True)
                item = result["containers"][0]
                self.assertEqual(item["sustained_metrics"]["consecutive_high_memory"], count)
                self.assertEqual(item["sustained_metrics"]["consecutive_high_cpu"], count)
                self.assertEqual(item["sustained_metrics_severity"], "high" if count == 3 else "no_indication")
                store.save(result["next_baseline"])
                baseline = store.load()[0]
                self.assertEqual(baseline["metrics"]["web"]["container_id"], "replacement")

    def test_legacy_metrics_without_identity_start_a_new_sequence(self):
        current = snapshot()
        service = collector(lambda _: deepcopy(current))
        baseline = {"metrics": {"web": {"consecutive_high_cpu": 2, "consecutive_high_memory": 2}}}
        for missing_current_id in (False, True):
            if missing_current_id:
                current.pop("container_id")
            with self.subTest(missing_current_id=missing_current_id):
                result = service.collect_state(baseline, True)
                self.assertEqual(result["containers"][0]["sustained_metrics"]["consecutive_high_memory"], 1)
                self.assertEqual(result["containers"][0]["sustained_metrics_severity"], "no_indication")
                following = service.collect_state(result["next_baseline"], True)
                self.assertEqual(following["containers"][0]["sustained_metrics"]["consecutive_high_memory"],
                                 1 if missing_current_id else 2)


class DiagnosticMetricFailureTests(unittest.TestCase):
    def service(self, stats, logs="", healthy=True):
        def docker(path, **_):
            if path.endswith("/stats"):
                if isinstance(stats, Exception):
                    raise stats
                return SimpleNamespace(json=lambda: stats)
            return SimpleNamespace(json=lambda: {"State": {"Running": True, "Status": "running",
                "Health": {"Status": "healthy" if healthy else "unhealthy"}}, "RestartCount": 0})
        return DiagnosticService(find_container=lambda _: {"Id": "old"}, docker_get=docker,
            get_logs=lambda *_: {"logs": logs, "truncated": False},
            filter_profile_noise=lambda _, text: (text, 0, []), expected_stopped_names=lambda: set(),
            collect_snapshot=None, analyze_snapshot=None, cache={}, cache_ttl=0, model="disabled")

    def test_stats_errors_are_explicit_nulls_with_valid_state_and_logs(self):
        for stats in (ToolError("Synthetic stats timeout"), ValueError("Invalid JSON"), [], {}):
            with self.subTest(stats=stats):
                result = self.service(stats, "warning synthetic log", healthy=False).snapshot("web", 10)
                facts = result["objective_data"]
                self.assertFalse(facts["metrics_available"])
                self.assertTrue(facts["metrics_error"])
                self.assertTrue(facts["running"])
                self.assertEqual(facts["health"], "unhealthy")
                self.assertEqual(result["logs"], "warning synthetic log")
                self.assertEqual(result["objective_severity"], "high")
                for value in (facts["cpu_percent"], facts["processes"], *facts["memory"].values(), *facts["network"].values()):
                    self.assertIsNone(value)
                sources = diagnostic_sources(result)
                self.assertFalse(json.loads(sources["metrics"])["metrics_available"])
                self.assertNotIn("Memory usage exceeds", str(result["objective_findings"]))

    def test_recovered_statistics_are_real_measurements(self):
        service = self.service(ToolError("timeout"))
        self.assertFalse(service.snapshot("web", 10)["objective_data"]["metrics_available"])
        service.docker_get = self.service({"cpu_stats": {"cpu_usage": {"total_usage": 10}, "system_cpu_usage": 20, "online_cpus": 1},
            "precpu_stats": {"cpu_usage": {"total_usage": 10}, "system_cpu_usage": 10},
            "memory_stats": {"usage": 96, "limit": 100}, "pids_stats": {"current": 2},
            "networks": {"eth0": {"rx_bytes": 10, "tx_bytes": 20}}}).docker_get
        result = service.snapshot("web", 10)
        self.assertTrue(result["objective_data"]["metrics_available"])
        self.assertEqual(result["objective_data"]["metrics_error"], "")
        self.assertEqual(result["objective_data"]["cpu_percent"], 0)
        self.assertEqual(result["objective_data"]["memory"]["percent"], 96)
        self.assertEqual(result["objective_data"]["processes"], 2)
        self.assertEqual(result["objective_severity"], "high")

    def test_provider_context_exposes_missing_stats_and_rejects_invented_zero(self):
        service = self.service(ToolError("Synthetic stats timeout"), "warning")
        snapshot_data = service.snapshot("web", 10)
        captured = {}
        answer = deepcopy(DIAGNOSTIC_RESULT)
        analyzer_for(answer, captured).diagnose(snapshot_data)
        prompt = captured["prompt"]
        self.assertIn('"metrics_available": false', prompt)
        self.assertIn('"cpu_percent": null', prompt)
        self.assertIn("Synthetic stats timeout", prompt)
        self.assertIn("Null metrics are unavailable", prompt)
        self.assertEqual(snapshot_data["objective_severity"], "low")
        answer["findings"][0].update(source="metrics", evidence='"cpu_percent": 0.0')
        with self.assertRaises(AnalysisValidationError):
            analyzer_for(answer).diagnose(snapshot_data)
        answer["findings"][0].update(source="metrics", evidence='"metrics_available": false')
        from shadow.validation import ValidatedProvider
        result = ValidatedProvider(analyzer_for(answer)).diagnose(snapshot_data)
        self.assertEqual(result["evidence_references"][0]["matches"][0]["input_pointer"],
                         "/objective_data/metrics_available")

    def test_diagnose_passes_missing_metrics_to_provider_and_preserves_objective_data(self):
        service = self.service(ToolError("Synthetic stats timeout"), "warning")
        service.collect_snapshot = service.snapshot
        seen = []
        def analyze(data):
            seen.append(deepcopy(data))
            return deepcopy(DIAGNOSTIC_RESULT)
        service.analyze_snapshot = analyze
        result = service.diagnose("web", 10)
        self.assertEqual(result["objective_data"], seen[0]["objective_data"])
        self.assertFalse(result["objective_data"]["metrics_available"])
        self.assertIsNone(result["objective_data"]["cpu_percent"])
        self.assertEqual(seen[0]["logs"], "warning")
        self.assertEqual(result["final_severity"], "low")
