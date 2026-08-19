"""MCP server exposing the cost pipeline to LLM agents.

Speaks the Model Context Protocol over stdio as newline-delimited JSON-RPC
2.0. Hand-rolled against the protocol rather than pulling in the MCP SDK, to
keep this project's zero-dependency property (same reasoning as
costmon/prometheus.py): the surface an agent needs is three tools and four
protocol methods, and the SDK would be more machinery than that is worth.

Register with an MCP client, e.g.:

    claude mcp add costmon -- python3 -m costmon.mcp_server

or in a client config file:

    {"mcpServers": {"costmon": {"command": "python3",
                                "args": ["-m", "costmon.mcp_server"]}}}

Needs `make port-forward` running, same as the CLI. A client may launch this
server at any time, so an unreachable Prometheus is reported as a tool error
with that hint rather than being treated as a fatal condition.
"""
import argparse
import json
import sys
from typing import Any

from costmon.cost import EFFICIENCY_THRESHOLD, rank_by_waste
from costmon.metrics import pull_workload_metrics

SERVER_NAME = "costmon"
SERVER_VERSION = "0.1.0"

# Protocol revisions this server is known to speak. Clients name the one they
# want in `initialize`; we echo it back when we recognise it, otherwise answer
# with our newest and let the client decide whether it can proceed.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

MIB = 2**20

_QUERY_PROPERTIES: dict[str, Any] = {
    "prometheus_url": {
        "type": "string",
        "description": "Base URL of the Prometheus HTTP API. Defaults to the server's setting.",
    },
    "namespace": {"type": "string", "description": "Kubernetes namespace to analyse."},
    "window": {
        "type": "string",
        "description": "Analysis window for the usage statistic, e.g. '15m', '6h', '7d'.",
    },
}

_THRESHOLD_PROPERTY: dict[str, Any] = {
    "threshold": {
        "type": "number",
        "description": (
            "Efficiency (usage/request, 0-1) below which a workload counts as "
            "over-provisioned. Defaults to 0.4."
        ),
    },
}


def _schema(with_threshold: bool) -> dict[str, Any]:
    properties = dict(_QUERY_PROPERTIES)
    if with_threshold:
        properties.update(_THRESHOLD_PROPERTY)
    # Every argument is optional: the server carries usable defaults, so an
    # agent can call any tool with {} and still get a real answer.
    return {"type": "object", "properties": properties, "required": []}


# (tool name == method name on CostmonServer, description, takes a threshold)
TOOLS = (
    (
        "list_workloads",
        "Live per-workload resource requests vs. actual usage for a namespace, summed "
        "across each Deployment's pods. Use for utilization questions.",
        False,
    ),
    (
        "get_cost_report",
        "Priced efficiency report: per-workload CPU/memory efficiency, monthly cost and "
        "monthly waste in USD, ranked by waste, plus fleet totals and the share of "
        "workloads that are over-provisioned.",
        True,
    ),
    (
        "get_rightsizing_recommendations",
        "Concrete request changes for over-provisioned workloads only: current vs. "
        "recommended CPU/memory requests. Use to answer 'what should I change?'.",
        True,
    ),
)


