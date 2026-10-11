"""Integrated strategy contract, observable pressure and isolation from other orders."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from ..core import Scheduler, SchedulerRequest
from ..core.endpoint_state import EndpointView
from ..policies import PercentileReference
from ..policies.classification import OutputPercentileClassifier
from ..policies.light_heavy_ranking import LightHeavyRankingPolicy
from ..policies.ranking import RankingResult, load_batch_order
from ..simulation import SimulationRunner
from .support import config, endpoint, req


def request(name, output=8, **options):
    return SchedulerRequest(name, "deepseek-flash", 1, output, **options)


class LightHeavyRankingTests(unittest.TestCase):
    def policy(self, mode="percentile_dynamic"):
        settings = config(output_classification=mode, input_threshold_tokens=100)
        return LightHeavyRankingPolicy(settings, classifier=OutputPercentileClassifier(
            settings, PercentileReference.from_lengths(range(1, 11))))

    def context(self, occupied=0, ready=0, limit=10, **options):
        return dict(endpoints=(EndpointView("a", 1000, 1000000, limit, 0, 0, occupied),),
                    now_ms=10, ready_count=ready, batch_id=3, classification_context="window", **options)

    def test_single_rank_call_assesses_pressure_selects_threshold_and_orders(self):
        policy = self.policy()
        requests = (request("eight"), request("short", 2), request("nine", 9))
        observed = []
        for occupied, ready in ((0, 0), (6, 0), (9, 0), (10, 10)):
            result = policy.rank_requests(requests, self.context(occupied, ready))
            observed.append(result.snapshot["threshold"])
            self.assertEqual(set(result.classification), {r.request_id for r in requests})
            self.assertTrue(all(result.classification[r.request_id]["classified_at_ms"] == 10 for r in requests))
            self.assertEqual([r.request_id for r in result.requests], ["eight", "short", "nine"] if occupied == 0
                             else ["short", "eight", "nine"])
        self.assertEqual(observed, [.9, .8, .7, .6])
        self.assertEqual(result.snapshot["classification_context"], "window")

    def test_unknown_capacity_uses_normal_and_fixed_mode_does_not_adjust(self):
        result = self.policy().rank_requests((request("a"),), self.context(occupied=1, ready=100, limit=None))
        self.assertEqual((result.snapshot["pressure_level"], result.snapshot["threshold"]), ("normal", .8))
        self.assertIsNone(result.snapshot["concurrency_utilization"])
        fixed = self.policy("percentile_fixed")
        for occupied, ready in ((0, 0), (10, 100)):
            self.assertEqual(fixed.rank_requests((request("a"),), self.context(occupied, ready)).snapshot["threshold"], .8)

    def test_results_are_frozen_against_later_batches_and_caller_mutation(self):
        policy = self.policy()
        first = policy.rank_requests((request("a"),), self.context())
        policy.rank_requests((request("b"),), self.context(10, 100))
        self.assertFalse(first.classification["a"]["heavy"])
        self.assertEqual(first.snapshot["threshold"], .9)
        first.snapshot["threshold"] = .1
        self.assertEqual(policy.artifacts()["trace"][0]["threshold"], .9)

    def test_token_mode_needs_no_reference_and_uses_estimates(self):
        settings = config()
        with patch.object(OutputPercentileClassifier, "load_default", side_effect=AssertionError("unneeded reference")):
            policy = load_batch_order("light_heavy", config=settings)
        rows = (request("heavy", 20), request("light", 5), request("shorter", 1),
                replace(request("input_heavy", 1), input_tokens=20))
        result = policy.rank_requests(rows, self.context())
        self.assertEqual([r.request_id for r in result.requests], ["light", "shorter", "heavy", "input_heavy"])
        self.assertTrue(result.classification["input_heavy"]["input_heavy"])
        self.assertIsNone(policy.artifacts())

    def test_fifo_and_weighted_do_not_load_or_run_dynamic_classification(self):
        for name in ("fifo", "weighted_length"):
            settings = config(batch_order=name, output_classification="percentile_dynamic",
                output_reference_path="does-not-exist.parquet", output_reference_metadata_path="does-not-exist.json",
                endpoints=(endpoint(concurrency=1),))
            with self.subTest(name=name), patch.object(OutputPercentileClassifier, "load_default",
                    side_effect=AssertionError("unneeded classifier")):
                result = SimulationRunner(settings).run((req(0), req(1), req(2)))
            self.assertEqual(result.summary["unclassified_requests"], 3)
            self.assertEqual(result.summary["light_requests"], 0)
            self.assertIsNone(result.classification_artifacts)
            self.assertFalse(any(e["event"] == "classification_updated" for e in result.events))

    def test_immediate_requests_are_assessed_without_waiting_or_sorting(self):
        settings = config(endpoints=(endpoint(concurrency=4),), busy_concurrency_reserve=3,
                          batch_size=100, batch_wait_ms=20)
        scheduler = Scheduler(tuple(e.to_core() for e in settings.endpoints), settings.scheduler_config())
        scheduler.submit_request(request("first"))
        low = scheduler.submit_request(request("low"))
        high = scheduler.submit_request(request("high", 20, priority=1))
        self.assertEqual(low.scheduling_path, "busy_window")
        self.assertIsNone(low.classification["heavy"])
        self.assertEqual(high.scheduling_path, "priority_immediate")
        self.assertTrue(high.classification["heavy"])
        self.assertEqual(high.decision.dispatched_at_ms, 0)
        scheduler.flush()
        self.assertFalse(low.classification["heavy"])
        self.assertEqual(scheduler.batches[0]["dispatch_order"], ["low"])

    def test_invalid_strategy_diagnostics_fail_before_window_dispatch(self):
        class InvalidPolicy:
            def rank_requests(self, requests, context):
                return RankingResult(tuple(requests), {"foreign": {"heavy": True}})
        settings = config(endpoints=(endpoint(concurrency=1),), batch_size=1)
        scheduler = Scheduler(tuple(e.to_core() for e in settings.endpoints), settings.scheduler_config(),
                              ranking_policy=InvalidPolicy())
        scheduler.submit_request(request("first"))
        with self.assertRaisesRegex(ValueError, "classify every request"):
            scheduler.submit_request(request("second"))
        self.assertIsNone(scheduler.records["second"].decision)
        self.assertEqual(scheduler.states.states["a"].total_requests, 1)
