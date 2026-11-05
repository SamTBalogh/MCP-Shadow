"""Cross-process report locking and failure while persisting acknowledgement."""
import errno
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from shadow.baseline import BaselineStore
from shadow.delivery import ReportDelivery


def _worker(directory, action, identifier, ready, results):
    try:
        store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
        delivery = ReportDelivery(store)
        ready.wait(10)
        if action == "generate":
            def build(save):
                with (Path(directory) / "build-count").open("a") as file: file.write("built\n")
                proposed = store.empty(); proposed["last_run"] = "2026-01-01T00:00:10Z"
                save(proposed)
                return {"baseline": {"saved": True}}
            result = delivery.generate(build, True)["delivery"]["id"]
        else:
            result = delivery.acknowledge(identifier)["already_acknowledged"]
        results.put((True, result))
    except Exception as error:
        results.put((False, type(error).__name__))


class ProcessRecoveryTests(unittest.TestCase):
    def test_concurrent_processes_build_once_and_acknowledge_once(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            def run(action, identifier=None):
                ready, results = context.Event(), context.Queue()
                workers = [context.Process(target=_worker, args=(directory, action, identifier, ready, results)) for _ in range(4)]
                try:
                    for worker in workers: worker.start()
                    ready.set()
                    received = [results.get(timeout=20) for _ in workers]
                    for worker in workers:
                        worker.join(10)
                        self.assertEqual(worker.exitcode, 0)
                    self.assertTrue(all(ok for ok, _ in received), received)
                    return [result for _, result in received]
                finally:
                    for worker in workers:
                        if worker.is_alive(): worker.terminate(); worker.join(5)
                    results.close()
                    results.join_thread()
            identifiers = run("generate")
            self.assertEqual(len(set(identifiers)), 1)
            self.assertEqual((Path(directory) / "build-count").read_text().splitlines(), ["built"])
            outcomes = run("acknowledge", identifiers[0])
            self.assertEqual(outcomes.count(False), 1)
            self.assertEqual(outcomes.count(True), 3)
            self.assertIsNone(BaselineStore(Path(directory) / "baseline.json", 2, 16).load()[0]["delivery"])

    def test_enospc_before_ack_replace_preserves_outbox_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
            delivery = ReportDelivery(store)
            def build(save):
                save(store.empty())
                return {"baseline": {"saved": True}}
            report = delivery.generate(build, True)
            before = store.path.read_bytes()
            with patch("shadow.persistence.json.dump", side_effect=OSError(errno.ENOSPC, "No space left on device")):
                with self.assertRaises(ToolError): delivery.acknowledge(report["delivery"]["id"])
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(delivery.generate(build, True), report)
            self.assertTrue(delivery.acknowledge(report["delivery"]["id"])["acknowledged"])
