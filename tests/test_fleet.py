"""Tests that the demo fleet in workloads/ is the fleet the README describes.

The headline claim -- 3 of 10 workloads (30%) over-provisioned across a 40-pod
cluster -- is a property of the manifests, not of the Python. Nothing else in
the suite would notice if a replica count or a request were edited, so the
claim would quietly go stale. These tests read the manifests and re-derive it.

Requests and replica counts come from the YAML. Per-pod usage cannot: it only
exists on a running cluster. So the engineered duty-cycle figures each manifest
documents in its header comment are restated here as EXPECTED_USAGE, and the
real cost code decides what that combination flags.
"""
import re
import unittest
from pathlib import Path

from costmon.cost import EFFICIENCY_THRESHOLD, rank_by_waste
from costmon.metrics import WorkloadMetrics

MIB = 2**20
WORKLOAD_DIR = Path(__file__).resolve().parent.parent / "workloads"

EXPECTED_DEPLOYMENTS = 10
EXPECTED_PODS = 40

# The three deliberately misprovisioned workloads, and the axis each is wrong on.
EXPECTED_FLAGGED = {
    "idle-hog": ("cpu", "mem"),
    "overprovisioned-web": ("cpu", "mem"),
    # Over on memory, UNDER on CPU -- must never be flagged for a CPU cut.
    "underprovisioned-cruncher": ("mem",),
}

# Per-pod steady-state usage each manifest is engineered to produce, from the
# duty-cycle math in its header comment: cpu_millicores, memory_mib.
EXPECTED_USAGE = {
    "idle-hog": (0, 1),
    "overprovisioned-web": (167, 33),
    "underprovisioned-cruncher": (200, 17),
    "api-gateway": (60, 41),
    "event-consumer": (66, 49),
    "session-cache": (20, 97),
    "search-indexer": (75, 81),
    "notification-worker": (50, 33),
    "metrics-forwarder": (50, 41),
    "rightsized-worker": (100, 48),
}


def _cpu_cores(value: str) -> float:
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def _mem_bytes(value: str) -> float:
    for suffix, scale in (("Gi", 2**30), ("Mi", MIB), ("Ki", 2**10)):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * scale
    return float(value)


def _parse_manifest(path: Path) -> dict:
    """Pull name, replicas and the container's *requests* out of a Deployment.

    A hand-rolled indentation-aware scan rather than PyYAML: the project is
    stdlib-only, and these manifests are a known, flat shape. Scoped to the
    `requests:` block so the `limits:` values (which are deliberately
    different) cannot be read by mistake.
    """
    name = replicas = cpu = mem = None
    in_requests = False
    requests_indent = 0

    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if in_requests and indent <= requests_indent:
            in_requests = False
        if re.match(r"\s*requests:\s*$", line):
            in_requests, requests_indent = True, indent
            continue

        if in_requests:
            if match := re.match(r"\s*cpu:\s*\"?([\w.]+)\"?\s*$", line):
                cpu = _cpu_cores(match.group(1))
            elif match := re.match(r"\s*memory:\s*\"?([\w.]+)\"?\s*$", line):
                mem = _mem_bytes(match.group(1))
        elif name is None and (match := re.match(r"\s*name:\s*([\w-]+)\s*$", line)):
            name = match.group(1)
        elif match := re.match(r"\s*replicas:\s*(\d+)\s*$", line):
            replicas = int(match.group(1))

    return {"name": name, "replicas": replicas, "cpu": cpu, "mem": mem}


def _deployments() -> list[dict]:
    parsed = [
        _parse_manifest(p)
        for p in sorted(WORKLOAD_DIR.glob("*.yaml"))
        if "kind: Deployment" in p.read_text()
    ]
    for d in parsed:
        assert all(v is not None for v in d.values()), f"incomplete manifest: {d}"
    return parsed


def _fleet_metrics() -> list[WorkloadMetrics]:
    """Manifest requests x replicas, paired with the engineered usage."""
    metrics = []
    for d in _deployments():
        cpu_used_m, mem_used_mib = EXPECTED_USAGE[d["name"]]
        metrics.append(
            WorkloadMetrics(
                namespace="cost-demo",
                workload=d["name"],
                # Summed across pods, matching what the pipeline aggregates.
                cpu_request_cores=d["cpu"] * d["replicas"],
                cpu_usage_cores=(cpu_used_m / 1000) * d["replicas"],
                mem_request_bytes=d["mem"] * d["replicas"],
                mem_usage_bytes=mem_used_mib * MIB * d["replicas"],
                pods=d["replicas"],
            )
        )
    return metrics


class FleetShapeTests(unittest.TestCase):
    def test_fleet_is_ten_deployments_and_forty_pods(self):
        deployments = _deployments()
        self.assertEqual(len(deployments), EXPECTED_DEPLOYMENTS)
        self.assertEqual(sum(d["replicas"] for d in deployments), EXPECTED_PODS)

    def test_every_deployment_has_a_documented_usage_expectation(self):
        # A new workload must come with its engineered usage, or the share below
        # would silently shift.
        self.assertEqual(
            {d["name"] for d in _deployments()},
            set(EXPECTED_USAGE),
        )


class FleetFlaggingTests(unittest.TestCase):
    def test_thirty_percent_of_workloads_are_flagged(self):
        costs = rank_by_waste(_fleet_metrics())
        flagged = [c for c in costs if c.cpu_overprovisioned or c.mem_overprovisioned]

        self.assertEqual(len(costs), EXPECTED_DEPLOYMENTS)
        self.assertEqual(len(flagged), len(EXPECTED_FLAGGED))
        self.assertAlmostEqual(len(flagged) / len(costs), 0.30)

    def test_exactly_the_intended_workloads_are_flagged_on_the_intended_axes(self):
        # The seven honestly-sized workloads are the control group: if any of
        # them appears here, the threshold or the math is wrong.
        actual = {}
        for c in rank_by_waste(_fleet_metrics()):
            axes = tuple(
                axis
                for axis, over in (("cpu", c.cpu_overprovisioned), ("mem", c.mem_overprovisioned))
                if over
            )
            if axes:
                actual[c.workload] = axes

        self.assertEqual(actual, EXPECTED_FLAGGED)

    def test_worst_offender_ranks_first_by_waste(self):
        costs = rank_by_waste(_fleet_metrics())
        self.assertEqual(costs[0].workload, "idle-hog")
        # Every honestly-sized workload wastes nothing at all.
        self.assertEqual(
            [c.monthly_waste_usd for c in costs if c.workload not in EXPECTED_FLAGGED],
            [0.0] * (EXPECTED_DEPLOYMENTS - len(EXPECTED_FLAGGED)),
        )

    def test_control_group_clears_the_threshold_on_both_axes(self):
        for c in rank_by_waste(_fleet_metrics()):
            if c.workload in EXPECTED_FLAGGED:
                continue
            with self.subTest(workload=c.workload):
                self.assertGreaterEqual(c.cpu_efficiency, EFFICIENCY_THRESHOLD)
                self.assertGreaterEqual(c.mem_efficiency, EFFICIENCY_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
