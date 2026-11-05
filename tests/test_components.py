import unittest
import json
from unittest.mock import patch

from shadow.events import (
    _classify_event_relevance,
    _group_log_lines_into_events,
    _redact_sensitive_text,
)
from shadow.metrics import _evaluate_sustained_metrics, _update_sustained_metric_entry


class EventTests(unittest.TestCase):
    def test_multiline_traceback_is_one_relevant_event(self):
        logs = (
            "2026-01-01T00:00:00Z Traceback (most recent call last):\n"
            '  File "app.py", line 1, in main\n'
            "ValueError: failed\n"
            "2026-01-01T00:00:01Z INFO ready\n"
        )
        events = _group_log_lines_into_events(logs)
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0]["multiline"])
        self.assertTrue(_classify_event_relevance(events[0])["relevant"])
        self.assertFalse(_classify_event_relevance(events[1])["relevant"])

    def test_credentials_are_redacted(self):
        result = _redact_sensitive_text("password=secret token=abc")
        self.assertNotIn("secret", result)
        self.assertNotIn("abc", result)

    def test_json_credentials_are_redacted_and_json_remains_valid(self):
        source = '{"token": "secret value", "password": "another secret"}'
        parsed = json.loads(_redact_sensitive_text(source))
        self.assertEqual(parsed, {"token": "[REDACTED]", "password": "[REDACTED]"})


class MetricsTests(unittest.TestCase):
    def test_high_cpu_must_persist_and_resets_when_sample_is_missing(self):
        snapshot = {
            "running": True,
            "metrics_available": True,
            "cpu_percent": 95,
            "memory": {"percent": 20},
        }
        with patch.dict("os.environ", {"SUSTAINED_METRIC_SAMPLES": "3"}):
            first = _update_sustained_metric_entry(snapshot, {}, "now")
            self.assertEqual(
                _evaluate_sustained_metrics(first)["severity"], "no_indication"
            )
            second = _update_sustained_metric_entry(snapshot, first, "now")
            third = _update_sustained_metric_entry(snapshot, second, "now")
            self.assertEqual(_evaluate_sustained_metrics(third)["severity"], "low")
            missing = _update_sustained_metric_entry({"running": True}, third, "now")
            self.assertEqual(missing["consecutive_high_cpu"], 0)


if __name__ == "__main__":
    unittest.main()
