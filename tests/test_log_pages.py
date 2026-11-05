"""Regression tests for bounded, resumable log intervals."""
import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from scripts.mcp_demo import consume_reports
from shadow.baseline import BaselineStore
from shadow.containers import ContainerService
from shadow.collector import ObservationCollector
from shadow.delivery import ReportDelivery
from shadow.events import _group_log_lines_into_events
from shadow.log_pages import read_page
from shadow.log_spool import LogSpool
from shadow.observation import ObservationService
from shadow.reporting import ReportService


def framed(data, sizes=(7, 31, 2, 100)):
    wire = b"".join(b"\x01\0\0\0" + len(data[i:i + 80]).to_bytes(4, "big") + data[i:i + 80]
                    for i in range(0, len(data), 80))
    pos = 0
    index = 0
    while pos < len(wire):
        size = sizes[index % len(sizes)]
        yield wire[pos:pos + size]
        pos += size
        index += 1


class LogPageTests(unittest.TestCase):
    def test_same_timestamp_unicode_multiline_and_many_pages(self):
        entries = [f"2026-01-01T00:00:00.000000001Z ERROR incident {i} café\n"
                   f"2026-01-01T00:00:00.000000001Z   continuation {i}\n"
                   for i in range(100)]
        data = "".join(entries).encode()
        pages = []
        cursor = {}
        while not cursor.get("complete", False):
            page, cursor = read_page(framed(data), cursor, 500)
            pages.append(page)
        self.assertGreater(len(pages), 2)
        self.assertTrue(all(len(page.encode()) < 650 for page in pages))
        self.assertEqual("".join(pages).encode(), data)
        self.assertEqual(sum(len(_group_log_lines_into_events(p)) for p in pages), 100)
        self.assertTrue(all(event["multiline"] for page in pages for event in _group_log_lines_into_events(page)))

    def test_rotation_or_shortened_prefix_fails_closed(self):
        source = ("2026-01-01T00:00:00Z ERROR event\n" * 50).encode()
        _, cursor = read_page(framed(source), {}, 100)
        for changed in (b"different\n" + source, source[:10]):
            with self.assertRaisesRegex(ToolError, "prefix"):
                read_page(framed(changed), cursor, 100)

    def test_leading_blank_lines_and_invalid_cursor_are_handled(self):
        data = ("\n\n" + "2026-01-01T00:00:00Z ERROR one\n" * 30).encode()
        cursor = {}
        pages = []
        while not cursor.get("complete", False):
            page, cursor = read_page(framed(data), cursor, 100)
            pages.append(page)
        self.assertEqual("".join(pages).encode(), data)
        with self.assertRaisesRegex(ToolError, "cursor"):
            read_page(framed(data), {"offset": 1, "sha256": "á" * 64}, 100)

    def test_large_single_event_and_incomplete_frame_fail_closed(self):
        with self.assertRaisesRegex(ToolError, "single log event"):
            read_page(iter([b"x" * 10000]), {}, 100)
        with self.assertRaisesRegex(ToolError, "incomplete frame"):
            read_page(iter([b"\x01\0\0\0\0\0\0\x10abc"]), {}, 100)
        with self.assertRaisesRegex(ToolError, "incomplete frame"):
            read_page(iter([b"\x01\0\0\0\0"]), {}, 100)

    def test_container_replacement_fails_closed(self):
        service = ContainerService(find_container=lambda name: {"Id": "new"},
            docker_get=None, get_bounded=None, all_containers=None,
            managed_container_names=None, container_names=None, max_lines=10, max_chars=1000)
        with self.assertRaisesRegex(ToolError, "container changed"):
            service.log_page("web", 1, 2, {"container_id": "old"}, lambda *_: iter([]))

    def test_partial_progress_survives_restart_without_advancing_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 1, 256)
            old = store.empty()
            old["last_run"] = "2026-01-01T00:00:00Z"
            store.save(old)
            progress = {"since": 1767225600, "until": 1767225601,
                        "containers": {"web": {"offset": 3, "sha256": hashlib.sha256(b"abc").hexdigest(), "container_id": "a"}}}
            service = ObservationService(load_baseline=store.load,
                collect_objective_server_state=lambda *_: {"next_baseline": {**old, "last_run": "2026-01-01T01:00:00Z"}},
                collect_new_container_logs=lambda *_: {"active": True, "reason": "differential_query", "summary": {"pending_pages": 1}, "log_progress": progress},
                safe_interpret_relevant_events=lambda _: {},
                normalize_event_fingerprint_store=store.normalize_fingerprints, save_baseline=store.save)
            result = service.run()
            self.assertFalse(result["baseline"]["saved"])
            self.assertTrue(result["baseline"]["progress_saved"])
            restored, _, _ = store.load()
            self.assertEqual(restored["last_run"], old["last_run"])
            self.assertEqual(restored["log_progress"], progress)
            service.collect_new_container_logs = lambda *_: {"active": True, "reason": "differential_query", "summary": {"pending_pages": 0}, "log_progress": progress}
            self.assertTrue(service.run()["baseline"]["saved"])
            restored, _, _ = store.load()
            self.assertEqual(restored["last_run"], "2026-01-01T00:00:01Z")
            self.assertIsNone(restored["log_progress"])

    def test_collector_delivers_repeated_identical_events_across_pages(self):
        entry = "2026-01-01T00:00:00Z ERROR duplicate occurrence\n"
        data = (entry * 100).encode()
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 1, 16)
            container = ContainerService(find_container=lambda name: {"Id": "abc"},
                docker_get=None, get_bounded=None, all_containers=None,
                managed_container_names=None, container_names=None, max_lines=10, max_chars=100)
            collector = ObservationCollector(inspect_container=None, get_stats=None,
                expected_stopped_names=None, managed_names=lambda: {"web"}, disk_snapshot=None,
                normalize_fingerprints=store.normalize_fingerprints,
                load_profiles=lambda: ({}, []), baseline_to_since=lambda value, overlap_seconds=2: (1767225600, []),
                get_logs=None, resolve_profile=lambda *_: {}, objective_snapshot=None,
                docker_workers=1, baseline_version=1, max_log_lines=10,
                get_log_page=lambda name, since, until, cursor: container.log_page(
                    name, since, until, cursor, lambda *_: framed(data)),
                managed_log_ids=lambda: {"web": "abc"})
            baseline = store.empty()
            baseline["last_run"] = "2026-01-01T00:00:00Z"
            total = 0
            while True:
                result = collector.collect_logs(baseline, True)
                total += result["summary"]["new_events"]
                progress = result["log_progress"]
                self.assertTrue(progress["membership_fixed"])
                if baseline["log_progress"] is None:
                    # A checkpoint from an older build contains cursors but no membership flag.
                    progress.pop("membership_fixed")
                baseline["log_progress"] = progress
                if result["summary"]["pending_pages"] == 0:
                    break
            self.assertEqual(total, 100)

    def test_report_exposes_each_event_with_common_credentials_redacted(self):
        cycle = {"log_activity": {"containers": [{"name": "web", "events": [
            {"message": "ERROR token=synthetic-secret", "content": "ERROR token=synthetic-secret"},
            {"message": "ERROR second", "content": "ERROR second"},
        ]}]}}
        service = ReportService(server_name="test", max_log_lines=100,
                                run_cycle=lambda: cycle, render_report=lambda *_: "")
        events = service.generate()["log_activity"]["containers"][0]["events"]
        self.assertEqual(len(events), 2)
        self.assertNotIn("synthetic-secret", str(events))
        self.assertIn("[REDACTED]", events[0]["message"])


class AcknowledgedConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_same_interval_after_capture_failure_with_no_pending_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 1, 16)
            old = store.empty()
            old["last_run"] = "2026-01-01T00:00:00Z"
            store.save(old)
            delivery = ReportDelivery(store)
            spool = LogSpool(Path(directory) / "snapshots", 1000000, min_free_bytes=0)
            managed = {"web": "abc"}
            container = ContainerService(find_container=lambda name: {"Id": managed[name]},
                docker_get=None, get_bounded=None, all_containers=None,
                managed_container_names=None, container_names=None, max_lines=100, max_chars=1000)
            intervals = []

            def stream(_path, params):
                intervals.append((int(params["since"]), int(params["until"])))
                if len(intervals) == 1:
                    yield b"\x01\0\0\0\0\0\0\x10abc"  # Docker stream interrupted during capture.
                else:
                    yield from framed(b"2026-01-01T00:00:00Z ERROR recovered event\n")

            collector = ObservationCollector(inspect_container=None, get_stats=None,
                expected_stopped_names=None, managed_names=lambda: {"web"}, disk_snapshot=None,
                normalize_fingerprints=store.normalize_fingerprints,
                load_profiles=lambda: ({}, []), baseline_to_since=lambda *_args, **_kwargs: (1767225600, []),
                get_logs=None, resolve_profile=lambda *_: {}, objective_snapshot=None,
                docker_workers=1, baseline_version=1, max_log_lines=100,
                get_log_page=lambda name, since, until, cursor: container.log_page(
                    name, since, until, cursor, stream, spool),
                managed_log_ids=lambda: dict(managed))

            def build(save):
                return ObservationService(
                    load_baseline=store.load,
                    collect_objective_server_state=lambda baseline, _: {"next_baseline": dict(baseline)},
                    collect_new_container_logs=collector.collect_logs,
                    safe_interpret_relevant_events=lambda _: {},
                    normalize_event_fingerprint_store=store.normalize_fingerprints,
                    save_baseline=save,
                ).run()

            acknowledged_reports = []

            async def call(tool, **arguments):
                if tool == "generate_server_report":
                    return delivery.generate(build, arguments["acknowledged_delivery"])
                self.assertEqual(tool, "acknowledge_server_report")
                report = store.load()[0]["delivery"]["report"]
                ack = delivery.acknowledge(arguments["delivery_id"])
                self.assertTrue(ack["acknowledged"])
                acknowledged_reports.append(report)
                if len(acknowledged_reports) == 1:
                    baseline = report["baseline"]
                    self.assertEqual(baseline["pending_pages"], 0)
                    self.assertFalse(baseline["complete_logs"])
                    self.assertEqual(baseline["log_errors"], 1)
                    saved = store.load()[0]
                    self.assertEqual(saved["last_run"], old["last_run"])
                    progress = saved["log_progress"]
                    self.assertEqual((progress["since"], progress["until"]), intervals[0])
                    self.assertEqual(progress["containers"], {"web": {"container_id": "abc"}})
                    for fixtures, error in (({}, "no longer managed"),
                                            ({"web": "replacement", "new": "other"}, "container changed")):
                        managed.clear()
                        managed.update(fixtures)
                        restarted = ReportDelivery(store)
                        blocked = restarted.generate(build, True)
                        self.assertFalse(blocked["baseline"]["complete_logs"])
                        self.assertEqual(blocked["baseline"]["log_errors"], 1)
                        self.assertIn(error, blocked["log_activity"]["containers"][0]["error"])
                        self.assertEqual(intervals, [(progress["since"], progress["until"])])
                        restarted.acknowledge(blocked["delivery"]["id"])
                        saved = store.load()[0]
                        self.assertEqual(saved["last_run"], old["last_run"])
                        self.assertEqual(saved["log_progress"], progress)
                    managed["web"] = "abc"  # The new container waits for the next interval.
                return ack

            with redirect_stderr(io.StringIO()) as errors:
                messages = await consume_reports(call, "web", verify_replay=True)

            self.assertEqual(messages, ["ERROR recovered event"])
            self.assertIn("Log capture failed (log_errors=1)", errors.getvalue())
            self.assertEqual(len(acknowledged_reports), 2)
            self.assertTrue(acknowledged_reports[-1]["baseline"]["complete_logs"])
            self.assertEqual(intervals, [intervals[0], intervals[0]])
            saved = store.load()[0]
            self.assertEqual(saved["last_run"],
                             datetime.fromtimestamp(intervals[0][1], timezone.utc)
                             .isoformat(timespec="seconds").replace("+00:00", "Z"))
            self.assertIsNone(saved["log_progress"])


if __name__ == "__main__":
    unittest.main()
