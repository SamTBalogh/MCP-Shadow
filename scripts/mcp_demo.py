"""Query MCP Shadow with the official MCP Python client; optionally use test fixtures.

The synthetic mode is only for tests/integration.compose.yaml. It emits generated
errors and advances that temporary stack's report baseline, without a model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def consume_reports(call, container_name, *, verify_replay=False):
    # The synthetic fixture uses an in-memory sink. Real consumers need durable
    # storage for the processed data and delivery IDs before acknowledging.
    messages_by_delivery = {}
    capture_failures = 0
    for _ in range(100):
        report = await call("generate_server_report", acknowledged_delivery=True)
        if verify_replay:
            replay = await call("generate_server_report", acknowledged_delivery=True)
            if report != replay:
                raise RuntimeError("Pending delivery changed on replay")
        delivery_id = report["delivery"]["id"]
        if delivery_id not in messages_by_delivery:
            messages_by_delivery[delivery_id] = [
                event["message"]
                for container in report["log_activity"]["containers"]
                if container["name"] == container_name
                for event in container["events"]
            ]
        ack = await call("acknowledge_server_report", delivery_id=delivery_id)
        if not ack["acknowledged"]:
            raise RuntimeError("Report was not acknowledged")

        baseline = report["baseline"]
        if baseline["complete_logs"]:
            return [message for messages in messages_by_delivery.values() for message in messages]
        if baseline["log_errors"]:
            print(
                f"Log capture failed (log_errors={baseline['log_errors']}) "
                f"in delivery {delivery_id}. The log interval is incomplete.",
                file=sys.stderr,
            )
            capture_failures += 1
            if capture_failures == 3:
                raise RuntimeError(
                    "Log capture still failed after two retries. Restore capture and "
                    "request the next acknowledged report to resume the interval."
                )
            await asyncio.sleep(1)
        else:
            capture_failures = 0
    raise RuntimeError("Synthetic report interval is still incomplete after 100 deliveries")


async def run(args):
    async with streamable_http_client(args.url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, **arguments):
                result = await session.call_tool(name, arguments=arguments)
                if result.isError:
                    raise RuntimeError(f"MCP tool failed: {name}")
                data = result.structuredContent
                if data is None:
                    raise RuntimeError(f"No structured result from {name}")
                return data.get("result", data)

            tools = await session.list_tools()
            services = await call("list_managed_services")
            selected = [item for item in services if item["compose_project"] == args.project
                        and item["compose_service"] == args.service]
            if len(selected) != 1:
                raise RuntimeError("Expected one labelled service in the specified Compose project")
            name = selected[0]["container_name"]
            details = await call("inspect_container", container_name=name)
            stats = await call("get_container_stats", container_name=name)
            output = {"client": "official MCP Python SDK", "tools": len(tools.tools),
                      "service": args.service, "state": details["state"],
                      "running": details["running"], "memory_percent": stats["memory"]["percent"]}

            if args.synthetic:
                if args.service != "log-fixture":
                    raise RuntimeError("Synthetic mode requires the isolated log-fixture service")

                await consume_reports(call, name)  # Establish a baseline before emitting events.
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.post("http://log-fixture:8001/emit")
                    response.raise_for_status()
                await asyncio.sleep(1.1)  # Close Docker's whole-second interval.
                logs = await call("get_container_logs", container_name=name, lines=10)
                diagnostic = await call("diagnose_container", container_name=name, lines=10)
                messages = await consume_reports(call, name, verify_replay=True)
                events = [message for message in messages if "ERROR integration-log-" in message]
                if len(events) != 40 or len(set(events)) != 40:
                    raise RuntimeError("Expected all 40 distinct synthetic events")
                if diagnostic["model_severity_advisory"] is not None:
                    raise RuntimeError("This demo expects analysis to be disabled or unavailable")
                if "integration-log-" not in logs["logs"]:
                    raise RuntimeError("Direct logs did not include the synthetic fixture")
                output.update(synthetic=True, observed_error_events=len(events),
                              objective_severity=diagnostic["objective_severity"],
                              model_severity_advisory=None,
                              delivery_replay_verified=True, checkpoint_acknowledged=True)
            print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", default="server")
    parser.add_argument("--synthetic", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
