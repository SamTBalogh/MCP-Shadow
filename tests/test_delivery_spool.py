"""Durable replay, commit failures, concurrent clients and immutable log snapshots."""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from shadow.baseline import BaselineStore
from shadow.containers import ContainerService
from shadow.delivery import ReportDelivery
from shadow.events import _group_log_lines_into_events
from shadow.log_spool import LogSpool
from test_log_pages import framed


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = BaselineStore(Path(self.directory.name) / "baseline.json", 2, 16)
        self.initial = self.store.empty()
        self.initial["last_run"] = "2026-01-01T00:00:00Z"
        self.store.save(self.initial)
        self.delivery = ReportDelivery(self.store)
        self.builds = 0

    def build(self, save):
        self.builds += 1
        proposed = deepcopy(self.initial)
        proposed["last_run"] = "2026-01-01T00:00:10Z"
        if save: save(proposed)
        else: self.store.save(proposed)
        return {"baseline": {"saved": True, "progress_saved": False}, "log_activity": {"events": ["synthetic"]}}

    def test_failure_after_save_before_receipt_replays_across_restart(self):
        first = self.delivery.generate(self.build, True)
        fresh = ReportDelivery(BaselineStore(self.store.path, 2, 16))
        self.assertEqual(first, fresh.generate(self.build, True))
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.store.load()[0]["last_run"], self.initial["last_run"])
        self.assertFalse(first["baseline"]["saved"])
        self.assertTrue(fresh.acknowledge(first["delivery"]["id"])["acknowledged"])
        self.assertEqual(self.store.load()[0]["last_run"], "2026-01-01T00:00:10Z")
        self.assertTrue(fresh.acknowledge(first["delivery"]["id"])["already_acknowledged"])

    def test_legacy_client_cannot_bypass_pending_delivery(self):
        first = self.delivery.generate(self.build, True)
        with self.assertRaisesRegex(ToolError, "awaits acknowledgement"):
            self.delivery.generate(self.build)
        with self.assertRaises(ToolError): self.delivery.acknowledge("wrong")
        self.assertEqual(self.delivery.generate(self.build, True), first)

    def test_new_pending_report_survives_retry_of_previous_acknowledgement(self):
        first = self.delivery.generate(self.build, True)
        self.delivery.acknowledge(first["delivery"]["id"])
        second = self.delivery.generate(self.build, True)
        self.delivery.acknowledge(first["delivery"]["id"])
        self.assertEqual(self.store.load()[0]["delivery"]["id"], second["delivery"]["id"])

    def test_concurrent_clients_receive_one_identical_delivery(self):
        results, errors = [], []
        barrier = threading.Barrier(6)
        def run():
            try:
                barrier.wait(timeout=3)
                results.append(ReportDelivery(self.store).generate(self.build, True))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(len({result["delivery"]["id"] for result in results}), 1)
        self.assertEqual(self.builds, 1)

    def test_failed_outbox_write_or_ack_keeps_previous_atomic_state(self):
        before = self.store.path.read_bytes()
        with patch("shadow.persistence.os.replace", side_effect=OSError("injected failure")):
            with self.assertRaises(ToolError): self.delivery.generate(self.build, True)
        self.assertEqual(self.store.path.read_bytes(), before)
        first = self.delivery.generate(self.build, True)
        before_ack = self.store.path.read_bytes()
        with patch("shadow.persistence.os.replace", side_effect=OSError("injected failure")):
            with self.assertRaises(ToolError): self.delivery.acknowledge(first["delivery"]["id"])
        self.assertEqual(self.store.path.read_bytes(), before_ack)
        self.assertEqual(self.delivery.generate(self.build, True), first)

    def test_failed_collection_report_ack_does_not_advance_checkpoint(self):
        def failed_build(save):
            return {"baseline": {"saved": False}, "overall_severity": "critical", "error": "Logs unavailable"}
        report = self.delivery.generate(failed_build, True)
        self.delivery.acknowledge(report["delivery"]["id"])
        self.assertEqual(self.store.load()[0]["last_run"], self.initial["last_run"])

    def test_partial_page_checkpoint_is_committed_only_on_ack(self):
        def paged_build(save):
            proposed = deepcopy(self.initial)
            proposed["log_progress"] = {"since": 1, "until": 10, "containers": {"web": {"offset": 100}}}
            save(proposed)
            return {"baseline": {"saved": False, "progress_saved": True, "pending_pages": 1}}
        report = self.delivery.generate(paged_build, True)
        self.assertIsNone(self.store.load()[0]["log_progress"])
        self.delivery.acknowledge(report["delivery"]["id"])
        saved = self.store.load()[0]
        self.assertEqual(saved["log_progress"]["containers"]["web"]["offset"], 100)
        self.assertEqual(saved["last_run"], self.initial["last_run"])

    def test_legacy_delivery_still_commits_immediately(self):
        report = self.delivery.generate(self.build)
        self.assertNotIn("delivery", report)
        self.assertEqual(self.store.load()[0]["last_run"], "2026-01-01T00:00:10Z")


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.spool = LogSpool(self.path, 1000000)
        self.container_id = "abc"
        self.container = ContainerService(find_container=lambda _: {"Id": self.container_id},
            docker_get=None, get_bounded=None, all_containers=None, managed_container_names=None,
            container_names=None, max_lines=10, max_chars=1000)
        self.data = b"".join((f"2026-01-01T00:00:01.000000001Z ERROR duplicate\n"
                              f"2026-01-01T00:00:01.000000001Z   continuation\n").encode() for _ in range(1000))
        self.fetches = 0

    def stream(self, *args):
        self.fetches += 1
        return framed(self.data, sizes=(8192,))

    def page(self, cursor):
        return self.container.log_page("web", 1, 10, cursor, self.stream,
            spool=LogSpool(self.path, 1000000))

    def test_restarts_same_timestamp_duplicates_and_multiline_use_one_docker_fetch(self):
        cursor, pages = {}, []
        while not cursor.get("complete"):
            result = self.page(cursor)
            pages.append(result["logs"])
            cursor = json.loads(json.dumps(result["cursor"]))
        self.assertEqual(self.fetches, 1)
        self.assertEqual("".join(pages).encode(), self.data)
        events = [event for page in pages for event in _group_log_lines_into_events(page)]
        self.assertEqual(len(events), 1000)
        self.assertTrue(all(event["multiline"] for event in events))

    def test_resume_reads_near_cursor_without_rehashing_prefix(self):
        cursor = {}
        for _ in range(75): cursor = self.page(cursor)["cursor"]
        self.assertGreater(cursor["offset"], 65536)
        # Damage a block strictly before the cursor. Resume must not read it;
        # corruption of the current block must still fail below.
        file = self.path / f"{cursor['spool_id']}.bin"
        with file.open("r+b") as target: target.write(b"X")
        self.page(cursor)
        with file.open("r+b") as target:
            target.seek(cursor["offset"]); target.write(b"X")
        with self.assertRaisesRegex(ToolError, "integrity"): self.page(cursor)

    def test_rotation_after_snapshot_does_not_lose_captured_events(self):
        first = self.page({})
        self.data = b"rotated away\n"
        remaining = self.page(first["cursor"])
        self.assertIn("ERROR duplicate", remaining["logs"])
        self.assertEqual(self.fetches, 1)

    def test_container_replacement_or_missing_snapshot_cannot_advance(self):
        first = self.page({})
        self.container_id = "replacement"
        with self.assertRaisesRegex(ToolError, "container changed"): self.page(first["cursor"])
        self.container_id = "abc"
        (self.path / f"{first['cursor']['spool_id']}.bin").unlink()
        with self.assertRaisesRegex(ToolError, "snapshot"): self.page(first["cursor"])
        self.assertEqual(self.fetches, 1)

    def test_incomplete_stream_and_disk_limit_leave_no_committed_snapshot(self):
        for stream in (lambda: iter([b"\x01\0\0\0\0\0\0\x10abc"]),
                       lambda: iter([b"x" * 200])):
            spool = LogSpool(self.path, 100)
            with self.assertRaises(ToolError): spool.page(stream, "abc", 1, 10, {}, 1000)
            self.assertEqual(list(self.path.iterdir()), [])

    def test_snapshot_write_failure_leaves_no_cursor(self):
        with patch("shadow.log_spool.atomic_json", side_effect=OSError("disk failure")):
            with self.assertRaises(ToolError): self.page({})
        self.assertEqual(list(self.path.iterdir()), [])

    def test_snapshot_permissions_and_cleanup_preserve_pending_intervals(self):
        first = self.page({})
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.path.iterdir()))
        live = set(self.path.iterdir())
        self.page({})  # Published capture without a saved cursor for the same interval.
        (self.path / ("b" * 32 + ".bin")).touch()
        (self.path / ("." + "c" * 32 + ".json.synthetic")).touch()
        unknown = self.path / "unrelated"
        unknown.touch()
        self.spool.cleanup({"containers": {"web": first["cursor"]}})
        self.assertEqual(set(self.path.iterdir()), live | {unknown})
        self.page(first["cursor"])
        self.spool.cleanup(None)
        self.assertEqual(list(self.path.iterdir()), [unknown])

    def test_legacy_prefix_cursor_finishes_without_spool_migration(self):
        from shadow.log_pages import read_page
        logs, cursor = read_page(self.stream(), {}, 1000)
        cursor["container_id"] = self.container_id
        result = self.page(cursor)
        self.assertNotIn("spool_id", result["cursor"])
        self.assertIn("sha256", result["cursor"])


