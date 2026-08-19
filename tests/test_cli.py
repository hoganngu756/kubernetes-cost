"""Tests for report rendering (costmon.cli).

The table is the deliverable, so the two things worth pinning down are the
ones a reader would trust without re-deriving: that TOTAL actually sums the
rows, and that only the flagged axis produces a recommendation.
"""
import unittest

from costmon.cli import BAR_WIDTH, _bar, render
from costmon.cost import rank_by_waste
from costmon.metrics import WorkloadMetrics

MIB = 2**20


def _report(metrics, threshold=0.4):
    return render(rank_by_waste(metrics, threshold), threshold)


def _recommendations(report):
    """Just the lines below the recommendations header -- the table rows above
    also start with the workload name and would otherwise match."""
    _, _, tail = report.partition("Recommended request changes")
    return tail


class RenderTests(unittest.TestCase):
    def test_total_row_sums_cost_and_waste(self):
        metrics = [
            # cost = (0.5*0.048 + 0.5*0.012) * 730 = 21.90, waste = 19.053
            WorkloadMetrics("cost-demo", "web", 0.5, 0.05, 512 * MIB, 51.2 * MIB),
            # cost = (0.13*0.048 + 0.0625*0.012) * 730 = 5.10, waste = 0
            WorkloadMetrics("cost-demo", "worker", 0.13, 0.078, 64 * MIB, 38.4 * MIB),
        ]
        total = next(l for l in _report(metrics).splitlines() if l.startswith("TOTAL"))

        self.assertIn("27.00", total)  # 21.90 + 5.10
        self.assertIn("19.05", total)  # 19.053 + 0

    def test_only_the_flagged_axis_gets_a_recommendation(self):
        # Mirrors underprovisioned-cruncher: needs more CPU, wastes memory.
        metrics = [WorkloadMetrics("cost-demo", "cruncher", 0.05, 0.2, 64 * MIB, 16 * MIB)]
        rec = _recommendations(_report(metrics))

        self.assertIn("cpu ok", rec)  # never recommend cutting an under-provisioned axis
        self.assertIn("mem 64Mi -> 21Mi", rec)  # 16 * 1.3 = 20.8

    def test_flagged_share_is_reported(self):
        # The headline number: how much of the fleet is over-provisioned.
        metrics = [
            WorkloadMetrics("cost-demo", "waste", 0.5, 0.05, 512 * MIB, 51.2 * MIB),
            WorkloadMetrics("cost-demo", "ok1", 0.13, 0.078, 64 * MIB, 38.4 * MIB),
            WorkloadMetrics("cost-demo", "ok2", 0.13, 0.078, 64 * MIB, 38.4 * MIB),
            WorkloadMetrics("cost-demo", "ok3", 0.13, 0.078, 64 * MIB, 38.4 * MIB),
        ]
        self.assertIn("Over-provisioned: 1 of 4 workloads (25%)", _report(metrics))

    def test_rightsized_only_report_has_no_recommendations_section(self):
        metrics = [WorkloadMetrics("cost-demo", "worker", 0.13, 0.078, 64 * MIB, 38.4 * MIB)]

        self.assertNotIn("Recommended request changes", _report(metrics))


class BarTests(unittest.TestCase):
    """The bar is the visualization -- fill has to mean what the legend says."""

    def test_bar_length_tracks_request_and_fill_tracks_usage(self):
        # Largest request in the fleet fills the full width.
        self.assertEqual(len(_bar(request=1.0, usage=1.0, scale=1.0)), BAR_WIDTH)
        # Half the fleet's largest request -> half the width.
        self.assertEqual(len(_bar(request=0.5, usage=0.5, scale=1.0)), BAR_WIDTH // 2)
        # 25% efficient at full width: a quarter used, three quarters idle.
        bar = _bar(request=1.0, usage=0.25, scale=1.0)
        self.assertEqual(bar.count("\u2588"), round(BAR_WIDTH * 0.25))
        self.assertEqual(bar.count("\u2591"), BAR_WIDTH - round(BAR_WIDTH * 0.25))

    def test_idle_workload_bar_is_all_headroom(self):
        bar = _bar(request=1.0, usage=0.0, scale=1.0)
        self.assertEqual(bar, "\u2591" * BAR_WIDTH)

    def test_underprovisioned_bar_overflows_past_the_request(self):
        # usage 2x the request -> full request bar, then overflow cells.
        bar = _bar(request=0.25, usage=0.5, scale=1.0)
        self.assertEqual(bar.count("\u2588"), round(BAR_WIDTH * 0.25))
        self.assertEqual(bar.count("\u2593"), round(BAR_WIDTH * 0.5) - round(BAR_WIDTH * 0.25))
        self.assertNotIn("\u2591", bar)  # nothing idle when you are over the request

    def test_zero_scale_does_not_divide_by_zero(self):
        self.assertEqual(_bar(request=0.0, usage=0.0, scale=0.0), "")


if __name__ == "__main__":
    unittest.main()
