"""Command-line efficiency report -- the one entry point for the pipeline.

    python3 -m costmon.cli --help

Pulls per-workload metrics, prices the request/usage gap, and prints a
waste-ranked table plus the concrete request changes that would close it.
"""
import argparse
import sys

from costmon.cost import EFFICIENCY_THRESHOLD, RECOMMENDATION_HEADROOM, WorkloadCost, rank_by_waste
from costmon.metrics import pull_workload_metrics

MIB = 2**20


def _millicores(cores: float) -> str:
    return f"{cores * 1000:.0f}m"


def _mib(byte_count: float) -> str:
    return f"{byte_count / MIB:.0f}Mi"


def _pct(ratio: float | None) -> str:
    # None means a zero request: no denominator, so no efficiency to report.
    return "n/a" if ratio is None else f"{ratio:.0%}"


def render(costs: list[WorkloadCost], threshold: float) -> str:
    lines = [f"{'workload':<26}{'cpu eff':>9}{'mem eff':>9}{'$/mo cost':>12}{'$/mo waste':>12}"]
    for c in costs:
        lines.append(
            f"{c.workload:<26}{_pct(c.cpu_efficiency):>9}{_pct(c.mem_efficiency):>9}"
            f"{c.monthly_cost_usd:>12.2f}{c.monthly_waste_usd:>12.2f}"
        )

    total_cost = sum(c.monthly_cost_usd for c in costs)
    total_waste = sum(c.monthly_waste_usd for c in costs)
    lines.append(f"{'TOTAL':<26}{'':>9}{'':>9}{total_cost:>12.2f}{total_waste:>12.2f}")

    flagged = [c for c in costs if c.cpu_overprovisioned or c.mem_overprovisioned]
    if flagged:
        lines.append("")
        lines.append(
            f"Recommended request changes "
            f"(efficiency < {threshold:.0%}, {RECOMMENDATION_HEADROOM}x headroom):"
        )
        for c in flagged:
            cpu = (
                f"cpu {_millicores(c.cpu_request_cores)} -> "
                f"{_millicores(c.recommended_cpu_request_cores)}"
                if c.cpu_overprovisioned
                else "cpu ok"
            )
            mem = (
                f"mem {_mib(c.mem_request_bytes)} -> {_mib(c.recommended_mem_request_bytes)}"
                if c.mem_overprovisioned
                else "mem ok"
            )
            lines.append(f"  {c.workload:<26}{cpu:<26}{mem}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="costmon", description=__doc__.splitlines()[0])
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--namespace", default="cost-demo")
    parser.add_argument(
        "--window",
        default="15m",
        help="analysis window for the usage statistic (default: %(default)s)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=EFFICIENCY_THRESHOLD,
        help="efficiency below which a workload is flagged (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        metrics = pull_workload_metrics(args.prometheus_url, args.namespace, args.window)
    except OSError as exc:
        # urllib.error.URLError subclasses OSError, so this covers refused
        # connections, DNS failures and timeouts alike -- overwhelmingly a
        # forgotten `make port-forward`.
        print(
            f"error: could not reach Prometheus at {args.prometheus_url}: {exc}\n"
            f"hint: is `make port-forward` running?",
            file=sys.stderr,
        )
        return 1

    if not metrics:
        print(
            f"error: no Deployments found in namespace {args.namespace!r}",
            file=sys.stderr,
        )
        return 1

    print(render(rank_by_waste(metrics, args.threshold), args.threshold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
