"""Exercise the real Streamable HTTP route with the pinned MCP SDK."""

import tempfile
import unittest
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import server
from shadow.settings import Settings, SettingsError


class TransportSettingsTests(unittest.TestCase):
    def test_additional_exact_values_and_published_port(self):
        settings = Settings.from_env({
            "MCP_PORT": "9010",
            "MCP_ALLOWED_HOSTS": "shadow.example, shadow-alias:8000",
            "MCP_ALLOWED_ORIGINS": "https://console.example",
        })
        self.assertIn("127.0.0.1:9010", settings.mcp_allowed_hosts)
        self.assertIn("http://127.0.0.1:9010", settings.mcp_allowed_origins)
        self.assertIn("mcp-shadow:8000", settings.mcp_allowed_hosts)
        self.assertIn("shadow.example", settings.mcp_allowed_hosts)
        self.assertIn("shadow-alias:8000", settings.mcp_allowed_hosts)
        self.assertIn("https://console.example", settings.mcp_allowed_origins)
        self.assertNotIn("https://shadow.example", settings.mcp_allowed_origins)

    def test_rejects_wildcards_and_non_authorities(self):
        for name, value in (
            ("MCP_ALLOWED_HOSTS", "shadow.example:*"),
            ("MCP_ALLOWED_HOSTS", "https://shadow.example"),
            ("MCP_ALLOWED_ORIGINS", "https://*.example"),
            ("MCP_ALLOWED_ORIGINS", "https://console.example/path"),
            ("MCP_ALLOWED_ORIGINS", "null"),
        ):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(SettingsError, name):
                Settings.from_env({name: value})


class TransportSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamable_http_headers_and_official_client(self):
        app = server.mcp.streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as client:
                foreign_host = ("Host", "foreign.example:8010")
                foreign_origin = ("Origin", "https://foreign.example")
                allowed_origin = ("Origin", "http://127.0.0.1:8010")
                for path, headers, expected in (
                    ("/mcp", [foreign_host], 421),
                    ("/mcp", [foreign_origin], 403),
                    ("/mcp", [foreign_host, foreign_origin], 421),
                    ("/mcp", [("Host", "127.0.0.1:8010@foreign.example")], 421),
                    ("/mcp", [("Host", "127.0.0.1:8010"), foreign_host], 421),
                    ("/mcp", [("Origin", "http://127.0.0.1:8010, https://foreign.example")], 403),
                    ("/mcp", [allowed_origin, foreign_origin], 403),
                    ("/mcp", [("Origin", "")], 403),
                    ("/mcp", [("Origin", "null")], 403),
                    ("/mcp%0A", [("Origin", "")], 403),
                    ("/mcp", [allowed_origin], 200),
                ):
                    with self.subTest(path=path, headers=headers):
                        response = await client.post(
                            path, headers=[("Accept", "application/json, text/event-stream"), *headers],
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        )
                        self.assertEqual(response.status_code, expected)
                for method in ("GET", "DELETE"):
                    with self.subTest(method=method):
                        response = await client.request(method, "/mcp", headers={"Origin": "https://foreign.example"})
                        self.assertEqual(response.status_code, 403)

            async def check_headers(request):
                self.assertNotIn("origin", request.headers)

            with tempfile.TemporaryDirectory() as probe, patch.object(server, "DISK_PROBE_PATH", probe):
                for host in ("127.0.0.1:8010", "mcp-shadow:8000"):
                    with self.subTest(host=host):
                        async with (
                            httpx.AsyncClient(transport=transport, event_hooks={"request": [check_headers]}) as client,
                            streamable_http_client(f"http://{host}/mcp", http_client=client) as (read, write, _),
                            ClientSession(read, write) as session,
                        ):
                            initialized = await session.initialize()
                            self.assertEqual(initialized.serverInfo.name, "mcp-shadow")
                            tools = await session.list_tools()
                            self.assertIn("get_storage_usage", {tool.name for tool in tools.tools})
                            usage = await session.call_tool("get_storage_usage")
                            self.assertFalse(usage.isError)
                            self.assertGreater(usage.structuredContent["total_bytes"], 0)
