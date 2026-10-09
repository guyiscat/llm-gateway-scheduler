"""Ordering extension contracts and released-window FIFO behavior."""
from dataclasses import FrozenInstanceError
import unittest

from ..simulation import SimulationRunner, load_batch_order
from ..simulation.ordering import validate_batch_order
from .support import config, endpoint, req


class ReverseOrder:
    def order_batch(self, requests, endpoints, now_ms):
        return [r.request_id for r in reversed(requests)]


class AsyncOrder:
    async def order_batch(self, requests, endpoints, now_ms):
        return []


class OrderingTests(unittest.TestCase):
    def test_loader_and_invalid_plugin_contracts(self):
        self.assertEqual(load_batch_order("fifo").order_batch((req(0), req(1)), (), 0), ["0", "1"])
        spec = "workload_profiling.tests.test_ordering:ReverseOrder"
        self.assertEqual(load_batch_order(spec).order_batch((req(0), req(1)), (), 0), ["1", "0"])
        with self.assertRaises(TypeError):
            load_batch_order("workload_profiling.tests.test_ordering:AsyncOrder")
        for spec in ("shortest_first", "effective_priority", "missing:Order"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                load_batch_order(spec)

    def test_order_must_be_complete_unique_sequence(self):
        requests = (req(0), req(1))
        for plan in ([], ["0"], ["0", "0"], ["0", "foreign"], [0, 1], "01", iter(["0", "1"])):
            with self.subTest(plan=type(plan).__name__), self.assertRaises((ValueError, TypeError)):
                validate_batch_order(requests, plan)

    def test_invalid_plan_is_rejected_before_window_dispatch(self):
        class InvalidOrder:
            def order_batch(self, requests, endpoints, now_ms):
                return ["foreign"]
        with self.assertRaises(ValueError):
            SimulationRunner(config(endpoints=(endpoint(concurrency=1, latency=99),)),
                             batch_order=InvalidOrder()).run([req(0), req(1), req(2)])

    def test_batches_retain_fifo_and_order_sees_immutable_busy_endpoints(self):
        snapshots = []
        class ObserveOrder(ReverseOrder):
            def order_batch(self, requests, endpoints, now_ms):
                snapshots.append((requests, endpoints))
                with self_test.assertRaises(FrozenInstanceError):
                    requests[0].priority_level = 4
                with self_test.assertRaises(FrozenInstanceError):
                    endpoints[0].concurrency = 0
                return super().order_batch(requests, endpoints, now_ms)
        self_test = self
        result = SimulationRunner(config(endpoints=(endpoint(concurrency=1, latency=99),)),
                                  batch_order=ObserveOrder()).run([req(i) for i in range(5)])
        self.assertEqual([b["dispatch_order"] for b in result.batches], [["2", "1"], ["4", "3"]])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 200, 100, 400, 300])
        self.assertTrue(all(endpoints[0].concurrency == 1 for _, endpoints in snapshots))

    def test_async_and_missing_strategies_fail_at_construction(self):
        with self.assertRaises(TypeError):
            SimulationRunner(config(), batch_order=AsyncOrder())
        with self.assertRaises(TypeError):
            SimulationRunner(config(), strategy=object())
        runner = SimulationRunner(config())
        runner.run([])
        with self.assertRaises(RuntimeError):
            runner.run([])
