"""A full capture budget must preserve pending pages and objective progress."""
import errno
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from shadow.log_spool import LogSpool
from shadow.storage import SnapshotBudget
from shadow.settings import Settings, SettingsError
from test_log_pages import framed


class StorageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.data = b"2026-01-01T00:00:00Z ERROR retained event\n" * 200

    def test_global_budget_counts_other_containers_and_never_deletes_pending_data(self):
        first = LogSpool(self.path, 20000, total_max_bytes=30000, min_free_bytes=0)
        _, cursor = first.page(lambda: framed(self.data), "first", 1, 10, {}, 1000)
        before = {path.name: path.read_bytes() for path in self.path.iterdir()}
        second = LogSpool(self.path, 20000, total_max_bytes=len(self.data) + 2500, min_free_bytes=0)
        with self.assertRaisesRegex(ToolError, "global LOG_SPOOL_TOTAL_MAX_BYTES"):
            second.page(lambda: framed(self.data), "second", 1, 10, {}, 1000)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.path.iterdir()})
        constrained = LogSpool(self.path, 20000, total_max_bytes=1, min_free_bytes=10**20)
        logs, next_cursor = constrained.page(lambda: self.fail("Must not refetch"), "first", 1, 10, cursor, 1000)
        self.assertIn("retained event", logs)
        self.assertGreater(next_cursor["offset"], cursor["offset"])
        self.assertEqual(constrained.status()["status"], "capture_blocked")

    def test_disk_headroom_checked_during_capture_closes_stream_and_removes_partial_file(self):
        closed = []
        def stream():
            try:
                yield self.data[:100]
                yield self.data[100:]
            finally:
                closed.append(True)
        usage = shutil._ntuple_diskusage
        with patch("shadow.storage.shutil.disk_usage", side_effect=[usage(10000, 0, 10000), usage(10000, 0, 10000), usage(10000, 9999, 1)]):
            with self.assertRaisesRegex(ToolError, "LOG_SPOOL_MIN_FREE_BYTES"):
                LogSpool(self.path, 20000, min_free_bytes=100).page(stream, "first", 1, 10, {}, 1000)
        self.assertEqual(closed, [True])
        self.assertEqual(list(self.path.iterdir()), [])

    def test_oversize_first_page_does_not_consume_quota_on_retry(self):
        data = b"2026-01-01T00:00:00Z ERROR " + b"x" * 20000 + b"\n"
        spool = LogSpool(self.path, 30000, total_max_bytes=24000, min_free_bytes=0)
        for _ in range(3):
            with self.assertRaisesRegex(ToolError, "single log event"):
                spool.page(lambda: framed(data), "fixture", 1, 10, {}, 1000)
            self.assertEqual(list(self.path.iterdir()), [])

    def test_all_snapshot_files_count_and_status_contains_no_log_contents(self):
        (self.path / "unknown-owned-file").write_bytes(b"sensitive log text")
        status = SnapshotBudget(self.path, 1000, 0).status()
        self.assertEqual(status["snapshot_bytes"], 18)
        self.assertNotIn("sensitive", json.dumps(status))
        self.assertEqual((self.path / "unknown-owned-file").read_bytes(), b"sensitive log text")

    def test_global_quota_and_free_space_configuration_is_validated(self):
        for name, value in (("LOG_SPOOL_TOTAL_MAX_BYTES", "0"), ("LOG_SPOOL_TOTAL_MAX_BYTES", "1.5"),
                            ("LOG_SPOOL_MIN_FREE_BYTES", "-1"), ("LOG_SPOOL_MIN_FREE_BYTES", "nan")):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(SettingsError, name):
                Settings.from_env({name: value})
