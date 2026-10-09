"""Core contracts, three paths, shared feedback, and simulation isolation."""
from dataclasses import asdict, replace
import json
import subprocess
import sys
import unittest

from ..common.paths import PACKAGE, ROOT
from ..core import (Scheduler, SchedulerRequest, SchedulerConfig, EndpointConfig,
                    ExecutionResult, EndpointFilter, EndpointStateManager, RouteDecision,
                    load_endpoint_configs, load_scheduler_config)
from ..core.batch_window import BatchWindow
from ..core.busy_detector import LoadMonitor
from ..core.endpoint_history import EndpointHistory
from ..policies.ranking import WeightedLengthOrderStrategy
from ..policies.routing import load_strategy
from ..simulation import load_config, SimulationConfig, SimulationRunner, WorkloadRequest
from ..simulation.data_processing import RequestParameterGenerator
from ..simulation.request_sender import SimulatedRequestSender
from .support import config, temporary_directory


def ep(name="a", **changes):
    return EndpointConfig(**(dict(endpoint_id=name, rpm_limit=100, tpm_limit=10000,
                                 concurrency_limit=4) | changes))


def request(name, **changes):
    return SchedulerRequest(**(dict(request_id=name, target_model="default", input_tokens=1,
                                    predicted_output_tokens=1) | changes))


def feedback(decision, finished=10, **changes):
    return ExecutionResult(**(dict(request_id=decision.request.request_id,
        endpoint_id=decision.selected_endpoint_id, success=True,
        started_at_ms=decision.dispatched_at_ms, finished_at_ms=finished) | changes))


