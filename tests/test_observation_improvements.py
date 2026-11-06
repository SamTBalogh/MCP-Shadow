"""Coverage for expected inventory, timed metrics, partial data and recovery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.server.fastmcp.exceptions import ToolError
from shadow.baseline import BaselineStore
from shadow.discovery import ContainerRegistry
from shadow.docker_client import ContainerNotFoundError
from shadow.delivery import ReportDelivery
from shadow.log_spool import LogSpool
from shadow.metrics import _update_sustained_metric_entry, _evaluate_sustained_metrics
from shadow.monitoring import observation_health, monitor_status
from shadow.recovery import close_interval, interval_id
from shadow.settings import Settings, SettingsError
from shadow.statistics import parse_statistics
from test_observation_failures import collector, snapshot
import test_observation_failures as failures


def at(seconds):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def raw_stats():
    return {"cpu_stats": {"cpu_usage": {"total_usage": 96}, "system_cpu_usage": 100, "online_cpus": 1},
            "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
            "memory_stats": {"usage": 96, "limit": 100}, "networks": {}, "pids_stats": {"current": 0}}


class InventoryTests(unittest.TestCase):
    def test_expected_missing_is_distinct_from_unauthorized_or_failed_inventory(self):
        query = Mock(return_value=[])
        registry = ContainerRegistry(query, 0, expected_names={"web"})
        with self.assertRaises(ContainerNotFoundError): registry.find("web")
        query.return_value = [{"Id": "private", "Names": ["/web"], "Labels": {}}]
        with self.assertRaises(ToolError) as error: registry.find("web")
        self.assertNotIsInstance(error.exception, ContainerNotFoundError)
        self.assertEqual(registry.names(), set())
        query.side_effect = ToolError("503")
        with self.assertRaises(ToolError) as error: registry.find("web")
        self.assertNotIsInstance(error.exception, ContainerNotFoundError)

    def test_missing_recreated_and_deliberately_retired_service(self):
        current = snapshot()
        source = Mock(return_value=current)
        service = collector(source)
        service.expected_names = lambda: {"web"}
        initial = service.collect_state({}, False)["next_baseline"]
        current["container_id"] = "new"
        current["restarts"] = 10
        recreated = service.collect_state(initial, True)
        self.assertEqual(recreated["changes"][0]["type"], "recreated_service")
        self.assertEqual(recreated["summary"]["new_restarts"], 0)
        service.managed_names = set
        source.side_effect = ContainerNotFoundError("missing")
        missing = service.collect_state(recreated["next_baseline"], True)
        self.assertEqual(missing["summary"]["not_found"], 1)
        service.expected_names = set
        retired = service.collect_state(missing["next_baseline"], True)
        self.assertEqual(retired["changes"][0]["type"], "retired_service")
        self.assertEqual(retired["overall_severity"], "no_indication")
        self.assertEqual(retired["next_baseline"]["containers"], {})

    def test_inventory_failure_preserves_previous_state(self):
        service = collector(lambda _: snapshot())
        initial = service.collect_state({}, False)["next_baseline"]
        service.managed_names = Mock(side_effect=ToolError("503"))
        failed = service.collect_state(initial, True)
        self.assertEqual(failed["next_baseline"]["containers"], initial["containers"])
        self.assertEqual(failed["summary"]["inventory_errors"], 1)
        self.assertEqual(failed["summary"]["not_found"], 0)

    def test_configuration_rejects_patterns_and_invalid_times(self):
        self.assertEqual(Settings.from_env({"EXPECTED_CONTAINER_NAMES": "web, db-1"}).expected_container_names, ("web", "db-1"))
        for key, value in (("EXPECTED_CONTAINER_NAMES", "web*"), ("EXPECTED_CONTAINER_NAMES", "../db"),
                           ("SUSTAINED_MIN_DURATION_SECONDS", "0"), ("SUSTAINED_MAX_SAMPLE_GAP_SECONDS", "nan"),
                           ("METRIC_MAX_AGE_SECONDS", "-1")):
            with self.subTest(key=key), self.assertRaises(SettingsError): Settings.from_env({key: value})


class TimedMetricsTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"SUSTAINED_METRIC_SAMPLES": "3", "SUSTAINED_MIN_DURATION_SECONDS": "120",
            "SUSTAINED_MAX_SAMPLE_GAP_SECONDS": "300"})
        env.start(); self.addCleanup(env.stop)

    def sequence(self, times, current=None, previous=None):
        entry = previous or {}
        for time in times:
            entry = _update_sustained_metric_entry(current or snapshot(), entry, at(time))
        return entry

    def test_burst_cannot_replace_duration_and_duplicate_time_cannot_add_samples(self):
        entry = self.sequence([0, 1, 2])
        self.assertEqual(_evaluate_sustained_metrics(entry)["severity"], "no_indication")
        entry = self.sequence([120], previous=entry)
        self.assertEqual(_evaluate_sustained_metrics(entry)["severity"], "high")
        duplicate = self.sequence([120], previous=entry)
        self.assertEqual(duplicate["consecutive_high_cpu"], entry["consecutive_high_cpu"])

    def test_large_gap_backward_clock_legacy_and_identity_reset(self):
        previous = self.sequence([0, 60, 120])
        for times, current, baseline in (([421], snapshot(), previous), ([-1], snapshot(), previous),
                ([180], {**snapshot(), "container_id": "new"}, previous),
                ([180], snapshot(), {**previous, "high_cpu_since": None, "high_memory_since": None})):
            with self.subTest(times=times, current=current["container_id"]):
                result = self.sequence(times, current, baseline)
                self.assertEqual(result["consecutive_high_cpu"], 1)
                self.assertFalse(result["sustained_high_memory"])

    def test_save_reload_and_partial_metrics_keep_only_valid_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
            baseline = store.empty()
            baseline["metrics"]["web"] = self.sequence([0, 60])
            store.save(baseline)
            data = raw_stats(); del data["memory_stats"]
            current = {**snapshot(), **parse_statistics(data, observed_at=at(120))}
            result = self.sequence([120], current, store.load()[0]["metrics"]["web"])
            self.assertTrue(result["sustained_high_cpu"])
            self.assertEqual(result["consecutive_high_memory"], 0)
            self.assertIsNone(result["memory_percent"])


class PartialStatisticsTests(unittest.TestCase):
    def test_partial_invalid_and_zero_are_distinct(self):
        for invalid in (None, float("nan"), -1, True, "95"):
            data = raw_stats(); data["memory_stats"]["usage"] = invalid
            result = parse_statistics(data)
            self.assertFalse(result["metrics_available"])
            self.assertEqual(result["cpu_percent"], 96)
            self.assertEqual(result["processes"], 0)
            self.assertIsNone(result["memory"]["percent"])
            self.assertTrue(result["metric_status"]["cpu_percent"]["available"])

    def test_stale_and_invalid_timestamps_do_not_produce_live_metrics(self):
        for timestamp in (at(0), at(1000), "invalid"):
            result = parse_statistics({**raw_stats(), "read": timestamp}, observed_at=at(400))
            self.assertFalse(result["metrics_available"])
            self.assertIsNone(result["cpu_percent"])

    def test_valid_memory_still_affects_diagnostics_when_cpu_is_missing(self):
        data = raw_stats(); del data["precpu_stats"]
        service = failures.DiagnosticMetricFailureTests().service(data, "warning")
        result = service.snapshot("web", 10)
        self.assertIsNone(result["objective_data"]["cpu_percent"])
        self.assertEqual(result["objective_data"]["memory"]["percent"], 96)
        self.assertEqual(result["objective_severity"], "high")
        self.assertEqual(result["logs"], "warning")


class RecoveryAndMonitorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = BaselineStore(self.root / "baseline.json", 2, 16)
        self.progress = {"since": 1, "until": 60, "membership_fixed": True,
                         "containers": {"web": {"container_id": "old"}}}
        initial = self.store.empty(); initial["log_progress"] = self.progress
        initial["last_run"] = at(0); self.store.save(initial)

    def test_closure_preserves_captured_data_and_retry_is_idempotent_after_restart(self):
        spool = LogSpool(self.root / "log-spool", 100000, min_free_bytes=0)
        _, cursor = spool.page(lambda: iter([b"2026-01-01T00:00:01Z ERROR test\n"]), "old", 1, 60, {}, 1000)
        baseline = self.store.load()[0]; baseline["log_progress"]["containers"]["web"].update(cursor)
        self.store.save(baseline)
        identity = interval_id(baseline["log_progress"])
        result = close_interval(self.store, identity, "Synthetic fixture replaced")
        self.assertTrue(result["coverage_lost"])
        self.assertFalse(result["already_closed"])
        restored = BaselineStore(self.store.path, 2, 16)
        self.assertTrue(close_interval(restored, identity, "retry")["already_closed"])
        saved = restored.load()[0]
        self.assertIsNone(saved["log_progress"])
        self.assertEqual(datetime.fromisoformat(saved["last_run"]).timestamp(), 60)
        spool.cleanup(None, [gap["progress"] for gap in saved["log_gaps"]])
        self.assertTrue((spool.directory / f"{cursor['spool_id']}.bin").exists())

    def test_closure_refuses_wrong_id_pending_delivery_and_failed_save(self):
        identity = interval_id(self.progress)
        before = self.store.path.read_bytes()
        with self.assertRaises(ToolError): close_interval(self.store, "wrong", "test")
        with patch.object(self.store, "save", side_effect=ToolError("disk full")), self.assertRaises(ToolError):
            close_interval(self.store, identity, "test")
        self.assertEqual(self.store.path.read_bytes(), before)
        delivery = ReportDelivery(self.store)
        pending = delivery.generate(lambda save: {"baseline": {}}, True)
        with self.assertRaises(ToolError): close_interval(self.store, identity, "test")
        delivery.acknowledge(pending["delivery"]["id"])
        self.assertTrue(close_interval(self.store, identity, "test")["closed"])

    def test_monitor_reports_age_partial_coverage_and_pending_age(self):
        good = observation_health({"generated_at": at(0), "containers": [snapshot()]}, {})
        bad = observation_health({"generated_at": at(60), "containers": [
            {**snapshot(), "inspection_available": False}], "summary": {"inspection_errors": 1}}, good)
        baseline = {"monitoring": bad, "delivery": {"created_at": at(30), "next_baseline": {"monitoring": bad}}}
        result = monitor_status(baseline, datetime.fromisoformat(at(90)))
        self.assertEqual(result["services"]["web"]["last_valid_age_seconds"], 90)
        self.assertEqual(result["inspection_coverage_percent"], 0)
        self.assertEqual(result["pending_report_age_seconds"], 60)
        self.assertEqual(result["query_errors"]["inspection"], 1)
        self.assertIsNone(monitor_status({})["last_complete_observation_age_seconds"])
