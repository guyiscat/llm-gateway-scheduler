from dataclasses import replace
import math
import unittest

from ..simulation import SimulationRunner, EndpointConfig, WorkloadRequest, load_config
from ..simulation.models import EndpointState


class ServiceJitterTests(unittest.TestCase):
    def test_zero_jitter_preserves_original_timing(self):
        state = EndpointState(EndpointConfig("a", 600, 5000000, 8))
        for i, o in ((0, 0), (101, 999), (377804, 282)):
            request = WorkloadRequest(str(i), i, o)
            self.assertEqual(state.service_time_ms(request), max(1, math.ceil(5 + i / 2000 + o / 20)))

    def test_bounds_and_order_independent_repeatability(self):
        settings = EndpointConfig("a", 600, 5000000, 8, service_jitter_fraction=.2)
        requests = [WorkloadRequest(str(i), 10, 1000) for i in range(100)]
        first = EndpointState(settings)
        draws = {r.request_id: first.service_multiplier(r) for r in requests}
        repeated = EndpointState(settings)
        self.assertEqual(draws, {r.request_id: repeated.service_multiplier(r) for r in reversed(requests)})
        self.assertTrue(all(.8 <= value <= 1.2 for value in draws.values()))
        self.assertGreater(len(set(draws.values())), 90)
        alternate = EndpointState(replace(settings, service_jitter_seed=1))
        self.assertNotEqual(draws, {r.request_id: alternate.service_multiplier(r) for r in requests})

    def test_invalid_settings(self):
        for fraction in (-.1, 1, float("nan"), True):
            with self.assertRaises(ValueError):
                EndpointConfig("a", 600, 5000000, 8, service_jitter_fraction=fraction)
        for seed in (-1, 1.2, True):
            with self.assertRaises(ValueError):
                EndpointConfig("a", 600, 5000000, 8, service_jitter_seed=seed)

    def test_replay_repeats_and_preserves_capacity_accounting(self):
        settings = load_config()
        settings = replace(settings, batch_size=1, input_threshold_tokens=0, busy_concurrency_reserve=0,
                           arrival_mode="fixed", priority_assignment="uniform",
                           endpoints=(replace(settings.endpoints[0], concurrency_limit=1,
                                              service_jitter_fraction=.2),))
        requests = [WorkloadRequest(str(i), 10, 1000) for i in range(8)]
        first = SimulationRunner(settings).run(requests)
        second = SimulationRunner(settings).run(requests)
        self.assertEqual(first.requests, second.requests)
        self.assertEqual(first.events, second.events)
        self.assertEqual(first.summary["completed_requests"], 8)
        self.assertEqual(first.endpoints[0]["peak_concurrency"], 1)
        self.assertGreater(first.requests[-1]["capacity_wait_ms"], 0)
