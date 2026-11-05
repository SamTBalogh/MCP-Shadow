import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from mcp.server.fastmcp.exceptions import ToolError

from shadow.containers import ContainerService
from shadow.discovery import ContainerRegistry, container_metadata, container_names
from shadow.settings import Settings, SettingsError
from shadow.profiles import ProfileService


class ContainerServiceTests(unittest.TestCase):
    def setUp(self):
        self.managed = {
            "Id": "abc", "Names": ["/web"], "Image": "example", "State": "running",
            "Status": "Up", "Labels": {"io.shadowofintent.mcp.enabled": "true"},
        }
        self.private = {"Id": "def", "Names": ["/private"], "Labels": {}}
        self.query = Mock(return_value=[self.managed, self.private])
        self.registry = ContainerRegistry(self.query, ttl_seconds=60)
        self.service = ContainerService(
            find_container=self.registry.find,
            docker_get=Mock(), get_bounded=Mock(),
            all_containers=self.registry.managed,
            managed_container_names=self.registry.names,
            container_names=container_names, max_lines=200, max_chars=1000,
        )

    def test_only_labelled_services_are_discovered_and_cached(self):
        self.assertEqual(self.registry.names(), {"web"})
        self.assertEqual(self.registry.find("/web")["Id"], "abc")
        with self.assertRaises(ToolError):
            self.registry.find("private")
        self.assertEqual(self.query.call_count, 1)
        self.registry.managed(force_refresh=True)
        self.assertEqual(self.query.call_count, 2)

    def test_listing_returns_expected_public_schema(self):
        self.assertEqual(self.service.list_containers(), [{
            "name": "web", "image": "example", "state": "running", "detail": "Up"
        }])
        self.assertEqual(container_metadata(self.managed)["name"], "web")

    def test_log_read_is_bounded_and_rejects_unmanaged_container(self):
        self.service.get_bounded.return_value = (b"hello", False)
        result = self.service.logs("web", 10, since=123)
        self.assertEqual(result["logs"], "hello")
        self.assertEqual(result["mode"], "differential")
        self.assertEqual(self.service.get_bounded.call_args.args[1]["tail"], "all")
        self.assertEqual(self.service.get_bounded.call_args.args[2], 1000 * 4 + 8192)
        with self.assertRaises(ToolError):
            self.service.logs("private", 10)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_limits_are_independent_of_process_environment(self):
        settings = Settings.from_env({"DOCKER_WORKERS": "8", "MAX_LOG_LINES": "1"})
        self.assertEqual(settings.docker_workers, 8)
        self.assertEqual(settings.max_log_lines, 1)
        self.assertEqual(settings.ollama_model, "qwen3.5")

    def test_invalid_configuration_names_the_setting(self):
        for name, value in (
            ("DOCKER_WORKERS", "99"), ("MAX_LOG_LINES", "0"),
            ("OLLAMA_TIMEOUT_SECONDS", "nan"), ("OLLAMA_MAX_INPUT_CHARS", "oops"),
            ("OLLAMA_API_BASE", "file:///tmp/model"), ("DOCKER_API_BASE", "http://host:99999"),
            ("STATE_DIR", ""), ("OLLAMA_MODEL", "   "),
            ("AUTO_ANALYSIS_MAX_EVENTS_TOTAL", "0"),
            ("AUTO_ANALYSIS_MAX_CONTEXT_CHARS", "invalid"),
            ("SUSTAINED_CPU_THRESHOLD_PERCENT", "inf"),
            ("SUSTAINED_METRIC_SAMPLES", "1"),
            ("SUSTAINED_MEMORY_HIGH_THRESHOLD_PERCENT", "50"),
            ("OLLAMA_THINK", "yes"), ("OLLAMA_TEMPERATURE", "nan"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(SettingsError, name):
                Settings.from_env({name: value})


class ProfileServiceTests(unittest.TestCase):
    def test_applies_service_profile_and_reports_invalid_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps({
                "version": 1,
                "profiles": {"web": {"ignore_patterns": ["healthcheck", "["]}},
            }))
            container = {"Names": ["/web"], "Labels": {
                "io.shadowofintent.mcp.log-profile": "web",
            }}
            profiles = ProfileService(path=path, find_container=lambda name: container)
            text, omitted, warnings = profiles.filter_noise("web", "healthcheck\nerror")
            self.assertEqual(text, "error")
            self.assertEqual(omitted, 1)
            self.assertTrue(any("invalid regular expression" in warning.lower() for warning in warnings))


if __name__ == "__main__":
    unittest.main()
