"""Pull per-workload requests and actual usage out of Prometheus.

The join from pod -> Deployment is done here in Python rather than as a
single nested PromQL query: kube-state-metrics only maps pod -> ReplicaSet
(kube_pod_owner) and ReplicaSet -> Deployment (kube_replicaset_owner)
separately, so attributing usage to a Deployment is a two-hop join either
way. Doing it in Python keeps each PromQL query simple and keeps the join
logic in one place that's easy to unit test later (see README for the
equivalent single PromQL query, validated against the live cluster).
"""
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


def _pod_to_deployment(base_url: str, namespace: str) -> dict[str, str]:
    """pod name -> owning Deployment name, via ReplicaSet as the middle hop."""
    pod_to_rs = {
        m["metric"]["pod"]: m["metric"]["owner_name"]
        for m in instant_query(
            base_url,
            f'kube_pod_owner{{namespace="{namespace}", owner_kind="ReplicaSet"}}',
        )
    }
    rs_to_deploy = {
        m["metric"]["replicaset"]: m["metric"]["owner_name"]
        for m in instant_query(
            base_url,
            f'kube_replicaset_owner{{namespace="{namespace}", owner_kind="Deployment"}}',
        )
    }
    return {
        pod: rs_to_deploy[rs]
        for pod, rs in pod_to_rs.items()
        if rs in rs_to_deploy
    }


def _sum_by_deployment(rows: list[dict], pod_to_deployment: dict[str, str]) -> dict[str, float]:
    """Sum a per-pod instant-query result into per-Deployment totals."""
    totals: dict[str, float] = {}
    for row in rows:
        pod = row["metric"].get("pod")
        deployment = pod_to_deployment.get(pod)
        if deployment is None:
            continue  # pod not owned by a Deployment we know about (or already gone)
        totals[deployment] = totals.get(deployment, 0.0) + float(row["value"][1])
    return totals


def pull_workload_metrics(
    base_url: str, namespace: str, usage_window: str = "15m"
) -> list[WorkloadMetrics]:
    """Requests vs. actual usage over `usage_window`, aggregated per Deployment.

    Usage is a *peak-aware* statistic, not an average, because the numbers
    feed request recommendations: p95 of the CPU rate, and max working set
    for memory. An average would recommend a request that the workload
    exceeds half the time -- throttling on CPU, OOMKill on memory.

    Both statistics are computed per container and then summed, matching how
    requests are summed: a request is set per container, so that's the unit a
    recommendation applies to.

    See the README caveat on why a percentile is near-meaningless on this
    particular cluster (synthetic, flat load) while still being the right math.
    """
    pod_to_deployment = _pod_to_deployment(base_url, namespace)

    cpu_request = _sum_by_deployment(
        instant_query(
            base_url,
            f'kube_pod_container_resource_requests{{namespace="{namespace}", resource="cpu"}}',
        ),
        pod_to_deployment,
    )
    mem_request = _sum_by_deployment(
        instant_query(
            base_url,
            f'kube_pod_container_resource_requests{{namespace="{namespace}", resource="memory"}}',
        ),
        pod_to_deployment,
    )
    cpu_usage = _sum_by_deployment(
        instant_query(
            base_url,
            f'sum by (pod) (quantile_over_time({CPU_QUANTILE}, '
            f'rate(container_cpu_usage_seconds_total'
            f'{{namespace="{namespace}", container!=""}}[{CPU_RATE_WINDOW}])'
            f'[{usage_window}:{USAGE_STEP}]))',
        ),
        pod_to_deployment,
    )
    mem_usage = _sum_by_deployment(
        instant_query(
            base_url,
            f'sum by (pod) (max_over_time(container_memory_working_set_bytes'
            f'{{namespace="{namespace}", container!=""}}[{usage_window}]))',
        ),
        pod_to_deployment,
    )

    deployments = sorted(set(pod_to_deployment.values()))
    return [
        WorkloadMetrics(
            namespace=namespace,
            workload=d,
            cpu_request_cores=cpu_request.get(d, 0.0),
            cpu_usage_cores=cpu_usage.get(d, 0.0),
            mem_request_bytes=mem_request.get(d, 0.0),
            mem_usage_bytes=mem_usage.get(d, 0.0),
        )
        for d in deployments
    ]
