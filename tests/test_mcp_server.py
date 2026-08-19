"""Tests for the MCP server, driven over its real stdio JSON-RPC loop.

These deliberately go through `serve()` with fake stdin/stdout rather than
calling the tool methods directly: the claim being tested is "this is a
working MCP server", and the protocol framing is the part of that most likely
to be wrong.
"""
import io
import json
import unittest
from unittest import mock

from costmon.mcp_server import SUPPORTED_PROTOCOL_VERSIONS, CostmonServer
from costmon.metrics import WorkloadMetrics

MIB = 2**20

FAKE_METRICS = [
    # over-provisioned on both axes -> flagged
    WorkloadMetrics("cost-demo", "idle-hog", 1.0, 0.0, 1024 * MIB, 1 * MIB),
    # under on CPU, over on memory -> flagged on memory only
    WorkloadMetrics("cost-demo", "cruncher", 0.05, 0.2, 64 * MIB, 16 * MIB),
    # honest -> not flagged
    WorkloadMetrics("cost-demo", "worker", 0.13, 0.1, 64 * MIB, 48 * MIB),
]


def drive(requests, server=None):
    """Feed newline-delimited JSON-RPC through serve(), collect the replies."""
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    (server or CostmonServer()).serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def call(tool, arguments=None):
    with mock.patch("costmon.mcp_server.pull_workload_metrics", return_value=FAKE_METRICS):
        responses = drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments or {}},
                }
            ]
        )
    return responses[0]["result"]


class ProtocolTests(unittest.TestCase):
    def test_handshake_negotiates_version_and_advertises_tools(self):
        responses = drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
        )

        # The notification must NOT produce a reply -- 3 messages in, 2 out.
        self.assertEqual(len(responses), 2)

        init = responses[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertEqual(init["serverInfo"]["name"], "costmon")
        self.assertIn("tools", init["capabilities"])

        tools = responses[1]["result"]["tools"]
        self.assertEqual(
            [t["name"] for t in tools],
            ["list_workloads", "get_cost_report", "get_rightsizing_recommendations"],
        )
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["description"])

    def test_unknown_protocol_version_falls_back_to_a_supported_one(self):
        responses = drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"},
                }
            ]
        )
        self.assertIn(responses[0]["result"]["protocolVersion"], SUPPORTED_PROTOCOL_VERSIONS)

    def test_unknown_method_returns_a_jsonrpc_error_with_the_request_id(self):
        responses = drive([{"jsonrpc": "2.0", "id": 7, "method": "tools/nope"}])
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[0]["id"], 7)

    def test_malformed_json_returns_a_parse_error_without_killing_the_loop(self):
        stdout = io.StringIO()
        CostmonServer().serve(
            io.StringIO('{not json}\n{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n'), stdout
        )
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})  # loop survived, ping answered

    def test_unknown_tool_is_a_tool_error(self):
        result = call("no_such_tool")
        self.assertTrue(result["isError"])

    def test_unreachable_prometheus_is_a_tool_error_not_a_crash(self):
        with mock.patch(
            "costmon.mcp_server.pull_workload_metrics",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            responses = drive(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "get_cost_report", "arguments": {}},
                    }
                ]
            )
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("port-forward", result["content"][0]["text"])


class ToolTests(unittest.TestCase):
    def test_list_workloads_reports_requests_and_usage(self):
        payload = call("list_workloads")["structuredContent"]

        self.assertEqual(payload["workload_count"], 3)
        idle = next(w for w in payload["workloads"] if w["workload"] == "idle-hog")
        self.assertEqual(idle["cpu_request_cores"], 1.0)
        self.assertEqual(idle["cpu_usage_cores"], 0.0)
        self.assertEqual(idle["mem_request_mib"], 1024.0)

    def test_cost_report_ranks_by_waste_and_reports_overprovisioned_share(self):
        payload = call("get_cost_report")["structuredContent"]

        self.assertEqual([w["workload"] for w in payload["workloads"]][0], "idle-hog")
        self.assertEqual(payload["totals"]["workload_count"], 3)
        self.assertEqual(payload["totals"]["overprovisioned_count"], 2)
        self.assertAlmostEqual(payload["totals"]["overprovisioned_share"], 0.667, places=2)

    def test_recommendations_omit_the_axis_that_must_not_be_cut(self):
        payload = call("get_rightsizing_recommendations")["structuredContent"]
        by_name = {r["workload"]: r for r in payload["recommendations"]}

        self.assertEqual(payload["recommendation_count"], 2)
        self.assertNotIn("worker", by_name)  # honest workload gets no recommendation
        # cruncher needs MORE cpu, so no cpu key at all -- only memory is cut.
        self.assertNotIn("cpu", by_name["cruncher"])
        self.assertAlmostEqual(by_name["cruncher"]["memory"]["recommended_request_mib"], 20.8)

    def test_threshold_argument_changes_what_is_flagged(self):
        strict = call("get_cost_report", {"threshold": 0.8})["structuredContent"]
        self.assertEqual(strict["totals"]["overprovisioned_count"], 3)  # worker now flagged too

    def test_text_content_mirrors_structured_content(self):
        result = call("list_workloads")

        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])


if __name__ == "__main__":
    unittest.main()
