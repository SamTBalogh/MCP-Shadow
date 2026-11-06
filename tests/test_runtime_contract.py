"""Exercise public payloads and baseline behavior without network services."""

import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from mcp.server.fastmcp.exceptions import ToolError
from test_log_pages import framed


def load_server():
    modules = {
        name: types.ModuleType(name)
        for name in (
            "httpx",
            "psutil",
        )
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "server_under_test", Path(__file__).resolve().parents[1] / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class RuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def test_empty_logs_return_english_analysis(self):
        data = {"container_name": "web", "logs": "", "truncated": False}
        with patch.object(self.server, "_get_container_logs_data", return_value=data):
            result = self.server.analyze_container_logs("web")
        self.assertEqual(result["severity"], "no_indication")
        self.assertEqual(result["summary"], "The container did not return logs.")
        self.assertEqual(result["recommended_actions"], [])

    def test_analysis_calls_use_provider_contract(self):
        class FakeProvider:
            def analyze_logs(self, container_name, logs):
                return {"summary": f"{container_name}: {logs}"}, False

            def interpret_events(self, log_activity):
                return {"executed": True, "findings": log_activity["events"]}

            def diagnose(self, snapshot):
                return {"model_severity": "low", "summary": snapshot["name"]}

        from shadow.analysis import AnalysisProvider

        provider = FakeProvider()
        self.assertIsInstance(provider, AnalysisProvider)
        self.assertIsInstance(self.server._ollama_analyzer(), AnalysisProvider)
        with patch.object(self.server, "_analysis_provider", return_value=provider):
            result, truncated = self.server._analyze_logs_with_ollama("web", "test")
            self.assertEqual(result["summary"], "web: test")
            self.assertFalse(truncated)
            self.assertEqual(
                self.server._interpret_relevant_events_with_ollama({"events": ["x"]})["findings"],
                ["x"],
            )
            self.assertEqual(self.server._diagnose_with_ollama({"name": "web"})["summary"], "web")

    def test_provider_errors_are_translated_at_mcp_boundary(self):
        provider = types.SimpleNamespace(
            analyze_logs=lambda *_: (_ for _ in ()).throw(self.server.AnalysisError("provider offline"))
        )
        with patch.object(self.server, "_analysis_provider", return_value=provider):
            with self.assertRaisesRegex(self.server.ToolError, "provider offline"):
                self.server._analyze_logs_with_ollama("web", "sample")

    def test_provider_failure_cannot_change_objective_diagnostic(self):
        def fail(_snapshot):
            raise self.server.AnalysisError("provider offline")

        provider = types.SimpleNamespace(diagnose=fail)
        snapshot = {
            "name": "web", "objective_severity": "high", "objective_data": {},
            "log_indicators": {}, "objective_findings": [], "logs_truncated": False,
        }
        with (
            patch.object(self.server, "_analysis_provider", return_value=provider),
            patch.object(self.server, "_container_snapshot", return_value=snapshot),
            patch.object(self.server, "DIAGNOSTIC_CACHE_TTL_SECONDS", 0),
        ):
            result = self.server.diagnose_container("web")
        self.assertEqual(result["final_severity"], "high")
        self.assertEqual(result["objective_severity"], "high")
        self.assertIsNone(result["model_severity_advisory"])

    def test_bounded_differential_log_prefix_is_flagged(self):
        client = types.SimpleNamespace(get_bounded=lambda *args: (
            b"\x01\x00\x00\x00\x00\x00\x00\x10abcdefgh", True
        ))
        with (
            patch.object(self.server, "find_container", return_value={"Id": "abc"}),
            patch.object(self.server, "DOCKER_CLIENT", client),
            patch.object(self.server, "MAX_LOG_CHARS", 5),
        ):
            result = self.server._get_container_logs_data("web", 2, since=123)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["mode"], "differential")
        self.assertEqual(result["logs"], "abcde")

    def test_report_uses_english_keys_and_rendering(self):
        cycle = {
            "overall_severity": "high",
            "summary": {"running": 1},
            "objective_incidents": [
                {
                    "severity": "high",
                    "origin": "web",
                    "description": "Container stopped.",
                }
            ],
            "changes": [],
            "baseline": {"saved": True},
            "log_activity": {"summary": {}},
            "event_interpretation": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(self.server, "BASELINE_PATH", Path(directory) / "baseline.json"),
                  patch.object(self.server, "_run_locked_objective_server_cycle", return_value=cycle)):
                report = self.server.generate_server_report()
        self.assertEqual(report["overall_severity"], "high")
        self.assertEqual(report["incidents"][0]["origin"], "web")
        self.assertIn("Status:", report["markdown"])
        self.assertIn("Container stopped.", report["markdown"])

    def test_observation_cycle_persists_english_baseline(self):
        baseline = self.server._empty_baseline()
        next_baseline = self.server._empty_baseline()
        logs = {"active": False, "reason": "initial_baseline", "summary": {}}
        with (
            patch.object(
                self.server, "_load_baseline", return_value=(baseline, [], False)
            ),
            patch.object(
                self.server,
                "_collect_objective_server_state",
                return_value={"next_baseline": next_baseline},
            ),
            patch.object(self.server, "_collect_new_container_logs", return_value=logs),
            patch.object(
                self.server,
                "_safe_interpret_relevant_events",
                return_value={"executed": False},
            ),
            patch.object(self.server, "_save_baseline") as save,
        ):
            result = self.server._run_locked_objective_server_cycle()
        self.assertTrue(result["baseline"]["saved"])
        self.assertEqual(save.call_args.args[0]["event_fingerprints"], {})
        self.assertIn("event_interpretation", result)

    def test_truncated_differential_logs_do_not_advance_baseline(self):
        baseline = self.server._empty_baseline()
        logs = {"active": True, "reason": "differential_query",
                "summary": {"truncated_queries": 1}}
        with (
            patch.object(self.server, "_load_baseline", return_value=(baseline, [], True)),
            patch.object(self.server, "_collect_objective_server_state",
                         return_value={"next_baseline": self.server._empty_baseline()}),
            patch.object(self.server, "_collect_new_container_logs", return_value=logs),
            patch.object(self.server, "_safe_interpret_relevant_events", return_value={}),
            patch.object(self.server, "_save_baseline") as save,
        ):
            result = self.server._run_locked_objective_server_cycle()
        self.assertEqual(save.call_args.args[0]["last_run"], baseline["last_run"])
        self.assertEqual(save.call_args.args[0]["containers"], baseline["containers"])
        self.assertTrue(result["baseline"]["monitoring_saved"])
        self.assertFalse(result["baseline"]["saved"])
        self.assertEqual(result["baseline"]["truncated_queries"], 1)
        self.assertIn("exceeded", result["baseline"]["reason_not_saved"])

    def test_container_observations_run_concurrently_with_stable_order(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def snapshot(name):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with guard:
                active -= 1
            return {"name": name, "state": "running", "running": True,
                    "expected_stopped": False, "health": "healthy"}

        with (
            patch.object(self.server, "managed_container_names", return_value={"c", "a", "b"}),
            patch.object(self.server, "_objective_container_snapshot", side_effect=snapshot),
            patch.object(self.server, "disk_snapshot", return_value={"usage_percent": 0}),
            patch.object(self.server, "DOCKER_WORKERS", 2),
        ):
            result = self.server._collect_objective_server_state(self.server._empty_baseline(), False)
        self.assertEqual(maximum, 2)
        self.assertEqual([item["name"] for item in result["containers"]], ["a", "b", "c"])

    def test_diagnostic_combines_english_snapshots(self):
        snapshot = {
            "name": "web",
            "objective_severity": "high",
            "objective_data": {},
            "log_indicators": {},
            "objective_findings": [],
            "logs_truncated": False,
        }
        with (
            patch.object(self.server, "_container_snapshot", return_value=snapshot),
            patch.object(
                self.server,
                "_diagnose_with_ollama",
                return_value={"model_severity": "low"},
            ),
            patch.object(self.server, "DIAGNOSTIC_CACHE_TTL_SECONDS", 0),
        ):
            result = self.server.diagnose_container("web")
        self.assertEqual(result["final_severity"], "high")
        self.assertEqual(result["model_severity_advisory"], "low")

    def test_diagnostic_survives_ollama_failure_without_caching_it(self):
        snapshot = {"name": "web", "objective_severity": "high",
                    "objective_data": {}, "log_indicators": {},
                    "objective_findings": [{"finding": "Container stopped."}],
                    "logs_truncated": False}
        with (patch.object(self.server, "_container_snapshot", return_value=snapshot),
              patch.object(self.server, "_diagnose_with_ollama", side_effect=self.server.ToolError("offline")),
              patch.object(self.server, "DIAGNOSTIC_CACHE", {})):
            result = self.server.diagnose_container("web")
            self.assertEqual(result["final_severity"], "high")
            self.assertIsNone(result["model_severity_advisory"])
            self.assertEqual(result["model_analysis"]["limitations"], ["offline"])
            self.assertEqual(self.server.DIAGNOSTIC_CACHE, {})

    def test_report_model_context_redacts_secrets(self):
        activity = {"containers": [{"name": "web", "relevant_event_details": [{
            "message": "token=secret-value", "content": "password=hidden-value",
            "relevance": {"relevant": True},
        }]}]}
        context = self.server._ollama_analyzer()._build_relevant_events_context(activity)
        self.assertTrue(context["active"])
        self.assertNotIn("secret-value", context["context"])
        self.assertNotIn("hidden-value", context["context"])

    def test_readiness_reports_required_and_optional_dependencies(self):
        class Client:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, path):
                return types.SimpleNamespace(raise_for_status=lambda: None,
                                             json=lambda: {"models": []})
        http = types.SimpleNamespace(Client=Client, HTTPError=Exception)
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(self.server, "STATE_DIR", Path(directory)),
                  patch.object(self.server, "DISK_PROBE_PATH", directory),
                  patch.object(self.server, "docker_get"),
                  patch.object(self.server.ANALYSIS_PROVIDER.backend, "http", http)):
                result = self.server._check_readiness()
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["checks"]["ollama"], "model_missing")
                with patch.object(self.server, "docker_get", side_effect=self.server.ToolError("offline")):
                    self.assertEqual(self.server._check_readiness()["status"], "not_ready")

    def test_report_cycles_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            active = 0
            maximum = 0
            guard = threading.Lock()
            def cycle():
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return {}
            with (patch.object(self.server, "BASELINE_PATH", path),
                  patch.object(self.server, "_run_locked_objective_server_cycle", side_effect=cycle)):
                threads = [threading.Thread(target=self.server._run_objective_server_cycle) for _ in range(3)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(maximum, 1)

    def test_container_snapshot_produces_english_findings(self):
        inspection = {
            "State": {"Status": "exited", "Running": False},
            "RestartCount": 0,
        }

        def docker_get(path, **kwargs):
            payload = inspection if path.endswith("/json") else {}
            return types.SimpleNamespace(json=lambda: payload)

        with (
            patch.object(self.server, "find_container", return_value={"Id": "abc"}),
            patch.object(self.server, "docker_get", side_effect=docker_get),
            patch.object(
                self.server,
                "_get_container_logs_data",
                return_value={"logs": "", "truncated": False},
            ),
            patch.object(
                self.server, "_filter_profile_noise", return_value=("", 0, [])
            ),
            patch.object(
                self.server, "expected_stopped_container_names", return_value=set()
            ),
        ):
            snapshot = self.server._container_snapshot("web", 100)
        self.assertEqual(snapshot["objective_severity"], "high")
        self.assertEqual(
            snapshot["objective_findings"][0]["finding"],
            "The container is not running.",
        )

    def test_baseline_round_trip_and_old_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            with (
                patch.object(self.server, "BASELINE_PATH", path),
                patch.object(self.server, "STATE_DIR", path.parent),
            ):
                baseline = self.server._empty_baseline()
                baseline["last_run"] = "2026-01-01T00:00:00+00:00"
                self.server._save_baseline(baseline)
                loaded, warnings, exists = self.server._load_baseline()
                self.assertTrue(exists)
                self.assertEqual(loaded, baseline)
                self.assertEqual(warnings, [])
                path.write_text(json.dumps({"version": 1}))
                loaded, warnings, exists = self.server._load_baseline()
                self.assertFalse(exists)
                self.assertEqual(loaded["version"], 2)
                self.assertTrue(warnings)

    def test_failed_baseline_replace_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text("previous data")
            with (
                patch.object(self.server, "BASELINE_PATH", path),
                patch("shadow.persistence.os.replace", side_effect=OSError("disk failure")),
                self.assertRaises(self.server.ToolError),
            ):
                self.server._save_baseline(self.server._empty_baseline())
            self.assertEqual(path.read_text(), "previous data")
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["baseline.json"])

    def test_failed_outbox_save_reclaims_only_unreferenced_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            spool = self.server.LogSpool(state / "log-spool", 20000, 14000, 0)
            data = b"2026-01-01T00:00:00Z ERROR synthetic event\n" * 200
            client = types.SimpleNamespace(iter_bytes=lambda *_: framed(data), get_bounded=None)
            baseline = self.server._empty_baseline()
            baseline["last_run"] = "2026-01-01T00:00:00Z"
            fixture = {"Id": "abc", "Names": ["/fixture"]}
            with (
                patch.object(self.server, "BASELINE_PATH", state / "baseline.json"),
                patch.object(self.server, "_log_spool", return_value=spool),
                patch.object(self.server, "managed_containers", return_value=[fixture]),
                patch.object(self.server, "find_container", return_value=fixture),
                patch.object(self.server, "DOCKER_CLIENT", client),
                patch.object(self.server, "MAX_LOG_CHARS", 1000),
                patch.object(self.server, "_load_log_profiles", return_value=({}, [])),
                patch.object(self.server, "_resolve_container_log_profile", return_value={}),
                patch.object(self.server, "_safe_interpret_relevant_events", return_value={}),
                patch.object(self.server, "_collect_objective_server_state", side_effect=lambda *_: {
                    "next_baseline": self.server._empty_baseline(),
                }),
            ):
                self.server._save_baseline(baseline)
                with (patch("shadow.baseline.atomic_json", side_effect=OSError("synthetic save failure")),
                      self.assertRaisesRegex(ToolError, "could not be saved")):
                    self.server.generate_server_report(acknowledged_delivery=True)
                abandoned = set(spool.directory.iterdir())
                self.assertEqual(len(abandoned), 2)
                self.assertEqual(self.server._load_baseline()[0], baseline)

                report = self.server.generate_server_report(acknowledged_delivery=True)
                self.assertEqual(report["baseline"]["pending_pages"], 1)
                staged = set(spool.directory.iterdir())
                self.assertEqual(len(staged), 2)
                self.assertFalse(abandoned & staged)
                self.assertEqual(self.server.generate_server_report(acknowledged_delivery=True), report)
                self.assertEqual(set(spool.directory.iterdir()), staged)

                self.server.acknowledge_server_report(report["delivery"]["id"])
                with patch.object(client, "iter_bytes", side_effect=AssertionError("Unexpected refetch")):
                    next_report = self.server.generate_server_report(acknowledged_delivery=True)
                self.assertEqual(next_report["baseline"]["log_errors"], 0)
                self.assertEqual(next_report["baseline"]["pending_pages"], 1)
                self.assertEqual(set(spool.directory.iterdir()), staged)

    def test_model_schema_and_prompt_are_english(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "response": json.dumps(
                    {
                        "severity": "low",
                        "summary": "Warning observed.",
                        "evidence": ["warning"],
                        "probable_causes": [],
                        "recommended_actions": [],
                        "confidence": 0.8,
                    }
                )
            },
            raise_for_status=lambda: None,
        )
        captured = {}

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, *args, **kwargs):
                captured.update(kwargs["json"])
                return response

        fake_http = types.SimpleNamespace(
            Client=Client,
            HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
            RequestError=type("RequestError", (Exception,), {}),
        )
        with (patch.object(self.server, "httpx", fake_http),
              patch.object(self.server, "_analysis_provider", side_effect=self.server._ollama_analyzer)):
            result, truncated = self.server._analyze_logs_with_ollama("web", "warning token=secret-value")
        self.assertEqual(result["severity"], "low")
        self.assertFalse(truncated)
        self.assertIn(
            "Write every human-readable string value in English.", captured["prompt"]
        )
        self.assertIn("recommended_actions", captured["format"]["properties"])
        self.assertNotIn("secret-value", captured["prompt"])

    def test_invalid_model_responses_are_rejected(self):
        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, *args, **kwargs):
                return types.SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"response": output},
                )

        fake_http = types.SimpleNamespace(
            Client=Client,
            HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
            RequestError=type("RequestError", (Exception,), {}),
        )
        valid = {"severity": "low", "summary": "ok", "evidence": [],
                 "probable_causes": [], "recommended_actions": [], "confidence": 0.5}
        invalid_outputs = [
            "not json",
            '{"severity":"low"}',
            json.dumps({**valid, "severity": []}),
            json.dumps({**valid, "summary": None}),
            json.dumps({**valid, "evidence": [123]}),
            json.dumps({**valid, "confidence": True}),
            json.dumps({**valid, "confidence": float("nan")}),
        ]
        for output in invalid_outputs:
            with (self.subTest(output=output), patch.object(self.server, "httpx", fake_http),
                  patch.object(self.server, "_analysis_provider", side_effect=self.server._ollama_analyzer)):
                with self.assertRaises(self.server.ToolError):
                    self.server._analyze_logs_with_ollama("web", "warning")

    def test_non_object_ollama_envelope_is_rejected(self):
        class Client:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def post(self, *args, **kwargs):
                return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: [])

        fake_http = types.SimpleNamespace(
            Client=Client,
            HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
            RequestError=type("RequestError", (Exception,), {}),
        )
        with (patch.object(self.server, "httpx", fake_http),
              patch.object(self.server, "_analysis_provider", side_effect=self.server._ollama_analyzer)):
            with self.assertRaisesRegex(self.server.ToolError, "invalid response object"):
                self.server._analyze_logs_with_ollama("web", "warning")


if __name__ == "__main__":
    unittest.main()
