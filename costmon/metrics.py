"""Pull per-workload requests and actual usage out of Prometheus.

The join from pod -> workload is done here in Python rather than as a
single nested PromQL query: kube-state-metrics only maps one ownership hop
per metric -- pod -> ReplicaSet/StatefulSet/DaemonSet/Job (kube_pod_owner),
ReplicaSet -> Deployment (kube_replicaset_owner), Job -> CronJob
(kube_job_owner) -- so attributing usage to a workload is a multi-hop join
either way. Doing it in Python keeps each PromQL query simple and keeps the
join logic in one place that's easy to unit test.

The Deployment half of the join as a single query, checked against the demo
cluster (plain rate() rather than the peak-aware statistics used below; it
validates the join only). label_replace is needed because on(...) matches
label names and both owner metrics call their target `owner_name`:

    sum by (deployment) (
      sum by (pod) (
        rate(container_cpu_usage_seconds_total{namespace="cost-demo",container!=""}[5m])
      )
      * on (pod) group_left(replicaset)
        label_replace(
          kube_pod_owner{namespace="cost-demo", owner_kind="ReplicaSet"},
          "replicaset", "$1", "owner_name", "(.*)"
        )
      * on (replicaset) group_left(deployment)
        label_replace(
          kube_replicaset_owner{namespace="cost-demo", owner_kind="Deployment"},
          "deployment", "$1", "owner_name", "(.*)"
        )
    )
"""
from collections import Counter
from dataclasses import dataclass

from costmon.prometheus import instant_query

# Inner window for rate(). Prometheus needs several samples to compute a rate
# accurately; at the 30s scrapeInterval this project pins, 2m gives 4.
CPU_RATE_WINDOW = "2m"

# Step between samples of the outer (percentile) window. One point per minute,
# so a 15m window yields 15 points to take a percentile over.
USAGE_STEP = "1m"

# CPU requests are sized off a high percentile rather than the max: CPU is
# compressible, so a brief spike costs latency, not an OOMKill, and sizing
# every workload for its worst second wastes most of the cluster.
CPU_QUANTILE = 0.95


@dataclass
class WorkloadMetrics:
    namespace: str
    workload: str
    cpu_request_cores: float
    cpu_usage_cores: float
    mem_request_bytes: float
    mem_usage_bytes: float
    # Values above are summed across this many pods; per-pod minimums scale by it.
    pods: int = 1
    # Owner kind as Kubernetes names it: Deployment, StatefulSet, DaemonSet,
    # CronJob, Job, or Pod for a pod with no owner.
    kind: str = "Deployment"


def _pod_to_workload(base_url: str, namespace: str) -> dict[str, tuple[str, str]]:
    """Running pod name -> (kind, name) of the workload that owns it.

    ReplicaSets resolve to their Deployment and Jobs to their CronJob when
    they have one; anything else is attributed to its direct owner. Pods that
    aren't Running are left out: kube-state-metrics still reports requests
    for Pending, Failed and Evicted pods, which use nothing, so they would
    otherwise count as pure waste.
    """
    running = {
        m["metric"]["pod"]
        for m in instant_query(
            base_url,
            f'kube_pod_status_phase{{namespace="{namespace}", phase="Running"}} == 1',
        )
    }
    rs_to_deploy = {
        m["metric"]["replicaset"]: m["metric"]["owner_name"]
        for m in instant_query(
            base_url,
            f'kube_replicaset_owner{{namespace="{namespace}", owner_kind="Deployment"}}',
        )
    }
    job_to_cronjob = {
        m["metric"]["job_name"]: m["metric"]["owner_name"]
        for m in instant_query(
            base_url,
            f'kube_job_owner{{namespace="{namespace}", owner_kind="CronJob"}}',
        )
    }

    owners: dict[str, tuple[str, str]] = {}
    for m in instant_query(base_url, f'kube_pod_owner{{namespace="{namespace}"}}'):
        # A pod with no owner has no owner labels at all on current
        # kube-state-metrics, and "<none>" on older versions.
        pod, kind, name = m["metric"]["pod"], m["metric"].get("owner_kind"), m["metric"].get("owner_name")
        if pod not in running:
            continue
        if kind == "ReplicaSet" and name in rs_to_deploy:
            kind, name = "Deployment", rs_to_deploy[name]
        elif kind == "Job" and name in job_to_cronjob:
            kind, name = "CronJob", job_to_cronjob[name]
        elif kind in (None, "<none>"):
            kind, name = "Pod", pod
        owners[pod] = (kind, name)
    return owners


