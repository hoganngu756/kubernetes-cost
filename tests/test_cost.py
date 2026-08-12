"""Unit tests for costmon.cost, against hand-calculated waste $.

Run with: python3 -m unittest discover

These exist because the live kind cluster can't validate the math on its
own -- it has no organic usage cycles, so every workload there is a flat,
engineered case. These fabricated inputs cover what the cluster can't:
exact $ figures checked by hand, and the under-provisioned / mixed cases.
"""
import unittest

from costmon.cost import evaluate
from costmon.metrics import WorkloadMetrics

MIB = 2**20
GIB = 2**30


class EvaluateTests(unittest.TestCase):
    def test_overprovisioned_both_axes_flagged_and_waste_matches_hand_calc(self):
        m = WorkloadMetrics(
            namespace="cost-demo",
            workload="overprovisioned-web",
            cpu_request_cores=0.5,
            cpu_usage_cores=0.05,  # efficiency 0.1 -> flagged
            mem_request_bytes=512 * MIB,
            mem_usage_bytes=51.2 * MIB,  # efficiency 0.1 -> flagged
        )
        c = evaluate(m)

        self.assertAlmostEqual(c.cpu_efficiency, 0.1)
        self.assertAlmostEqual(c.mem_efficiency, 0.1)
        self.assertTrue(c.cpu_overprovisioned)
        self.assertTrue(c.mem_overprovisioned)
        self.assertAlmostEqual(c.recommended_cpu_request_cores, 0.065)  # 0.05 * 1.3
        self.assertAlmostEqual(c.recommended_mem_request_bytes, 66.56 * MIB)  # 51.2 * 1.3

        # hand-calculated: monthly_cost = (0.5*0.048 + 0.5*0.012) * 730 = 21.9
        self.assertAlmostEqual(c.monthly_cost_usd, 21.9, places=2)
        # hand-calculated: cpu_waste=15.2424, mem_waste=3.8106 -> 19.053
        self.assertAlmostEqual(c.monthly_waste_usd, 19.053, places=2)

    def test_rightsized_workload_is_not_flagged_and_has_zero_waste(self):
        m = WorkloadMetrics(
            namespace="cost-demo",
            workload="rightsized-worker",
            cpu_request_cores=0.13,
            cpu_usage_cores=0.078,  # efficiency 0.6 -> not flagged
            mem_request_bytes=64 * MIB,
            mem_usage_bytes=38.4 * MIB,  # efficiency 0.6 -> not flagged
        )
        c = evaluate(m)

        self.assertFalse(c.cpu_overprovisioned)
        self.assertFalse(c.mem_overprovisioned)
        # not flagged -> recommendation leaves the request untouched
        self.assertAlmostEqual(c.recommended_cpu_request_cores, m.cpu_request_cores)
        self.assertAlmostEqual(c.recommended_mem_request_bytes, m.mem_request_bytes)
        self.assertEqual(c.monthly_waste_usd, 0.0)

    def test_mixed_workload_flags_only_the_overprovisioned_axis(self):
        # Mirrors workloads/underprovisioned-cruncher.yaml: CPU efficiency > 1
        # (needs MORE, not less) while memory is genuinely wasted. A cut on
        # CPU here would be actively harmful -- this is the case that proves
        # efficiency must be computed per-dimension, not as one blended score.
        m = WorkloadMetrics(
            namespace="cost-demo",
            workload="underprovisioned-cruncher",
            cpu_request_cores=0.05,
            cpu_usage_cores=0.2,  # efficiency 4.0 -> under-provisioned, not flagged
            mem_request_bytes=64 * MIB,
            mem_usage_bytes=16 * MIB,  # efficiency 0.25 -> flagged
        )
        c = evaluate(m)

        self.assertFalse(c.cpu_overprovisioned)
        self.assertTrue(c.mem_overprovisioned)
        # CPU recommendation must NOT be cut below the current request.
        self.assertAlmostEqual(c.recommended_cpu_request_cores, m.cpu_request_cores)
        self.assertAlmostEqual(c.recommended_mem_request_bytes, 20.8 * MIB)  # 16 * 1.3
        # hand-calculated: mem_waste only = (64-20.8)/1024 * 0.012 * 730
        self.assertAlmostEqual(c.monthly_waste_usd, 0.3695625, places=4)

    def test_zero_request_does_not_crash_and_is_not_flagged(self):
        m = WorkloadMetrics(
            namespace="cost-demo",
            workload="scaled-to-zero",
            cpu_request_cores=0.0,
            cpu_usage_cores=0.0,
            mem_request_bytes=0.0,
            mem_usage_bytes=0.0,
        )
        c = evaluate(m)

        self.assertIsNone(c.cpu_efficiency)
        self.assertIsNone(c.mem_efficiency)
        self.assertFalse(c.cpu_overprovisioned)
        self.assertFalse(c.mem_overprovisioned)
        self.assertEqual(c.monthly_waste_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
