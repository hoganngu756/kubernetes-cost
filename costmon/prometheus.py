"""Thin wrapper around the Prometheus HTTP API. Stdlib only -- no dependency
needed for a handful of instant queries.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


def instant_query(base_url: str, expr: str) -> list[dict]:
    """Run a PromQL instant query, return the raw `result` list (metric + value)."""
    url = base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        # HTTPError is an OSError, but Prometheus *was* reached: it rejected the
        # query (e.g. a bad window) with a JSON error body. Re-raise as a
        # non-OSError so callers don't blame the connection.
        try:
            payload = json.load(exc)
        except ValueError:
            raise RuntimeError(f"Prometheus returned HTTP {exc.code} {exc.reason}") from None
    if payload["status"] != "success":
        raise RuntimeError(f"Prometheus query failed: {payload.get('error', payload)}")
    return payload["data"]["result"]
