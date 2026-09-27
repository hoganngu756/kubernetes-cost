"""Tests for the Prometheus HTTP wrapper's error handling (costmon.prometheus).

A bad query and an unreachable server both surface from urllib as OSError
subclasses, but they need opposite advice: one is "fix your arguments", the
other "is port-forward running?". These pin down that they stay apart.
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from costmon.prometheus import instant_query


def _http_error(code, body):
    return urllib.error.HTTPError("http://prom/api/v1/query", code, "Bad Request", {}, io.BytesIO(body))


class InstantQueryErrorTests(unittest.TestCase):
    def test_rejected_query_raises_prometheus_message_not_a_connection_error(self):
        body = json.dumps(
            {"status": "error", "errorType": "bad_data", "error": 'invalid parameter "query": bad duration'}
        ).encode()
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(RuntimeError) as ctx:
                instant_query("http://prom", "up[foo]")

        self.assertNotIsInstance(ctx.exception, OSError)
        self.assertIn("bad duration", str(ctx.exception))

    def test_http_error_without_json_body_still_is_not_a_connection_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(502, b"<html>bad gateway</html>")):
            with self.assertRaises(RuntimeError) as ctx:
                instant_query("http://prom", "up")

        self.assertIn("502", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
