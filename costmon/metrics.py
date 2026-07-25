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
    base_url: str, namespace: str, usage_window: str = "5m"
) -> list[WorkloadMetrics]:
    """Snapshot of requests vs. actual usage, aggregated per Deployment.

    `usage_window` is the rate() window for CPU -- short-lived demo clusters
    should use a short window (see README caveat on p95 not being meaningful
    without organic usage cycles).
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
            f'sum by (pod) (rate(container_cpu_usage_seconds_total'
            f'{{namespace="{namespace}", container!=""}}[{usage_window}]))',
        ),
        pod_to_deployment,
    )
    mem_usage = _sum_by_deployment(
        instant_query(
            base_url,
            f'sum by (pod) (container_memory_working_set_bytes'
            f'{{namespace="{namespace}", container!=""}})',
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


if __name__ == "__main__":
    rows = pull_workload_metrics("http://localhost:9090", "cost-demo")
    print(f"{'workload':<26}{'cpu req':>10}{'cpu used':>10}{'mem req (Mi)':>15}{'mem used (Mi)':>15}")
    for r in rows:
        print(
            f"{r.workload:<26}{r.cpu_request_cores:>10.3f}{r.cpu_usage_cores:>10.3f}"
            f"{r.mem_request_bytes / 2**20:>15.1f}{r.mem_usage_bytes / 2**20:>15.1f}"
        )
