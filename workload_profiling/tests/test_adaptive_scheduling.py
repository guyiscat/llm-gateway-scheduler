"""Flowchart branches, load boundaries, and capacity recovery without starvation by queue heads."""
from dataclasses import replace
import json
import unittest

from ..simulation import (SimulationRunner, EndpointView, LoadMonitor,
                          WorkloadRequest, PriorityThenLightOrderStrategy, load_config)
from ..simulation.classification import OutputPercentileClassifier
from ..simulation.priority import assign_priority
from ..simulation.source import read_length_requests, read_prompt_requests
from ..simulation.cli import execute
from ..policies import PercentileReference
from ..common.paths import PACKAGE
from .support import config, endpoint, temporary_directory
from .support import FakeTokenizer


def settings(**overrides):
    return config(**(dict(busy_concurrency_reserve=0,
                          endpoints=(endpoint(concurrency=2, latency=99),),
                          batch_order="priority_then_light") | overrides))


def request(name, level=1, tokens=2):
    return WorkloadRequest(str(name), 1, tokens-1, priority_level=level, priority_level_source="recorded")


class LoadMonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = LoadMonitor(settings(busy_concurrency_reserve=3,
                                            endpoints=(endpoint(concurrency=10),)))

    def view(self, name="a", rpm=0, tpm=0, concurrency=0):
        return EndpointView(name, 100, 1000, 10, rpm, tpm, concurrency)

    def test_each_dimension_uses_inclusive_threshold(self):
        self.assertFalse(self.monitor.endpoint_load(self.view(rpm=94, tpm=949, concurrency=6)).busy)
        for view, reason in ((self.view(rpm=95), "rpm"), (self.view(tpm=950), "tpm"),
                             (self.view(concurrency=7), "concurrency")):
            with self.subTest(reason=reason):
                self.assertEqual(self.monitor.endpoint_load(view).reasons, (reason,))
        self.assertEqual(self.monitor.endpoint_load(self.view(rpm=95, tpm=950, concurrency=7)).reasons,
                         ("rpm", "tpm", "concurrency"))

    def test_system_requires_every_endpoint_busy_even_for_different_reasons(self):
        state = self.monitor.snapshot((self.view("a", rpm=95), self.view("b", tpm=950)))
        self.assertTrue(state.busy)
        state = self.monitor.snapshot((self.view("a", rpm=95), self.view("b")))
        self.assertFalse(state.busy)
        self.assertEqual(state.non_busy_endpoint_ids, ("b",))
        with self.assertRaises(ValueError):
            self.monitor.snapshot(())

    def test_thresholds_are_configurable(self):
        monitor = LoadMonitor(settings(busy_rpm_threshold=.5, busy_tpm_threshold=.4,
                                       busy_concurrency_reserve=0))
        self.assertTrue(monitor.endpoint_load(self.view(rpm=50)).busy)
        self.assertTrue(monitor.endpoint_load(self.view(tpm=400)).busy)
        self.assertFalse(monitor.endpoint_load(self.view(concurrency=9)).busy)
        self.assertTrue(monitor.endpoint_load(self.view(concurrency=10)).busy)

    def test_invalid_settings_fail_before_replay(self):
        for changes in ({"busy_rpm_threshold": 0}, {"busy_rpm_threshold": 95},
                        {"busy_tpm_threshold": float("nan")}, {"busy_tpm_threshold": True},
                        {"busy_concurrency_reserve": -1}, {"busy_concurrency_reserve": True},
                        {"busy_concurrency_reserve": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings(**changes)


class FourLevelPriorityTests(unittest.TestCase):
    def test_assignment_is_reproducible_independent_of_lengths_and_order(self):
        cfg = settings(priority_assignment="four_level")
        source = [WorkloadRequest(str(index), 1, 1) for index in range(100)]
        labels = {r.request_id: assign_priority(r, cfg).priority_level for r in source}
        self.assertEqual(set(labels.values()), {1, 2, 3, 4})
        self.assertEqual(labels, {r.request_id: assign_priority(replace(r, output_tokens=1000), cfg).priority_level
                                  for r in reversed(source)})
        changed = {r.request_id: assign_priority(r, replace(cfg, priority_seed=1)).priority_level for r in source}
        self.assertNotEqual(labels, changed)
        for level in range(1, 5):
            explicit = request("explicit", level)
            self.assertEqual(assign_priority(explicit, cfg), explicit)

    def test_sources_preserve_explicit_numeric_levels(self):
        with temporary_directory() as directory:
            path = directory / "lengths.jsonl"
            path.write_text(json.dumps({"input_tokens": 1, "output_tokens": 1, "priority_level": 4}), encoding="utf-8")
            recorded = next(read_length_requests(path))
            self.assertEqual((recorded.priority_level, recorded.priority_level_source), (4, "recorded"))
            path.write_text(json.dumps({"prompt": {"messages": [{"role": "user", "content": "hi"}]},
                                        "response": "answer", "priority_level": 1}), encoding="utf-8")
            recorded = next(read_prompt_requests(path, FakeTokenizer()))
            self.assertEqual((recorded.priority_level, recorded.priority_level_source), (1, "recorded"))

    def test_invalid_levels_are_rejected(self):
        for level in (0, 5, -1, True, 1.0, "4", None):
            with self.subTest(level=level), self.assertRaises(ValueError):
                WorkloadRequest("a", 1, 1, priority_level=level)

    def test_window_order_uses_level_then_light_and_stable_arrival_ties(self):
        cfg = settings()
        requests = (request("three_heavy", 3, 20), request("two_light", 2),
                    request("three_light", 3), request("three_light_later", 3), request("one", 1))
        self.assertEqual(PriorityThenLightOrderStrategy(cfg).order_batch(requests, (), 0),
                         ["three_light", "three_light_later", "three_heavy", "two_light", "one"])


class AdaptiveSchedulingTests(unittest.TestCase):
    def test_idle_light_request_executes_immediately_without_a_batch(self):
        result = SimulationRunner(settings(batch_size=100, batch_wait_ms=500)).run([request("light")])
        row = result.requests[0]
        self.assertEqual(row["dispatch_at_ms"], 0)
        self.assertFalse(row["heavy"])
        self.assertIsNone(row["batch_id"])
        self.assertEqual(row["batch_wait_ms"], 0)
        self.assertEqual(row["scheduling_path"], "idle_immediate")
        self.assertEqual(result.batches, [])
        self.assertEqual(result.summary["endpoint_executed_requests"], 1)

    def test_non_busy_scope_rechecked_after_each_same_time_arrival(self):
        cfg = settings(endpoints=(endpoint("a", concurrency=4, latency=99), endpoint("b", concurrency=4, latency=99)),
                       busy_concurrency_reserve=3, arrival_mode="burst", burst_size=4, burst_span_ms=0)
        result = SimulationRunner(cfg).run([request(i) for i in range(4)])
        self.assertEqual([r["endpoint_id"] for r in result.requests[:2]], ["a", "b"])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests[:2]], [0, 0])
        self.assertEqual(result.requests[1]["routing_candidate_ids"], ["b"])
        self.assertFalse(result.requests[1]["system_busy_at_arrival"])
        self.assertEqual(result.requests[2]["scheduling_path"], "busy_window")

    def test_priority_four_bypasses_busy_window(self):
        cfg = settings(endpoints=(endpoint(concurrency=5, latency=99),), busy_concurrency_reserve=3,
                       batch_size=100, batch_wait_ms=20)
        result = SimulationRunner(cfg).run([request("first"), request("second"), request("window"), request("urgent", 4, 30)])
        window, urgent = result.requests[2:]
        self.assertTrue(urgent["system_busy_at_arrival"])
        self.assertEqual(urgent["scheduling_path"], "priority_immediate")
        self.assertEqual(urgent["dispatch_at_ms"], 3)
        self.assertIsNone(urgent["batch_id"])
        self.assertEqual(urgent["batch_wait_ms"], 0)
        self.assertGreater(window["dispatch_at_ms"], urgent["dispatch_at_ms"])

    def test_priority_four_can_route_to_busy_endpoint_when_system_is_idle(self):
        class FirstEndpoint:
            def select(self, request, endpoints, now_ms):
                return endpoints[0].endpoint_id
        cfg = settings(endpoints=(endpoint("a", concurrency=4, latency=99), endpoint("b", concurrency=4, latency=99)),
                       busy_concurrency_reserve=3)
        result = SimulationRunner(cfg, strategy=FirstEndpoint()).run([request(0), request(1, 4)])
        urgent = result.requests[1]
        self.assertFalse(urgent["system_busy_at_arrival"])
        self.assertEqual(urgent["routing_candidate_ids"], ["a", "b"])
        self.assertEqual(urgent["endpoint_id"], "a")


    def test_strategy_cannot_select_busy_endpoint_for_normal_immediate_request(self):
        class AlwaysA:
            def select(self, request, endpoints, now_ms):
                return "a"
        cfg = settings(endpoints=(endpoint("a", concurrency=4, latency=99), endpoint("b", concurrency=4, latency=99)),
                       busy_concurrency_reserve=3)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            SimulationRunner(cfg, strategy=AlwaysA()).run([request(0), request(1)])

    def test_waiting_priority_four_gets_freed_slot_before_window_head(self):
        cfg = settings(endpoints=(endpoint(concurrency=1, latency=99),), batch_size=1)
        result = SimulationRunner(cfg).run([request(0), request(1), request(2, 4)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 200, 100])
        self.assertEqual(result.requests[2]["batch_wait_ms"], 0)
        self.assertEqual(result.requests[2]["capacity_wait_ms"], 98)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)

    def test_priority_arriving_at_completion_precedes_old_window_queue(self):
        cfg = settings(endpoints=(endpoint(concurrency=1, latency=99),), batch_size=1, arrival_interval_ms=50)
        result = SimulationRunner(cfg).run([request(0), request(1), request(2, 4)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 200, 100])

    def test_blocked_urgent_head_does_not_block_feasible_urgent_request(self):
        cfg = settings(endpoints=(endpoint(concurrency=3, latency=99, tpm=100),))
        result = SimulationRunner(cfg).run([request(0, 4, 60), request(1, 4, 60), request(2, 4, 20)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 60000, 2])

    def test_idle_request_waits_for_non_busy_scope_and_window_expiry_wakes_it(self):
        cfg = settings(endpoints=(endpoint("a", concurrency=10, tpm=10, latency=99),
                                  endpoint("b", concurrency=10, rpm=1, tpm=100, latency=99)))
        class LastEndpoint:
            def select(self, request, endpoints, now_ms):
                return endpoints[-1].endpoint_id
        result = SimulationRunner(cfg, strategy=LastEndpoint()).run([request(0, 4), request(1, 1, 20)])
        row = result.requests[1]
        self.assertEqual(row["scheduling_path"], "idle_immediate")
        self.assertEqual(row["endpoint_id"], "b")
        self.assertEqual(row["dispatch_at_ms"], 60000)
        self.assertIsNone(row["batch_id"])

    def test_rpm_and_tpm_busy_windows_recover_without_new_arrivals(self):
        for ep, tokens in ((endpoint(concurrency=10, rpm=1, latency=99), 2),
                           (endpoint(concurrency=10, tpm=15, latency=99), 11)):
            with self.subTest(ep=ep):
                cfg = settings(endpoints=(ep,), batch_size=1, busy_tpm_threshold=.5)
                result = SimulationRunner(cfg).run([request(0, tokens=tokens), request(1, tokens=tokens)])
                self.assertEqual(result.requests[1]["scheduling_path"], "busy_window")
                self.assertEqual(result.requests[1]["dispatch_at_ms"], 60000)

    def test_busy_window_retains_deadline_when_system_recovers(self):
        cfg = settings(endpoints=(endpoint(concurrency=2, latency=3),), busy_concurrency_reserve=1,
                       arrival_interval_ms=3, batch_wait_ms=5, batch_size=100)
        result = SimulationRunner(cfg).run([request(i) for i in range(4)])
        self.assertEqual(result.requests[1]["batch_released_at_ms"], 8)
        self.assertEqual(result.requests[2]["scheduling_path"], "idle_immediate")
        self.assertEqual(result.requests[2]["dispatch_at_ms"], 6)

    def test_busy_window_uses_numeric_priority_order(self):
        cfg = settings(endpoints=(endpoint(concurrency=1, latency=99),), batch_size=3)
        result = SimulationRunner(cfg).run([request(0, 2), request(1, 1), request(2, 3), request(3, 2)])
        self.assertEqual(result.batches[0]["dispatch_order"], ["2", "3", "1"])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 300, 100, 200])

    def test_percentile_classification_is_frozen_at_admission_or_release(self):
        cfg = settings(endpoints=(endpoint(concurrency=1, latency=99),), output_classification="percentile_dynamic",
                       batch_size=1)
        policy = OutputPercentileClassifier(cfg, PercentileReference.from_lengths([1, 2, 3, 4]))
        result = SimulationRunner(cfg, classification_policy=policy).run([request(0), request(1), request(2, 4)])
        self.assertEqual([r["classified_at_ms"] for r in result.requests], [0, 1, 2])
        self.assertEqual([s["classification_context"] for s in policy.trace], ["immediate", "window", "immediate"])
        self.assertEqual([s["batch_id"] for s in policy.trace], [None, 0, None])
        self.assertTrue(all(isinstance(r["heavy"], bool) for r in result.requests))
        for row in result.requests:
            self.assertEqual(row["queue_wait_ms"], row["batch_wait_ms"] + row["capacity_wait_ms"])

    def test_oversized_immediate_requests_rejected_without_blocking_window(self):
        result = SimulationRunner(settings(endpoints=(endpoint(concurrency=1, latency=99, tpm=10),))).run(
            [request(0, 4, 11), request(1)])
        self.assertEqual([r["status"] for r in result.requests], ["rejected", "completed"])
        self.assertEqual(result.requests[1]["dispatch_at_ms"], 1)

    def test_empty_input_and_reproducible_generated_priorities(self):
        cfg = settings(priority_assignment="four_level")
        self.assertEqual(SimulationRunner(cfg).run([]).summary["total_requests"], 0)
        source = [WorkloadRequest(str(index), 1, 1) for index in range(30)]
        self.assertEqual(SimulationRunner(cfg).run(source), SimulationRunner(cfg).run(source))

    def test_adaptive_route_outputs_are_reproducible(self):
        cfg = load_config(PACKAGE / "config/simulation_adaptive.json")
        with temporary_directory() as directory:
            source = directory / "lengths.jsonl"
            source.write_text("\n".join(json.dumps({"request_id": str(i), "input_tokens": 1,
                               "output_tokens": 2, "priority_level": i % 4 + 1}) for i in range(8)), encoding="utf-8")
            first = execute(cfg, source=source, source_format="lengths", output=directory / "first.jsonl")
            second = execute(cfg, source=source, source_format="lengths", output=directory / "second.jsonl")
            self.assertEqual(first.requests, second.requests)
            self.assertEqual(first.events, second.events)
            self.assertEqual(first.summary["priority_levels"]["4"]["requests"], 2)
            self.assertEqual((directory / "first.jsonl").read_bytes(), (directory / "second.jsonl").read_bytes())
            routes = [json.loads(line) for line in (directory / "first.jsonl").read_text(encoding="utf-8").splitlines()]
            dispatched = [event for event in first.events if event["event"] == "dispatched"]
            self.assertEqual([row["request_id"] for row in routes], [event["request_id"] for event in dispatched])


if __name__ == "__main__":
    unittest.main()
