"""Tests for the pod -> workload join (costmon.metrics), against a fake Prometheus.

The demo fleet is all Deployments, so the live cluster can't show that other
owner kinds are attributed, or that non-running pods are left out. These
fabricated query results cover both.
"""
import unittest
from unittest import mock

from costmon.metrics import pull_workload_metrics

MIB = 2**20

# pod -> (owner_kind, owner_name, phase), as kube_pod_owner / kube_pod_status_phase report it.
PODS = {
    "web-7d9f-a": ("ReplicaSet", "web-7d9f", "Running"),
    "web-7d9f-b": ("ReplicaSet", "web-7d9f", "Running"),
    "db-0": ("StatefulSet", "db", "Running"),
    "shipper-x1": ("DaemonSet", "shipper", "Running"),
    "backup-29001-q": ("Job", "backup-29001", "Running"),
    "migrate-z": ("Job", "migrate", "Running"),
    # No owner. Current kube-state-metrics leaves the owner labels off
    # entirely; older versions set them to "<none>". Both must work.
    "debug": (None, None, "Running"),
    "legacy-debug": ("<none>", "<none>", "Running"),
    # Not running: requests are still reported for these, usage is zero.
    "web-7d9f-pending": ("ReplicaSet", "web-7d9f", "Pending"),
    "db-1": ("StatefulSet", "db", "Failed"),
}
REPLICASET_OWNERS = {"web-7d9f": "web"}
JOB_OWNERS = {"backup-29001": "backup"}  # "migrate" is a standalone Job


def _row(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


def fake_instant_query(base_url, expr):
    if expr.startswith("kube_pod_status_phase"):
        return [_row(1, pod=p) for p, (_, _, phase) in PODS.items() if phase == "Running"]
    if expr.startswith("kube_replicaset_owner"):
        return [_row(1, replicaset=rs, owner_name=d) for rs, d in REPLICASET_OWNERS.items()]
    if expr.startswith("kube_job_owner"):
        return [_row(1, job_name=j, owner_name=c) for j, c in JOB_OWNERS.items()]
    if expr.startswith("kube_pod_owner"):
        return [
            _row(1, pod=p) if k is None else _row(1, pod=p, owner_kind=k, owner_name=n)
            for p, (k, n, _) in PODS.items()
        ]
    # Every pod requests 100m / 64Mi and uses 50m / 32Mi, including the
    # non-running ones, so any leak of those into the totals shows up.
    if 'resource="cpu"' in expr:
        return [_row(0.1, pod=p) for p in PODS]
    if 'resource="memory"' in expr:
        return [_row(64 * MIB, pod=p) for p in PODS]
    if "quantile_over_time" in expr:
        return [_row(0.05, pod=p) for p in PODS]
    if "max_over_time" in expr:
        return [_row(32 * MIB, pod=p, container="app") for p in PODS] + [
            # web-7d9f-a's container restarted inside the window (or was
            # reported by two kubelets): a second series for the same container.
            _row(40 * MIB, pod="web-7d9f-a", container="app", node="other"),
            # web-7d9f-b also runs a sidecar: a different container, so it adds.
            _row(8 * MIB, pod="web-7d9f-b", container="sidecar"),
        ]
    raise AssertionError(f"unexpected query: {expr}")


def _pull():
    with mock.patch("costmon.metrics.instant_query", side_effect=fake_instant_query):
        return {(m.kind, m.workload): m for m in pull_workload_metrics("http://prom", "ns")}


class WorkloadAttributionTests(unittest.TestCase):
    def test_every_owner_kind_is_attributed(self):
        self.assertEqual(
            set(_pull()),
            {
                ("Deployment", "web"),  # via its ReplicaSet
                ("StatefulSet", "db"),
                ("DaemonSet", "shipper"),
                ("CronJob", "backup"),  # via its Job
                ("Job", "migrate"),  # no CronJob owner: stays a Job
                ("Pod", "debug"),  # no owner: the pod is its own workload
                ("Pod", "legacy-debug"),
            },
        )

    def test_values_are_summed_per_workload(self):
        web = _pull()[("Deployment", "web")]
        self.assertEqual(web.pods, 2)
        self.assertAlmostEqual(web.cpu_request_cores, 0.2)
        self.assertAlmostEqual(web.cpu_usage_cores, 0.1)
        self.assertAlmostEqual(web.mem_request_bytes, 128 * MIB)


class DuplicateSeriesTests(unittest.TestCase):
    def test_duplicate_series_of_one_container_count_once_at_their_max(self):
        # a: max(32, 40) = 40Mi, b: app 32Mi + sidecar 8Mi = 40Mi.
        # Summing every series instead would give 112Mi.
        web = _pull()[("Deployment", "web")]
        self.assertAlmostEqual(web.mem_usage_bytes, 80 * MIB)


class RunningPodsOnlyTests(unittest.TestCase):
    def test_non_running_pods_add_no_requests(self):
        # A Pending or Failed pod reserves nothing and uses nothing; counting
        # its requests would show up as waste.
        workloads = _pull()
        self.assertEqual(workloads[("Deployment", "web")].pods, 2)  # pending pod excluded
        self.assertEqual(workloads[("StatefulSet", "db")].pods, 1)  # failed pod excluded
        self.assertAlmostEqual(workloads[("StatefulSet", "db")].cpu_request_cores, 0.1)


if __name__ == "__main__":
    unittest.main()
