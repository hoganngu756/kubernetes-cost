"""Thin wrapper around the Prometheus HTTP API. Stdlib only -- no dependency
needed for a handful of instant queries.
"""
import json
import urllib.parse
import urllib.request


def instant_query(base_url: str, expr: str) -> list[dict]:
    """Run a PromQL instant query, return the raw `result` list (metric + value)."""
    url = base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.load(resp)
    if payload["status"] != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload["data"]["result"]