class CostmonServer:
    """The three tools, plus enough JSON-RPC to be a valid MCP stdio server."""

    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        namespace: str = "cost-demo",
        window: str = "15m",
    ):
        self.defaults = {
            "prometheus_url": prometheus_url,
            "namespace": namespace,
            "window": window,
        }

    # ----- tools -------------------------------------------------------

    def _default(self, args: dict, key: str) -> str:
        return args.get(key) or self.defaults[key]

    def _pull(self, args: dict) -> list:
        return pull_workload_metrics(
            self._default(args, "prometheus_url"),
            self._default(args, "namespace"),
            self._default(args, "window"),
        )

    def list_workloads(self, args: dict) -> dict:
        metrics = self._pull(args)
        return {
            "namespace": self._default(args, "namespace"),
            "window": self._default(args, "window"),
            "workload_count": len(metrics),
            "note": "Values are summed across all pods of each Deployment.",
            "workloads": [
                {
                    "workload": m.workload,
                    "cpu_request_cores": round(m.cpu_request_cores, 4),
                    "cpu_usage_cores": round(m.cpu_usage_cores, 4),
                    "mem_request_mib": round(m.mem_request_bytes / MIB, 1),
                    "mem_usage_mib": round(m.mem_usage_bytes / MIB, 1),
                }
                for m in metrics
            ],
        }

    def get_cost_report(self, args: dict) -> dict:
        threshold = args.get("threshold", EFFICIENCY_THRESHOLD)
        ranked = rank_by_waste(self._pull(args), threshold)
        flagged = [c for c in ranked if c.cpu_overprovisioned or c.mem_overprovisioned]
        return {
            "namespace": self._default(args, "namespace"),
            "window": self._default(args, "window"),
            "threshold": threshold,
            "currency": "USD",
            "totals": {
                "monthly_cost_usd": round(sum(c.monthly_cost_usd for c in ranked), 2),
                "monthly_waste_usd": round(sum(c.monthly_waste_usd for c in ranked), 2),
                "workload_count": len(ranked),
                "overprovisioned_count": len(flagged),
                "overprovisioned_share": round(len(flagged) / len(ranked), 3) if ranked else 0.0,
            },
            "workloads": [
                {
                    "workload": c.workload,
                    "cpu_efficiency": _round_or_none(c.cpu_efficiency),
                    "mem_efficiency": _round_or_none(c.mem_efficiency),
                    "cpu_overprovisioned": c.cpu_overprovisioned,
                    "mem_overprovisioned": c.mem_overprovisioned,
                    "monthly_cost_usd": round(c.monthly_cost_usd, 2),
                    "monthly_waste_usd": round(c.monthly_waste_usd, 2),
                }
                for c in ranked
            ],
        }

    def get_rightsizing_recommendations(self, args: dict) -> dict:
        threshold = args.get("threshold", EFFICIENCY_THRESHOLD)
        ranked = rank_by_waste(self._pull(args), threshold)
        recommendations = []
        for c in ranked:
            if not (c.cpu_overprovisioned or c.mem_overprovisioned):
                continue
            entry: dict[str, Any] = {
                "workload": c.workload,
                "monthly_waste_usd": round(c.monthly_waste_usd, 2),
            }
            # An axis is present only if it is safe to cut. Omitting it is the
            # point: a workload can be over on memory and under on CPU at once.
            if c.cpu_overprovisioned:
                entry["cpu"] = {
                    "current_request_cores": round(c.cpu_request_cores, 4),
                    "recommended_request_cores": round(c.recommended_cpu_request_cores, 4),
                }
            if c.mem_overprovisioned:
                entry["memory"] = {
                    "current_request_mib": round(c.mem_request_bytes / MIB, 1),
                    "recommended_request_mib": round(c.recommended_mem_request_bytes / MIB, 1),
                }
            recommendations.append(entry)
        return {
            "namespace": self._default(args, "namespace"),
            "threshold": threshold,
            "note": (
                "Only the over-provisioned axis is listed; an axis absent from an entry "
                "is correctly sized or under-provisioned and must not be cut. A "
                "recommendation near zero means the workload is idle and should be "
                "deleted rather than resized."
            ),
            "recommendation_count": len(recommendations),
            "recommendations": recommendations,
        }

    # ----- JSON-RPC ----------------------------------------------------

    @staticmethod
    def tool_definitions() -> list[dict]:
        return [
            {"name": name, "description": description, "inputSchema": _schema(with_threshold)}
            for name, description, with_threshold in TOOLS
        ]

    def _call_tool(self, name: str, args: dict) -> dict:
        if name not in {tool[0] for tool in TOOLS}:
            return _tool_error(f"unknown tool {name!r}")
        try:
            payload = getattr(self, name)(args)
        except OSError as exc:
            # Prometheus unreachable. Reported as a tool error rather than a
            # protocol error so the agent can read the hint and retry.
            return _tool_error(
                f"could not reach Prometheus: {exc}. Is `make port-forward` running?"
            )
        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "structuredContent": payload,
            "isError": False,
        }

    def handle(self, request: Any) -> dict | None:
        """Return a response, or None for a notification (which gets no reply)."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")

        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        # Notifications carry no id and must never be answered. Every method
        # below arrives as an id-bearing request, so this needs no exception.
        if req_id is None:
            return None

        if method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = (
                requested
                if requested in SUPPORTED_PROTOCOL_VERSIONS
                else SUPPORTED_PROTOCOL_VERSIONS[-1]
            )
            return _result(
                req_id,
                {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return _result(req_id, {})
        if method == "tools/list":
            return _result(req_id, {"tools": self.tool_definitions()})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return _error(req_id, -32602, "params.name must be a string")
            return _result(req_id, self._call_tool(name, params.get("arguments") or {}))

        return _error(req_id, -32601, f"method not found: {method}")

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, _error(None, -32700, "parse error"))
                continue
            response = self.handle(request)
            if response is not None:
                _write(stdout, response)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


def _result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _write(stdout, message: dict) -> None:
    # stdout is the protocol channel: nothing but JSON-RPC may be written here.
    stdout.write(json.dumps(message) + "\n")
    stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="costmon-mcp", description="MCP stdio server for the costmon pipeline"
    )
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--namespace", default="cost-demo")
    parser.add_argument("--window", default="15m")
    args = parser.parse_args(argv)

    CostmonServer(args.prometheus_url, args.namespace, args.window).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
