"""Read-only access to the Docker proxy."""

import threading
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError


class DockerHTTPError(ToolError):
    """A failed proxy request with a machine-readable HTTP status."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"The Docker proxy returned HTTP {status_code}.")


class ContainerNotFoundError(ToolError):
    """Confirmed absence from a container lookup or inspection."""


class DockerClient:
    def __init__(self, base_url: str, http: Any, timeout: float = 8):
        self.base_url = base_url
        self.http = http
        self.timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        with self._lock:
            if self._client is None:
                self._client = self.http.Client(
                    base_url=self.base_url, timeout=self.timeout, trust_env=False
                )
            return self._client

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def get(self, path: str, params: dict[str, str] | None = None):
        try:
            response = self._get_client().get(path, params=params)
            response.raise_for_status()
            return response
        except self.http.HTTPStatusError as exception:
            raise DockerHTTPError(exception.response.status_code) from exception
        except self.http.RequestError as exception:
            raise ToolError("The Docker proxy could not be reached.") from exception

    def get_bounded(self, path: str, params: dict[str, str], limit: int):
        """Read at most limit bytes, closing the response as soon as it overflows."""
        data = bytearray()
        try:
            with self._get_client().stream("GET", path, params=params) as response:
                response.raise_for_status()
                for chunk in response.iter_raw(chunk_size=8192):
                    remaining = limit - len(data)
                    if len(chunk) > remaining:
                        data.extend(chunk[:remaining])
                        return bytes(data), True
                    data.extend(chunk)
            return bytes(data), False
        except self.http.HTTPStatusError as exception:
            raise DockerHTTPError(exception.response.status_code) from exception
        except self.http.RequestError as exception:
            raise ToolError("The Docker proxy could not be reached.") from exception

    def iter_bytes(self, path: str, params: dict[str, str]):
        """Stream a response and close it even when the caller stops early."""
        try:
            with self._get_client().stream("GET", path, params=params) as response:
                response.raise_for_status()
                yield from response.iter_raw(chunk_size=8192)
        except self.http.HTTPStatusError as exception:
            raise DockerHTTPError(exception.response.status_code) from exception
        except self.http.RequestError as exception:
            raise ToolError("The Docker proxy could not be reached.") from exception
