"""Tests for bounded streaming and shared Docker connections."""
import types
import unittest

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from shadow.docker_client import DockerClient


class CountingStream(httpx.SyncByteStream):
    def __init__(self):
        self.chunks_read = 0
        self.closed = False

    def __iter__(self):
        for _ in range(100):
            self.chunks_read += 1
            yield b"x" * 8192

    def close(self):
        self.closed = True


class DockerClientTests(unittest.TestCase):
    def test_stream_stops_at_limit_and_closes_response(self):
        stream = CountingStream()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        http = types.SimpleNamespace(
            Client=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
            HTTPStatusError=httpx.HTTPStatusError,
            RequestError=httpx.RequestError,
        )
        client = DockerClient("http://docker-proxy", http)
        try:
            content, truncated = client.get_bounded("/logs", {}, 10000)
            self.assertEqual(len(content), 10000)
            self.assertTrue(truncated)
            self.assertEqual(stream.chunks_read, 2)
            self.assertTrue(stream.closed)
        finally:
            client.close()

    def test_proxy_errors_are_reported(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(403))
        http = types.SimpleNamespace(
            Client=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
            HTTPStatusError=httpx.HTTPStatusError,
            RequestError=httpx.RequestError,
        )
        client = DockerClient("http://docker-proxy", http)
        try:
            with self.assertRaises(ToolError):
                client.get_bounded("/logs", {}, 100)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