def _sum_by_workload(
    rows: list[dict], pod_to_workload: dict[str, tuple[str, str]]
) -> dict[tuple[str, str], float]:
    """Sum a per-container instant-query result into per-workload totals.

    Series are first collapsed to one per (pod, container), keeping the max.
    A container that restarted inside the window, or one reported by two
    kubelets, shows up as several series, and adding them double-counts.
    """
    per_container: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row["metric"].get("pod"), row["metric"].get("container"))
        per_container[key] = max(per_container.get(key, 0.0), float(row["value"][1]))

    totals: dict[tuple[str, str], float] = {}
    for (pod, _), value in per_container.items():
        workload = pod_to_workload.get(pod)
        if workload is None:
            continue  # pod not running (or already gone)
        totals[workload] = totals.get(workload, 0.0) + value
    return totals


def pull_workload_metrics(
    base_url: str, namespace: str, usage_window: str = "15m"
) -> list[WorkloadMetrics]:
    """Requests vs. actual usage over `usage_window`, aggregated per workload.

    Usage is a *peak-aware* statistic, not an average, because the numbers
    feed request recommendations: p95 of the CPU rate, and max working set
    for memory. An average would recommend a request that the workload
    exceeds half the time -- throttling on CPU, OOMKill on memory.

    Both statistics are computed per container and then summed, matching how
    requests are summed: a request is set per container, so that's the unit a
    recommendation applies to.

    On the demo cluster the percentile changes nothing visible: the workloads
    run a fixed duty cycle, so p95, mean and max land within a few points of
    each other (a 4m and a 15m window gave the same verdicts). It matters on
    real workloads, which have bursts and daily cycles.
    """
    pod_to_workload = _pod_to_workload(base_url, namespace)

    cpu_request = _sum_by_workload(
        instant_query(
            base_url,
            f'kube_pod_container_resource_requests{{namespace="{namespace}", resource="cpu"}}',
        ),
        pod_to_workload,
    )
    mem_request = _sum_by_workload(
        instant_query(
            base_url,
            f'kube_pod_container_resource_requests{{namespace="{namespace}", resource="memory"}}',
        ),
        pod_to_workload,
    )
    cpu_usage = _sum_by_workload(
        instant_query(
            base_url,
            f'quantile_over_time({CPU_QUANTILE}, '
            f'rate(container_cpu_usage_seconds_total'
            f'{{namespace="{namespace}", container!=""}}[{CPU_RATE_WINDOW}])'
            f'[{usage_window}:{USAGE_STEP}])',
        ),
        pod_to_workload,
    )
    mem_usage = _sum_by_workload(
        instant_query(
            base_url,
            f'max_over_time(container_memory_working_set_bytes'
            f'{{namespace="{namespace}", container!=""}}[{usage_window}])',
        ),
        pod_to_workload,
    )

    pod_counts = Counter(pod_to_workload.values())
    return [
        WorkloadMetrics(
            namespace=namespace,
            workload=name,
            cpu_request_cores=cpu_request.get((kind, name), 0.0),
            cpu_usage_cores=cpu_usage.get((kind, name), 0.0),
            mem_request_bytes=mem_request.get((kind, name), 0.0),
            mem_usage_bytes=mem_usage.get((kind, name), 0.0),
            pods=pod_counts[(kind, name)],
            kind=kind,
        )
        for kind, name in sorted(pod_counts)
    ]
