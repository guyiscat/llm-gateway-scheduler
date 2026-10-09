"""用小型已知分布和人工状态验证契约，不模拟真实负载或拥塞时间线。"""
from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

import numpy as np

from ..policies.congestion import CongestionState
from ..policies.output_heavy_policy import OutputHeavyPolicy, load_config
from ..policies.percentile_reference import PercentileReference


class PercentileTests(unittest.TestCase):
    def setUp(self):
        self.reference = PercentileReference.from_lengths([10, 20, 20, 40])

    def test_known_ties(self):
        self.assertEqual(self.reference.percentile(10), 0.25)
        self.assertEqual(self.reference.percentile(20), 0.75)
        np.testing.assert_array_equal(self.reference.percentiles([20, 20, 20]), [0.75] * 3)

    def test_monotonic_and_boundaries(self):
        percentiles = self.reference.percentiles([0, 10, 19, 20, 39, 40, 100])
        np.testing.assert_array_equal(percentiles, [0, 0.25, 0.25, 0.75, 0.75, 1, 1])
        self.assertTrue(np.all(np.diff(percentiles) >= 0))

    def test_all_equal_lengths(self):
        reference = PercentileReference.from_lengths([7, 7, 7])
        self.assertEqual(reference.percentile(6), 0)
        self.assertEqual(reference.percentile(7), 1)

    def test_minimum_length_at_threshold(self):
        self.assertEqual(self.reference.minimum_length_at(0.75), 20)
        self.assertEqual(self.reference.minimum_length_at(0.95), 40)
        for value in [-0.1, 0, 1, 1.2, float("nan"), True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reference.minimum_length_at(value)

    def test_reference_copies_input_and_blocks_array_writes(self):
        lengths = np.array([10, 20, 20, 40])
        reference = PercentileReference.from_lengths(lengths)
        lengths[:] = 100
        self.assertEqual(reference.percentile(20), 0.75)
        for array in [reference.values, reference.counts, reference.cumulative_counts]:
            with self.assertRaises(ValueError):
                array[0] = 0

    def test_invalid_token_counts(self):
        for length in [-1, 2.5, "20", True, np.bool_(True), 2**63]:
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.reference.percentile(length)

    def test_invalid_reference_arrays(self):
        for lengths in [[], [1.0, 2.0], [True], [-1, 1], [[1, 2]]]:
            with self.subTest(lengths=lengths), self.assertRaises(ValueError):
                PercentileReference.from_lengths(lengths)
        for values, counts in [([20, 10], [1, 1]), ([10, 10], [1, 1]), ([10], [0]), ([10], [1, 1])]:
            with self.subTest(values=values, counts=counts), self.assertRaises(ValueError):
                PercentileReference(np.array(values), np.array(counts))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        # 契约示例使用独立基准，用户修改真实运行配置后仍能运行这些测试。
        self.config = {"default_threshold": 0.95,
                       "congestion_threshold_mapping": {"idle": 0.98, "normal": 0.95, "busy": 0.90, "critical": 0.80},
                       "percentile_definition": "empirical_cdf_right", "reference_sample_count": None}
        config_patch = patch("workload_profiling.policies.output_heavy_policy.load_config",
                             side_effect=lambda *args, **kwargs: deepcopy(self.config))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.reference = PercentileReference.from_lengths([10, 20, 20, 40])
        self.policy = OutputHeavyPolicy(self.reference)
        self.policy.update_from_congestion(CongestionState.NORMAL)

    def test_default_without_provider(self):
        policy = OutputHeavyPolicy(self.reference)
        self.assertEqual(policy.get_threshold(), 0.95)
        decision = policy.evaluate(40)
        self.assertEqual(decision["threshold_source"], "DEFAULT")
        self.assertIsNone(decision["congestion_state"])

    def test_normal_to_busy_changes_classification(self):
        # 0.92 是契约测试输入，不声称是某个真实请求的测量值。
        normal = self.policy.classify(0.92)
        self.assertEqual(normal["output_heavy_threshold"], 0.95)
        self.assertFalse(normal["output_heavy"])
        self.policy.update_from_congestion(CongestionState.BUSY)
        busy = self.policy.classify(0.92)
        self.assertEqual(busy["output_heavy_threshold"], 0.90)
        self.assertTrue(busy["output_heavy"])
        self.assertEqual(busy["threshold_source"], "CONGESTION")

    def test_all_configured_states(self):
        expected = {CongestionState.IDLE: 0.98, CongestionState.NORMAL: 0.95,
                    CongestionState.BUSY: 0.90, CongestionState.CRITICAL: 0.80}
        for state, threshold in expected.items():
            with self.subTest(state=state):
                self.policy.update_from_congestion(state)
                self.assertEqual(self.policy.get_threshold(), threshold)

    def test_manual_wins_every_state_until_clear(self):
        self.policy.set_threshold(0.85)
        for state in CongestionState:
            with self.subTest(state=state):
                self.policy.update_from_congestion(state)
                decision = self.policy.classify(0.86)
                self.assertEqual(decision["output_heavy_threshold"], 0.85)
                self.assertEqual(decision["threshold_source"], "MANUAL")
                self.assertEqual(decision["congestion_state"], state.value)
                self.assertTrue(decision["output_heavy"])
        self.policy.clear_manual_override()
        self.assertEqual(self.policy.get_threshold(), 0.80)
        self.assertEqual(self.policy.threshold_source, "CONGESTION")

    def test_clear_without_state_restores_default(self):
        policy = OutputHeavyPolicy()
        policy.set_threshold(0.85)
        policy.clear_manual_override()
        self.assertEqual(policy.get_threshold(), 0.95)
        self.assertEqual(policy.threshold_source, "DEFAULT")

    def test_pushed_state_preserved_under_override(self):
        policy = OutputHeavyPolicy()
        policy.set_threshold(0.85)
        policy.update_from_congestion(CongestionState.BUSY)
        self.assertEqual(policy.get_threshold(), 0.85)
        policy.clear_manual_override()
        self.assertEqual(policy.get_threshold(), 0.90)
        self.assertEqual(policy.threshold_source, "CONGESTION")

    def test_invalid_threshold_does_not_change_manual_value(self):
        self.policy.set_threshold(0.85)
        for value in [-0.1, 0, 1, 1.2, float("nan"), float("inf"), True, "0.9"]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "0 < threshold < 1"):
                self.policy.set_threshold(value)
            self.assertEqual(self.policy.get_threshold(), 0.85)

    def test_inclusive_threshold(self):
        self.assertTrue(self.policy.classify(0.95)["output_heavy"])
        self.assertFalse(self.policy.classify(0.949)["output_heavy"])

    def test_invalid_percentiles(self):
        for value in [-0.01, 1.01, float("nan"), float("inf"), True, "0.92"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.policy.classify(value)

    def test_full_runtime_decision(self):
        decision = self.policy.evaluate(np.int64(20))
        self.assertEqual(decision, {"output_tokens": 20, "output_percentile": 0.75,
                                   "output_heavy": False, "output_heavy_threshold": 0.95,
                                   "congestion_state": "normal", "threshold_source": "CONGESTION"})

    def test_threshold_changes_do_not_change_percentile(self):
        before = self.policy.evaluate(20)
        self.policy.update_from_congestion(CongestionState.CRITICAL)
        self.policy.set_threshold(0.70)
        after = self.policy.evaluate(20)
        self.assertEqual(before["output_percentile"], after["output_percentile"])
        self.assertEqual(self.reference.sample_count, 4)



    def test_evaluate_requires_reference(self):
        with self.assertRaisesRegex(ValueError, "requires a fixed"):
            OutputHeavyPolicy().evaluate(20)

    def test_central_config_changes_policy(self):
        config = load_config()
        config["congestion_threshold_mapping"]["busy"] = 0.88
        with patch("workload_profiling.policies.output_heavy_policy.load_config", return_value=config):
            policy = OutputHeavyPolicy(self.reference)
        policy.update_from_congestion(CongestionState.BUSY)
        self.assertEqual(policy.get_threshold(), 0.88)

    def test_reference_count_mismatch_rejected(self):
        config = load_config()
        config["reference_sample_count"] = 99
        with patch("workload_profiling.policies.output_heavy_policy.load_config", return_value=config):
            with self.assertRaisesRegex(ValueError, "sample counts"):
                OutputHeavyPolicy(self.reference)


if __name__ == "__main__":
    unittest.main()
