"""Exercise isolated tmpfs exhaustion and actual json-file Docker rotation."""
import errno
import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from shadow.baseline import BaselineStore
from shadow.containers import ContainerService
from shadow.delivery import ReportDelivery
from shadow.log_spool import LogSpool


def full_disk_acknowledgement():
    # /fault-state is a dedicated 1 MiB tmpfs, never the host state filesystem.
    with tempfile.TemporaryDirectory(dir="/fault-state") as directory:
        store = BaselineStore(Path(directory) / "baseline.json", 2, 16)
        delivery = ReportDelivery(store)
        def build(save):
            proposed = store.empty()
            proposed["last_run"] = "2026-01-01T00:00:10Z"
            save(proposed)
            return {"baseline": {"saved": True}, "synthetic": True}
        report = delivery.generate(build, True)
        before = store.path.read_bytes()
        filler = Path(directory) / "filler"
        with filler.open("wb", buffering=0) as file:
            try:
                while file.write(b"x" * 4096):
                    pass
            except OSError as error:
                assert error.errno == errno.ENOSPC
        try:
            delivery.acknowledge(report["delivery"]["id"])
        except ToolError:
            pass
        else:
            raise AssertionError("Acknowledgement unexpectedly succeeded on a full tmpfs")
        assert store.path.read_bytes() == before
        assert delivery.generate(build, True) == report
        filler.unlink()
        assert delivery.acknowledge(report["delivery"]["id"])["acknowledged"]
        assert store.load()[0]["last_run"] == "2026-01-01T00:00:10Z"
        print("Actual ENOSPC preserved the outbox; replay and acknowledgement retry succeeded")


def docker_rotation():
    with httpx.Client(timeout=60, trust_env=False) as client:
        project = os.environ["SHADOW_TEST_PROJECT"]
        containers = client.get("http://docker-proxy:2375/containers/json", params={
            "all": "true", "filters": json.dumps({"label": ["com.docker.compose.project=" + project]})}).json()
        candidates = [item for item in containers if item.get("Labels", {}).get("com.docker.compose.project") == project
                      and item.get("Labels", {}).get("com.docker.compose.service") == "rotation-fixture"]
        assert len(candidates) == 1
        identifier = candidates[0]["Id"]
        def emit(prefix, count):
            response = client.post("http://rotation-fixture:8002/emit", params={"prefix": prefix, "count": count})
            assert response.status_code == 204
        def stream(path, params):
            try:
                with client.stream("GET", "http://docker-proxy:2375" + path, params=params) as response:
                    response.raise_for_status()
                    yield from response.iter_bytes(chunk_size=512)
            except httpx.HTTPError as error:
                raise ToolError("The synthetic Docker log stream failed.") from error
        container = ContainerService(find_container=lambda _: {"Id": identifier}, docker_get=None,
            get_bounded=None, all_containers=None, managed_container_names=None, container_names=None,
            max_lines=100, max_chars=1000)
        emit("retained", 12)
        time.sleep(1.1)
        until = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            spool = LogSpool(Path(directory), 1000000, min_free_bytes=0)
            first = container.log_page("rotation-test", 0, until, {}, stream, spool)
            pages = [first["logs"]]
            cursor = first["cursor"]
            emit("rotated", 5000)
            retained = client.get(f"http://docker-proxy:2375/containers/{identifier}/logs",
                params={"stdout": "true", "stderr": "true", "timestamps": "true", "tail": "all"}).content
            assert b"retained-" not in retained, "The fixture did not rotate old records"
            while not cursor.get("complete"):
                page = container.log_page("rotation-test", 0, until, cursor, stream, spool)
                pages.append(page["logs"]); cursor = page["cursor"]
            text = "".join(pages)
            assert len(re.findall(r"ERROR retained-\d{5}", text)) == 12
            assert "rotated-" not in text
        print("Actual Docker rotation removed source records; the captured snapshot retained all 12 occurrences")

        emit("during", 40)
        time.sleep(1.1)
        until = int(time.time())
        rotation_triggered = []
        def rotating_stream(path, params):
            source = stream(path, params)
            try:
                for chunk in source:
                    if not rotation_triggered:
                        rotation_triggered.append(True)
                        emit("after", 5000)
                    yield chunk
            finally:
                source.close()
        with tempfile.TemporaryDirectory() as directory:
            spool = LogSpool(Path(directory), 1000000, min_free_bytes=0)
            try:
                page = container.log_page("rotation-test", 0, until, {}, rotating_stream, spool)
            except ToolError:
                assert rotation_triggered
                assert not list(Path(directory).glob("*.json"))
                print("Rotation during capture caused a visible stream failure without a published snapshot")
            else:
                pages = [page["logs"]]
                while not page["cursor"].get("complete"):
                    page = container.log_page("rotation-test", 0, until, page["cursor"], stream, spool)
                    pages.append(page["logs"])
                assert rotation_triggered
                assert "after-" not in "".join(pages), "Newer records escaped the fixed cutoff"
                print("Rotation occurred during stream consumption; the retained interval completed, with source completeness still unprovable")
            current = client.get(f"http://docker-proxy:2375/containers/{identifier}/logs",
                params={"stdout": "true", "timestamps": "true", "tail": "all"}).content
            assert b"during-" not in current


full_disk_acknowledgement()
docker_rotation()
