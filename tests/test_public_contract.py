from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "server.py"

EXPECTED_TOOLS = {
    "get_server_status",
    "get_storage_usage",
    "list_managed_services",
    "list_containers",
    "inspect_container",
    "get_container_stats",
    "get_container_logs",
    "analyze_container_logs",
    "diagnose_container",
    "generate_server_report",
    "acknowledge_server_report",
}

EXPECTED_RESOURCE = {"function": "server_inventory", "uri": "server://inventory"}

EXPECTED_ROUTE = {"function": "health", "path": "/health", "methods": ["GET"]}


def decorator_name(decorator: ast.expr) -> str:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator

    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return f"{target.value.id}.{target.attr}"

    return ""


def literal(value: ast.AST):
    try:
        return ast.literal_eval(value)
    except (ValueError, TypeError):
        return None


class PublicContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SERVER_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_exact_mcp_tool_names(self) -> None:
        tools = set()

        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                if decorator_name(decorator) == "mcp.tool":
                    tools.add(node.name)

        self.assertEqual(tools, EXPECTED_TOOLS)

    def test_inventory_resource_contract(self) -> None:
        resources = []

        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                if (
                    decorator_name(decorator) == "mcp.resource"
                    and isinstance(decorator, ast.Call)
                    and decorator.args
                ):
                    resources.append(
                        {"function": node.name, "uri": literal(decorator.args[0])}
                    )

        self.assertEqual(resources, [EXPECTED_RESOURCE])

    def test_tools_use_english_internal_payloads_where_available(self) -> None:
        expected_targets = {
            "get_server_status": "_get_server_status",
            "get_storage_usage": "_get_storage_usage",
            "list_containers": "_list_containers",
            "inspect_container": "_inspect_container",
            "get_container_stats": "_get_container_stats",
            "get_container_logs": "_get_container_logs_data",
        }
        functions = {
            node.name: node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef)
        }
        for name, target in expected_targets.items():
            with self.subTest(tool=name):
                returns = [
                    node
                    for node in functions[name].body
                    if isinstance(node, ast.Return)
                ]
                self.assertEqual(len(returns), 1)
                self.assertIsInstance(returns[0].value, ast.Call)
                self.assertIsInstance(returns[0].value.func, ast.Name)
                self.assertEqual(returns[0].value.func.id, target)

    def test_health_route_contract(self) -> None:
        routes = []

        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                if (
                    decorator_name(decorator) == "mcp.custom_route"
                    and isinstance(decorator, ast.Call)
                    and decorator.args
                ):
                    methods = None

                    for keyword in decorator.keywords:
                        if keyword.arg == "methods":
                            methods = literal(keyword.value)

                    routes.append(
                        {
                            "function": node.name,
                            "path": literal(decorator.args[0]),
                            "methods": methods,
                        }
                    )

        self.assertEqual(routes, [EXPECTED_ROUTE, {
            "function": "readiness", "path": "/ready", "methods": ["GET"]
        }])

    def test_public_callable_parameters(self) -> None:
        expected = {
            "health": {"parameters": ["request"], "defaults": {}},
            "get_server_status": {"parameters": [], "defaults": {}},
            "get_storage_usage": {"parameters": [], "defaults": {}},
            "list_managed_services": {"parameters": [], "defaults": {}},
            "list_containers": {"parameters": [], "defaults": {}},
            "inspect_container": {"parameters": ["container_name"], "defaults": {}},
            "get_container_stats": {"parameters": ["container_name"], "defaults": {}},
            "get_container_logs": {
                "parameters": ["container_name", "lines"],
                "defaults": {"lines": 100},
            },
            "analyze_container_logs": {
                "parameters": ["container_name", "lines"],
                "defaults": {"lines": 100},
            },
            "diagnose_container": {
                "parameters": ["container_name", "lines"],
                "defaults": {"lines": 100},
            },
            "generate_server_report": {
                "parameters": ["lines_per_container", "acknowledged_delivery"],
                "defaults": {"lines_per_container": 50, "acknowledged_delivery": False},
            },
            "acknowledge_server_report": {"parameters": ["delivery_id"], "defaults": {}},
            "server_inventory": {"parameters": [], "defaults": {}},
        }

        discovered = {}

        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            if node.name not in expected:
                continue

            parameters = [argument.arg for argument in node.args.args]

            defaults = {}

            default_offset = len(parameters) - len(node.args.defaults)

            for parameter, default_node in zip(
                parameters[default_offset:], node.args.defaults
            ):
                defaults[parameter] = literal(default_node)

            discovered[node.name] = {"parameters": parameters, "defaults": defaults}

        self.assertEqual(discovered, expected)

    def test_label_discovery_constants_exist(self) -> None:
        required = {
            "MCP_ENABLED_LABEL",
            "MCP_NAME_LABEL",
            "MCP_DESCRIPTION_LABEL",
            "MCP_LOG_PROFILE_LABEL",
            "MCP_EXPECTED_STOPPED_LABEL",
        }

        assigned = set()

        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)

            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    assigned.add(node.target.id)

        self.assertTrue(required.issubset(assigned))

    def test_legacy_allowlist_symbols_are_absent(self) -> None:
        self.assertNotIn("ALLOWED_CONTAINERS", self.source)
        self.assertNotIn("EXPECTED_STOPPED_CONTAINERS", self.source)

    def test_service_specific_filter_is_absent(self) -> None:
        self.assertNotIn("_filter_service_specific_noise", self.source)


if __name__ == "__main__":
    unittest.main()