class CoreSchedulerTests(unittest.TestCase):
    def test_core_import_does_not_load_simulation_tokenizer_or_percentile_dependencies(self):
        code = "from workload_profiling.core import Scheduler; import sys; assert not any(n.startswith(('workload_profiling.simulation', 'transformers', 'pandas', 'numpy')) for n in sys.modules)"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ingress_preserves_upstream_parameters_and_owns_copies(self):
        payload = asdict(request("r", predicted_output_tokens=15, messages=({"role": "user", "content": "hi"},),
            metadata={"tag": [1]}, slo={"ttft_ms": 100}))
        scheduler = Scheduler((ep(),))
        record = scheduler.submit_request(payload)
        payload["messages"][0]["content"] = "changed"
        payload["metadata"]["tag"].append(2)
        self.assertEqual(record.request.messages[0]["content"], "hi")
        self.assertEqual(record.request.metadata, {"tag": [1]})
        self.assertEqual(record.request.predicted_output_tokens, 15)
        self.assertEqual(record.request.slo.ttft_ms, 100)

    def test_invalid_core_priority_and_lengths_rejected(self):
        for fields in ({"priority": 4}, {"priority": True}, {"input_tokens": -1},
                       {"predicted_output_tokens": 1.5}, {"arrival_time": -1}, {"stream": 1},
                       {"slo": {"tpot_ms": float("nan")}}):
            with self.subTest(fields=fields), self.assertRaises((ValueError, TypeError)):
                request("r", **fields)

    def test_invalid_required_thresholds_and_clock_window_rejected(self):
        for fields in ({"busy_rpm_threshold": None}, {"busy_tpm_threshold": None},
                       {"window_ms": 60000.0}, {"busy_concurrency_threshold": 0},
                       {"input_weight": 0, "output_weight": 0}):
            with self.subTest(fields=fields), self.assertRaises((ValueError, TypeError)):
                SchedulerConfig(**fields)

    def test_duplicate_ingress_does_not_advance_clock(self):
        scheduler = Scheduler((ep(),))
        scheduler.submit_request(request("r"))
        before = scheduler.states.export(0)
        with self.assertRaises(ValueError):
            scheduler.submit_request(request("r", arrival_time=70000))
        self.assertEqual(scheduler.now_ms, 0)
        self.assertEqual(scheduler.states.export(0), before)

    def test_three_paths_share_router_with_prices_history_and_slo(self):
        calls = []
        class Policy:
            def select_endpoint(self, req, endpoints, context):
                calls.append((req, tuple(e.endpoint_id for e in endpoints), context))
                return endpoints[0].endpoint_id
        scheduler = Scheduler((ep(input_price_per_million=2), ep("b")),
            SchedulerConfig(max_batch_size=1), routing_policy=Policy())
        scheduler.submit_request(request("high", priority=1, slo={"e2e_ms": 200}))
        normal = scheduler.submit_request(request("normal"))
        batch = scheduler.submit_request(request("batch"))
        scheduler.tick(0)
        self.assertEqual(normal.scheduling_path, "idle_immediate")
        self.assertEqual(batch.scheduling_path, "busy_window")
        self.assertEqual([ids for _, ids, _ in calls], [("a", "b"), ("b",), ("a", "b")])
        self.assertEqual(calls[0][0].slo.e2e_ms, 200)
        self.assertEqual(calls[0][2].endpoint_configs["a"].input_price_per_million, 2)
        scheduler.accept_feedback(feedback(scheduler.records["high"].decision), drain=False)
        scheduler.submit_request(request("later", priority=1, arrival_time=10))
        self.assertEqual(calls[-1][2].performance["a"].successes, 1)

    def test_busy_uses_target_model_pool_even_when_hard_capacity_is_empty(self):
        scheduler = Scheduler((ep(), ep("other", supported_models=("other-model",))))
        for i in range(4):
            scheduler.submit_request(request(str(i), priority=1))
        pool = scheduler._pool(request("ordinary"))
        self.assertEqual([e.endpoint_id for e in pool.eligible], ["a"])
        self.assertFalse(pool.feasible)
        record = scheduler.submit_request(request("ordinary"))
        self.assertTrue(record.system_busy_at_arrival)
        self.assertEqual(record.scheduling_path, "busy_window")

    def test_high_priority_waits_for_capacity_then_precedes_window(self):
        scheduler = Scheduler((ep(),), SchedulerConfig(max_batch_size=1))
        for i in range(4):
            scheduler.submit_request(request(str(i), priority=1))
        ordinary = scheduler.submit_request(request("normal"))
        scheduler.tick(0)
        high = scheduler.submit_request(request("high", priority=1))
        self.assertIsNone(high.decision)
        self.assertIsNone(ordinary.decision)
        scheduler.accept_feedback(feedback(scheduler.records["0"].decision, 10))
        self.assertEqual(high.decision.dispatched_at_ms, 10)
        self.assertIsNone(ordinary.decision)
        scheduler.accept_feedback(feedback(scheduler.records["1"].decision, 11))
        self.assertEqual(ordinary.decision.dispatched_at_ms, 11)
        self.assertEqual(scheduler.states.states["a"].peak_concurrency, 4)

    def test_hard_filters_and_rule_extension_apply_to_high_priority(self):
        settings = SchedulerConfig(busy_concurrency_reserve=0)
        scheduler = Scheduler((ep(context_limit=20), ep("wrong", supported_models=("other",))), settings)
        record = scheduler.submit_request(request("bad-context", priority=1, max_tokens=30))
        self.assertEqual(record.rejection_reason, "no_compatible_endpoint")
        scheduler.states.update_health("a", False, 0)
        waiting = scheduler.submit_request(request("waiting", priority=1))
        self.assertIsNone(waiting.decision)
        scheduler.states.update_health("a", True, 0, cooldown_until_ms=50)
        scheduler.tick(49)
        self.assertIsNone(waiting.decision)
        scheduler.tick(50)
        self.assertEqual(waiting.decision.selected_endpoint_id, "a")
        filt = EndpointFilter()
        filt.register("blocked", lambda r, c, s, t: "experiment" if c.endpoint_id == "a" else None)
        snapshots = scheduler.states.snapshots(50)
        self.assertFalse(filt.filter(request("x"), scheduler.states.configs, snapshots, 50).compatible)
        filt.remove("blocked")
        self.assertEqual(len(filt.filter(request("x"), scheduler.states.configs, snapshots, 50).compatible), 1)
        self.assertFalse(filt.filter(request("x", api_type="embedding"), scheduler.states.configs, snapshots, 50).compatible)

    def test_hard_rpm_and_tpm_cannot_be_bypassed(self):
        for endpoint in (ep(rpm_limit=1), ep(tpm_limit=2)):
            with self.subTest(endpoint=endpoint):
                scheduler = Scheduler((endpoint,))
                first = scheduler.submit_request(request("first", priority=1))
                second = scheduler.submit_request(request("second", priority=1))
                self.assertIsNone(second.decision)
                scheduler.accept_feedback(feedback(first.decision), drain=False)
                scheduler.tick(59999)
                self.assertIsNone(second.decision)
                scheduler.tick(60000)
                self.assertEqual(second.decision.dispatched_at_ms, 60000)

    def test_busy_thresholds_are_or_and_configurable(self):
        manager = EndpointStateManager((ep(),), SchedulerConfig())
        view = manager.snapshots(0)[0]
        monitor = LoadMonitor(SchedulerConfig(busy_concurrency_threshold=.5))
        self.assertTrue(monitor.endpoint_load(replace(view, requests_in_window=95)).busy)
        self.assertTrue(monitor.endpoint_load(replace(view, tokens_in_window=9500)).busy)
        self.assertFalse(monitor.endpoint_load(replace(view, concurrency=1)).busy)
        self.assertTrue(monitor.endpoint_load(replace(view, concurrency=2)).busy)

    def test_window_deadline_not_reset_by_later_arrival(self):
        window = BatchWindow(4, 10)
        window.add(request("a", arrival_time=5), 5)
        window.add(request("b", arrival_time=12), 12)
        self.assertEqual(window.deadline, 15)
        batch = window.release("timeout", 15)
        self.assertEqual([r.request_id for r in batch.requests], ["a", "b"])
        window.add(request("c", arrival_time=20), 20)
        self.assertEqual(window.deadline, 30)

    def test_failure_cooldown_does_not_blame_invalid_requests(self):
        for kind in ("endpoint", "rate_limit", "request", "cancelled", None):
            with self.subTest(kind=kind):
                scheduler = Scheduler((ep(),), SchedulerConfig(cooldown_ms=50))
                first = scheduler.submit_request(request("first", priority=1))
                scheduler.accept_feedback(feedback(first.decision, success=False, failure_kind=kind), drain=False)
                second = scheduler.submit_request(request("second", priority=1, arrival_time=10))
                if kind in ("endpoint", "rate_limit"):
                    self.assertIsNone(second.decision)
                    self.assertEqual(scheduler.next_wakeup_ms, 60)
                    scheduler.tick(60)
                self.assertIsNotNone(second.decision)
                stats = scheduler.states.history.snapshot(("a",), scheduler.now_ms)["a"]
                self.assertEqual(stats.failures, 1)

    def test_feedback_reconciles_estimates_and_keeps_lifetime_totals_after_expiry(self):
        scheduler = Scheduler((ep(),))
        a = scheduler.submit_request(request("a", predicted_output_tokens=10, priority=1))
        b = scheduler.submit_request(request("b", predicted_output_tokens=10, priority=1))
        scheduler.accept_feedback(feedback(a.decision, actual_input_tokens=2, actual_output_tokens=3), drain=False)
        self.assertEqual(scheduler.states.states["a"].tokens_in_window, 16)
        scheduler.tick(60000)
        scheduler.accept_feedback(feedback(b.decision, 60001, actual_output_tokens=20), drain=False)
        state = scheduler.states.states["a"]
        self.assertEqual(state.tokens_in_window, 0)
        self.assertEqual(state.total_tokens, 26)
        self.assertEqual(state.concurrency, 0)

    def test_late_feedback_never_rewinds_clock_and_duplicate_is_atomic(self):
        scheduler = Scheduler((ep(),))
        record = scheduler.submit_request(request("a", priority=1))
        scheduler.tick(100)
        scheduler.accept_feedback(feedback(record.decision, 20, actual_output_tokens=5), drain=False)
        self.assertEqual(scheduler.now_ms, 100)
        before = scheduler.states.export(100)
        with self.assertRaises(ValueError):
            scheduler.accept_feedback(feedback(record.decision, 200), drain=False)
        self.assertEqual(scheduler.states.export(100), before)
        self.assertEqual(scheduler.now_ms, 100)

    def test_mismatched_feedback_does_not_release_reservation(self):
        scheduler = Scheduler((ep(),))
        record = scheduler.submit_request(request("a", priority=1))
        for changes in ({"endpoint_id": "wrong"}, {"started_at_ms": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scheduler.accept_feedback(feedback(record.decision, **changes))
        self.assertEqual(scheduler.states.states["a"].concurrency, 1)
        self.assertEqual(scheduler.now_ms, 0)

    def test_recent_history_handles_bounds_expiry_unknown_metrics_and_late_delivery(self):
        history = EndpointHistory(100, 2)
        for name, finished in (("a", 10), ("b", 30), ("c", 20)):
            history.add(ExecutionResult(name, "ep", True, 0, finished, ttft_ms=5, e2e_ms=finished+10))
        result = history.snapshot(("ep",), 30)["ep"]
        self.assertEqual((result.samples, result.mean_e2e_ms, result.mean_ttft_ms), (2, 35, 5))
        self.assertIsNone(result.mean_tpot_ms)
        history.add(ExecutionResult("fail", "ep", False, 0, 40))
        result = history.snapshot(("ep",), 40)["ep"]
        self.assertEqual((result.successes, result.failures, result.mean_e2e_ms), (1, 1, 40))
        self.assertEqual(history.snapshot(("ep",), 140)["ep"].samples, 0)
        history.add(ExecutionResult("late", "ep", True, 0, 20), now_ms=140)
        self.assertEqual(history.snapshot(("ep",), 140)["ep"].samples, 0)

    def test_e2e_history_includes_queue_wait(self):
        scheduler = Scheduler((ep(concurrency_limit=1),), SchedulerConfig(busy_concurrency_reserve=0))
        first = scheduler.submit_request(request("first", priority=1))
        second = scheduler.submit_request(request("second", priority=1))
        scheduler.accept_feedback(feedback(first.decision, 10))
        scheduler.accept_feedback(feedback(second.decision, 15))
        self.assertEqual(second.result.e2e_ms, 15)
        self.assertEqual(scheduler.states.history.snapshot(("a",), 15)["a"].mean_e2e_ms, 12.5)

    def test_injected_state_configuration_must_be_consistent(self):
        settings = SchedulerConfig(cooldown_ms=77)
        states = EndpointStateManager((ep(),), settings)
        scheduler = Scheduler((ep(),), state_manager=states)
        self.assertEqual(scheduler.config.cooldown_ms, 77)
        with self.assertRaises(ValueError):
            Scheduler((ep(),), SchedulerConfig(), state_manager=states)

    def test_dynamic_state_restore_validates_all_rows_before_mutating(self):
        with temporary_directory() as directory:
            path = directory / "state.json"
            states = EndpointStateManager((ep(),), SchedulerConfig())
            path.write_text(json.dumps({"endpoints": [{"endpoint_id": "a", "healthy": False},
                {"endpoint_id": "unknown"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                states.load_health_state(path)
            self.assertTrue(states.states["a"].healthy)
            path.write_text(json.dumps({"endpoints": [{"endpoint_id": "a", "healthy": False,
                "cooldown_until_ms": 50}]}), encoding="utf-8")
            states.load_health_state(path)
            self.assertFalse(states.states["a"].healthy)
            self.assertEqual(states.states["a"].cooldown_until_ms, 50)


class RefactoredSimulationTests(unittest.TestCase):
    def test_split_config_preserves_defaults_and_portable_flat_exports(self):
        loaded = load_config()
        self.assertEqual((loaded.batch_size, loaded.batch_wait_ms, loaded.strategy, loaded.batch_order),
                         (16, 20, "min_rpm", "priority_then_light"))
        self.assertEqual(loaded.priority_assignment, "four_level")
        self.assertEqual(loaded.endpoints[0].concurrency_limit, 8)
        self.assertEqual(loaded.endpoints[0].input_tokens_per_ms, 2000)
        self.assertEqual(load_endpoint_configs(PACKAGE / "config/endpoint_config.json")[0], loaded.endpoints[0].to_core())
        self.assertEqual(load_scheduler_config(PACKAGE / "config/scheduler_config.json"), loaded.scheduler_config())
        with temporary_directory() as directory:
            path = directory / "flat.json"
            path.write_text(json.dumps(loaded.to_dict()), encoding="utf-8")
            self.assertEqual(load_config(path), loaded)

    def test_random_sender_and_binary_priority_are_reproducible(self):
        settings = config(arrival_mode="random", random_min_interval_ms=2, random_max_interval_ms=7,
                          priority_assignment="binary", high_priority_ratio=.1)
        observations = [WorkloadRequest(str(i), 1, 200) for i in range(1000)]
        sender = SimulatedRequestSender(settings)
        times = [t for t, _ in sender.schedule(observations)]
        self.assertEqual(times, [t for t, _ in sender.schedule(observations)])
        self.assertEqual(times[0], 0)
        self.assertTrue(all(2 <= b-a <= 7 for a, b in zip(times, times[1:])))
        generate = RequestParameterGenerator(settings)
        priorities = [generate.prepare(o, 0).request.priority for o in observations]
        self.assertTrue(65 <= sum(priorities) <= 135)
        self.assertEqual(priorities, [generate.prepare(o, 0).request.priority for o in observations])
        self.assertEqual(generate.prepare(replace(observations[0], priority=1), 0).request.priority, 1)
        for ratio, expected in ((0, 0), (1, 1)):
            generated = RequestParameterGenerator(replace(settings, high_priority_ratio=ratio))
            self.assertEqual({generated.prepare(o, 0).request.priority for o in observations}, {expected})

    def test_estimates_drive_ranking_and_quotas_actual_tokens_only_in_feedback(self):
        settings = config(prediction_mode="fixed", predicted_output_tokens=5,
            priority_assignment="binary", high_priority_ratio=0, batch_order="weighted_length")
        generator = RequestParameterGenerator(settings)
        short = generator.prepare(WorkloadRequest("a", 1, 900), 0)
        long = generator.prepare(WorkloadRequest("b", 2, 1, predicted_output_tokens=30), 0)
        ranking = WeightedLengthOrderStrategy(settings)
        self.assertEqual([r.request_id for r in ranking.rank_requests((long.request, short.request))], ["a", "b"])
        self.assertEqual(short.request.total_tokens, 6)
        self.assertEqual(short.observation.output_tokens, 900)
        result = SimulationRunner(settings).run((short.observation, long.observation))
        self.assertEqual(result.requests[0]["predicted_output_tokens"], 5)
        self.assertEqual(result.requests[0]["actual_output_tokens"], 900)
        self.assertEqual(result.endpoints[0]["total_tokens"], 904)
        self.assertIsNone(result.endpoint_history["a"]["mean_ttft_ms"])

    def test_new_style_policy_can_be_loaded_by_module_path(self):
        policy = load_strategy(__name__ + ":NewRoute")
        self.assertTrue(callable(policy.select_endpoint))

    def test_weighted_core_window_ranks_predictions_and_rechecks_candidates(self):
        scheduler = Scheduler((ep(),), SchedulerConfig(max_batch_size=2,
            ranking_policy="weighted_length", input_weight=1, output_weight=2))
        scheduler.submit_request(request("high", priority=1))
        scheduler.submit_request(request("long", predicted_output_tokens=20))
        scheduler.submit_request(request("short", predicted_output_tokens=2))
        self.assertEqual(scheduler.batches[0]["dispatch_order"], ["short", "long"])
        scheduler.states.update_health("a", False, 0)
        scheduler.tick(0)
        self.assertIsNone(scheduler.records["short"].decision)
        scheduler.states.update_health("a", True, 10)
        scheduler.tick(10)
        self.assertEqual([d.request.request_id for d in scheduler.take_decisions()], ["high", "short", "long"])

    def test_simulated_failure_goes_through_shared_feedback_and_cooldown(self):
        class FailureExecutor:
            def plan(self, decision, observation):
                return feedback(decision, decision.dispatched_at_ms + 1, success=False, failure_kind="rate_limit")
        settings = config(priority_assignment="binary", high_priority_ratio=1, cooldown_ms=50)
        result = SimulationRunner(settings, executor=FailureExecutor()).run((WorkloadRequest("a", 1, 1),
                                                                            WorkloadRequest("b", 1, 1)))
        self.assertEqual(result.summary["failed_requests"], 2)
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 51])
        self.assertEqual(result.endpoints[0]["concurrency"], 0)
        self.assertEqual(result.endpoint_history["a"]["failures"], 2)


class NewRoute:
    def select_endpoint(self, request, endpoints, context):
        return endpoints[0].endpoint_id
