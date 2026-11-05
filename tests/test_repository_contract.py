from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_log_profiles_are_reusable(self) -> None:
        path = ROOT / "config" / "log-profiles.json"
        data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data.get("version"), 1)

        profiles = data.get("profiles")
        self.assertIsInstance(profiles, dict)

        expected = {
            "generic": 0,
            "nginx": 0,
            "homepage": 2,
            "code-server": 1,
            "docker-proxy": 4,
        }

        self.assertEqual(set(profiles), set(expected))

        for name, pattern_count in expected.items():
            profile = profiles[name]
            self.assertIsInstance(profile, dict)

            patterns = profile.get("ignore_patterns")
            self.assertIsInstance(patterns, list)
            self.assertEqual(len(patterns), pattern_count)

    def test_internal_identifiers_are_english(self) -> None:
        server_path = ROOT / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))

        expected_parameters = {
            "_get_container_logs_data": ["container_name", "lines", "since"],
            "_analyze_logs_with_ollama": ["container_name", "logs"],
            "_filter_profile_noise": ["container_name", "value"],
            "_objective_container_snapshot": ["container_name"],
            "_container_snapshot": ["container_name", "lines"],
        }

        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for function_name, parameters in expected_parameters.items():
            node = functions[function_name]

            self.assertEqual([argument.arg for argument in node.args.args], parameters)

        analyze_tool = functions["_analyze_container_logs"]

        local_names = {
            node.id for node in ast.walk(analyze_tool) if isinstance(node, ast.Name)
        }

        self.assertIn("input_truncated", local_names)

    def test_compose_uses_public_defaults(self) -> None:
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

        required_markers = (
            "${SERVER_NAME:-mcp-shadow}",
            "${MCP_BIND_ADDRESS:-127.0.0.1}",
            "${MCP_PORT:-8010}",
            "${MCP_STATE_PATH:-./state}",
            "${DISK_PROBE_SOURCE:-./disk-probe}",
            "${MONITORING_NETWORK:-homelab-monitoring}",
            "${OLLAMA_API_BASE:-http://ollama:11434}",
            "${OLLAMA_MODEL:-qwen3.5}",
        )

        for marker in required_markers:
            self.assertIn(marker, compose)

    def test_public_deployment_files_have_no_private_defaults(self) -> None:
        public_files = (
            ROOT / "server.py",
            ROOT / "compose.yaml",
            ROOT / ".env.example",
            ROOT / "Dockerfile",
            ROOT / "requirements.txt",
            ROOT / "config" / "log-profiles.json",
        )

        forbidden = (r"/(?:home|Users|srv)/", r"\b192\.168\.\d+\.\d+\b",
                     r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b")

        for path in public_files:
            content = path.read_text(encoding="utf-8")

            for value in forbidden:
                self.assertIsNone(re.search(value, content),
                                  msg=f"Private deployment default in {path.relative_to(ROOT)}")

    def test_local_environment_is_gitignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = {
            line.strip()
            for line in gitignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn(".env", lines)
        self.assertIn(".env.*", lines)
        self.assertIn("!/.env.example", lines)
        self.assertNotIn("!.env.example", lines)

    def test_runtime_state_is_gitignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("state/", gitignore)
        self.assertIn("backups/", gitignore)


if __name__ == "__main__":
    unittest.main()
