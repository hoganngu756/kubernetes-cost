"""Efficiency and waste calculation.

Consumes WorkloadMetrics (costmon.metrics) and prices the gap between
requested and actually-used resources against the static rate table in
costmon.pricing. Requests, not limits, drive this model: the scheduler
bin-packs nodes on requests, so requests are what's effectively reserved
and billed for.
"""
from dataclasses import dataclass

from costmon.metrics import WorkloadMetrics
from costmon.pricing import CPU_HOURLY_RATE_PER_CORE, HOURS_PER_MONTH, MEM_HOURLY_RATE_PER_GIB

GIB = 2**30

# Below this fraction of requested resources actually used, a workload counts
# as overprovisioned on that dimension. Independent per CPU/memory: a workload
# can be flagged on one axis without the other (see underprovisioned-cruncher
# in workloads/, which is over on memory and under on CPU at the same time).
EFFICIENCY_THRESHOLD = 0.4

# Recommended request = usage * this headroom multiplier, when flagged.
RECOMMENDATION_HEADROOM = 1.3

# Per-pod floor on a recommendation. An idle workload's usage * headroom is
# ~0, and a 0m / 0Mi request is not a setting anyone should apply.
MIN_CPU_REQUEST_CORES = 0.010
MIN_MEM_REQUEST_BYTES = 16 * 2**20


@dataclass
class WorkloadCost:
    workload: str
    kind: str
    # Carried through from WorkloadMetrics so a recommendation or a delta bar
    # can be rendered without re-joining against the input.
    cpu_request_cores: float
    cpu_usage_cores: float
    mem_request_bytes: float
    mem_usage_bytes: float
    # The values above and the recommendations below are summed across this
    # many pods; divide by it for the per-pod request someone would set.
    pods: int
    cpu_efficiency: float | None
    mem_efficiency: float | None
    cpu_overprovisioned: bool
    mem_overprovisioned: bool
    recommended_cpu_request_cores: float
    recommended_mem_request_bytes: float
    monthly_cost_usd: float
    monthly_waste_usd: float


def _efficiency(usage: float, request: float) -> float | None:
    if request <= 0:
        return None
    return usage / request


def _recommend(usage: float, request: float, floor: float) -> float:
    # Capped at the current request: the axis was flagged for being too big,
    # so a floor above it must not turn the recommendation into an increase.
    return min(request, max(usage * RECOMMENDATION_HEADROOM, floor))


def evaluate(m: WorkloadMetrics, threshold: float = EFFICIENCY_THRESHOLD) -> WorkloadCost:
    cpu_efficiency = _efficiency(m.cpu_usage_cores, m.cpu_request_cores)
    mem_efficiency = _efficiency(m.mem_usage_bytes, m.mem_request_bytes)

    cpu_over = cpu_efficiency is not None and cpu_efficiency < threshold
    mem_over = mem_efficiency is not None and mem_efficiency < threshold

    recommended_cpu = (
        _recommend(m.cpu_usage_cores, m.cpu_request_cores, MIN_CPU_REQUEST_CORES * m.pods)
        if cpu_over
        else m.cpu_request_cores
    )
    recommended_mem = (
        _recommend(m.mem_usage_bytes, m.mem_request_bytes, MIN_MEM_REQUEST_BYTES * m.pods)
        if mem_over
        else m.mem_request_bytes
    )

    monthly_cost = (
        m.cpu_request_cores * CPU_HOURLY_RATE_PER_CORE
        + (m.mem_request_bytes / GIB) * MEM_HOURLY_RATE_PER_GIB
    ) * HOURS_PER_MONTH

    cpu_waste = (
        (m.cpu_request_cores - recommended_cpu) * CPU_HOURLY_RATE_PER_CORE * HOURS_PER_MONTH
        if cpu_over
        else 0.0
    )
    mem_waste = (
        ((m.mem_request_bytes - recommended_mem) / GIB) * MEM_HOURLY_RATE_PER_GIB * HOURS_PER_MONTH
        if mem_over
        else 0.0
    )

    return WorkloadCost(
        workload=m.workload,
        kind=m.kind,
        cpu_request_cores=m.cpu_request_cores,
        cpu_usage_cores=m.cpu_usage_cores,
        mem_request_bytes=m.mem_request_bytes,
        mem_usage_bytes=m.mem_usage_bytes,
        pods=m.pods,
        cpu_efficiency=cpu_efficiency,
        mem_efficiency=mem_efficiency,
        cpu_overprovisioned=cpu_over,
        mem_overprovisioned=mem_over,
        recommended_cpu_request_cores=recommended_cpu,
        recommended_mem_request_bytes=recommended_mem,
        monthly_cost_usd=monthly_cost,
        monthly_waste_usd=cpu_waste + mem_waste,
    )


def rank_by_waste(
    metrics: list[WorkloadMetrics], threshold: float = EFFICIENCY_THRESHOLD
) -> list[WorkloadCost]:
    return sorted(
        (evaluate(m, threshold) for m in metrics),
        key=lambda c: c.monthly_waste_usd,
        reverse=True,
    )
