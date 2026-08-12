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


@dataclass
class WorkloadCost:
    workload: str
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


def evaluate(m: WorkloadMetrics) -> WorkloadCost:
    cpu_efficiency = _efficiency(m.cpu_usage_cores, m.cpu_request_cores)
    mem_efficiency = _efficiency(m.mem_usage_bytes, m.mem_request_bytes)

    cpu_over = cpu_efficiency is not None and cpu_efficiency < EFFICIENCY_THRESHOLD
    mem_over = mem_efficiency is not None and mem_efficiency < EFFICIENCY_THRESHOLD

    recommended_cpu = (
        m.cpu_usage_cores * RECOMMENDATION_HEADROOM if cpu_over else m.cpu_request_cores
    )
    recommended_mem = (
        m.mem_usage_bytes * RECOMMENDATION_HEADROOM if mem_over else m.mem_request_bytes
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
        cpu_efficiency=cpu_efficiency,
        mem_efficiency=mem_efficiency,
        cpu_overprovisioned=cpu_over,
        mem_overprovisioned=mem_over,
        recommended_cpu_request_cores=recommended_cpu,
        recommended_mem_request_bytes=recommended_mem,
        monthly_cost_usd=monthly_cost,
        monthly_waste_usd=cpu_waste + mem_waste,
    )


def rank_by_waste(metrics: list[WorkloadMetrics]) -> list[WorkloadCost]:
    return sorted((evaluate(m) for m in metrics), key=lambda c: c.monthly_waste_usd, reverse=True)


if __name__ == "__main__":
    from costmon.metrics import pull_workload_metrics

    metrics = pull_workload_metrics("http://localhost:9090", "cost-demo")
    ranked = rank_by_waste(metrics)

    print(f"{'workload':<26}{'cpu eff':>9}{'mem eff':>9}{'$/mo cost':>12}{'$/mo waste':>12}")
    for c in ranked:
        cpu_eff = f"{c.cpu_efficiency:.0%}" if c.cpu_efficiency is not None else "n/a"
        mem_eff = f"{c.mem_efficiency:.0%}" if c.mem_efficiency is not None else "n/a"
        print(
            f"{c.workload:<26}{cpu_eff:>9}{mem_eff:>9}"
            f"{c.monthly_cost_usd:>12.2f}{c.monthly_waste_usd:>12.2f}"
        )