class CorruptStateTests(unittest.TestCase):
    def test_corrupt_state_cannot_be_reinitialized_over_pending_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            store = BaselineStore(path, 2, 16)
            for payload in ('{', '[]', '{"version":2,"containers":[]}',
                            '{"version":1,"delivery":{"id":"pending"}}'):
                path.write_text(payload)
                with self.assertRaises(ToolError): store.load()
                self.assertEqual(path.read_text(), payload)

    def test_complete_multiline_event_cannot_bypass_single_event_limit(self):
        from shadow.log_pages import read_page
        data = b"2026-01-01T00:00:00Z ERROR large\n" + b"  continuation\n" * 1000 + b"2026-01-01T00:00:01Z ERROR next\n"
        with self.assertRaisesRegex(ToolError, "single log event"):
            read_page(iter([data]), {}, 100)

    def test_invalid_utf8_fails_instead_of_modifying_evidence(self):
        from shadow.log_pages import read_page
        with self.assertRaisesRegex(ToolError, "UTF-8"):
            read_page(iter([b"2026-01-01T00:00:00Z ERROR \xff\n"]), {}, 1000)


class InconsistentDeliveryTests(unittest.TestCase):
    def test_outbox_checkpoint_and_delivery_identifiers_must_be_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
            baseline = store.empty()
            identifier = "a" * 32
            valid = {"id": identifier, "report": {"delivery": {"id": identifier, "acknowledgement_required": True}},
                     "next_baseline": store.empty()}
            for field in ("id", "version", "containers"):
                outbox = deepcopy(valid)
                if field == "id": outbox["report"]["delivery"]["id"] = "wrong"
                else: outbox["next_baseline"][field] = "invalid"
                baseline["delivery"] = outbox
                store.save(baseline)
                before = store.path.read_bytes()
                delivery = ReportDelivery(store)
                with self.assertRaises(ToolError): delivery.acknowledge(identifier)
                with self.assertRaises(ToolError): delivery.generate(lambda *_: None, True)
                self.assertEqual(store.path.read_bytes(), before)
