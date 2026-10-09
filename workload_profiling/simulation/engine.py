"""Virtual-clock experiment driver around the same core used by real requests."""
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
import heapq
import inspect

from ..common.metrics import summarize_ms as statistics
from ..core import Scheduler
from .ordering import load_batch_order
from .routing import load_strategy
from .data_processing import RequestParameterGenerator
from .request_sender import SimulatedRequestSender
from .execution import SimulatedExecutor


@dataclass
class RunResult:
    requests: list[dict]
    events: list[dict]
    batches: list[dict]
    endpoints: list[dict]
    summary: dict
    classification_artifacts: dict | None = None
    endpoint_history: dict = field(default_factory=dict)


class SimulationRunner:
    def __init__(self, config, *, strategy=None, batch_order=None, classification_policy=None, executor=None):
        self.config = config
        if classification_policy is not None and config.output_classification == "tokens":
            raise ValueError("Injected percentile policy requires percentile output_classification")
        self.classification_policy = classification_policy
        if config.output_classification != "tokens" and classification_policy is None:
            from .classification import OutputPercentileClassifier
            self.classification_policy = OutputPercentileClassifier.load_default(config)
        self.strategy = strategy if strategy is not None else load_strategy(config.strategy)
        selector = getattr(self.strategy, "select_endpoint", None) or getattr(self.strategy, "select", None)
        if not callable(selector) or inspect.iscoroutinefunction(selector):
            raise TypeError("Routing must implement a synchronous selector")
        self.batch_order = batch_order if batch_order is not None else load_batch_order(
            config.batch_order, config=config, classification_policy=self.classification_policy)
        rank = getattr(self.batch_order, "rank_requests", None) or getattr(self.batch_order, "order_batch", None)
        if not callable(rank) or inspect.iscoroutinefunction(rank):
            raise TypeError("Batch ordering must implement a synchronous policy")
        self.executor = executor or SimulatedExecutor(config.endpoints)
        self._used = False

    def run(self, requests):
        if self._used:
            raise RuntimeError("Create a new SimulationRunner for each replay")
        self._used = True
        config = self.config
        generator = RequestParameterGenerator(config)
        sender = SimulatedRequestSender(config)
        scheduled = iter(sender.schedule(requests))
        observations, plans, running, events = {}, {}, [], []
        sequence = 0

        class RankingAdapter:
            def order_batch(adapter_self, core_requests, endpoints, now_ms):
                legacy_order = getattr(self.batch_order, "order_batch", None)
                if callable(legacy_order):
                    # Preserve four-level ordering only in recorded simulation;
                    # every output length exposed to ranking is still an estimate.
                    legacy_requests = tuple(replace(observations[r.request_id], output_tokens=r.predicted_output_tokens)
                                            for r in core_requests)
                    return legacy_order(legacy_requests, endpoints, now_ms)
                rank = getattr(self.batch_order, "rank_requests", None)
                return [r.request_id for r in rank(core_requests, {"endpoints": endpoints, "now_ms": now_ms})]

        def dispatched(decision):
            nonlocal sequence
            feedback = self.executor.plan(decision, observations[decision.request.request_id])
            plans[decision.request.request_id] = feedback
            sequence += 1
            heapq.heappush(running, (feedback.finished_at_ms, sequence, feedback))

        def record_event(event):
            event = dict(event)
            request_id = event.get("request_id")
            if "priority" in event:
                event["priority_level"] = observations[request_id].priority_level
            if event["event"] == "dispatched":
                event["finished_at_ms"] = plans[request_id].finished_at_ms
            events.append(event)

        scheduler = Scheduler(tuple(e.to_core() for e in config.endpoints), config.scheduler_config(),
                              routing_policy=self.strategy, ranking_policy=RankingAdapter(),
                              classifier=self.classification_policy, on_dispatch=dispatched, on_event=record_event)
        item = next(scheduled, None)
        next_arrival = item[0] if item is not None else 0
        now = 0
        while next_arrival is not None or scheduler.has_waiting or running:
            times = []
            if next_arrival is not None:
                times.append(next_arrival)
            if running:
                times.append(running[0][0])
            if scheduler.next_wakeup_ms is not None:
                times.append(scheduler.next_wakeup_ms)
            if not times:
                scheduler.reject_waiting()
                break
            now = min(times)
            scheduler.advance_to(now, observe=False)
            while running and running[0][0] <= now:
                _, _, feedback = heapq.heappop(running)
                scheduler.accept_feedback(feedback, drain=False)
            scheduler.observe_load()
            while next_arrival == now:
                if item is None:
                    next_arrival = None
                    scheduler.flush()
                else:
                    prepared = generator.prepare(item[1], now)
                    observations[prepared.request.request_id] = prepared.observation
                    sender.submit(prepared.request, scheduler)
                    item = next(scheduled, None)
                    next_arrival = item[0] if item is not None else now + config.arrival_interval_ms
            scheduler.tick(now)

        rows = []
        for request_id, record in scheduler.records.items():
            observation = observations[request_id]
            request, decision, feedback = record.request, record.decision, record.result
            ready_at = record.batch_released_at_ms if record.batch_released_at_ms is not None else request.arrival_time
            dispatch_at = decision.dispatched_at_ms if decision is not None else None
            finished_at = feedback.finished_at_ms if feedback is not None else record.rejected_at_ms
            view = record.endpoint_before
            row = {name: getattr(observation, name) for name in (
                "request_id", "input_tokens", "output_tokens", "source_line", "priority_level", "priority_level_source")}
            row.update(total_tokens=observation.total_tokens, priority=request.priority,
                       target_model=request.target_model, stream=request.stream,
                       predicted_output_tokens=request.predicted_output_tokens,
                       actual_input_tokens=feedback.actual_input_tokens if feedback else None,
                       actual_output_tokens=feedback.actual_output_tokens if feedback else None,
                       reserved_tokens=request.total_tokens, arrival_at_ms=request.arrival_time,
                       **record.classification)
            row.update(batch_id=record.batch_id, batch_position=record.batch_position, batch_trigger=record.batch_trigger,
                batch_released_at_ms=record.batch_released_at_ms, batch_wait_ms=ready_at-request.arrival_time,
                capacity_wait_ms=(dispatch_at if dispatch_at is not None else finished_at)-ready_at,
                queue_wait_ms=(dispatch_at if dispatch_at is not None else finished_at)-request.arrival_time,
                endpoint_id=decision.selected_endpoint_id if decision else None, dispatch_at_ms=dispatch_at,
                finished_at_ms=finished_at, service_ms=finished_at-dispatch_at if dispatch_at is not None else 0,
                latency_ms=finished_at-request.arrival_time, status=record.status, rejection_reason=record.rejection_reason,
                endpoint_rpm_before=view.requests_in_window if view else None,
                endpoint_tpm_before=view.tokens_in_window if view else None,
                endpoint_concurrency_before=view.concurrency if view else None,
                rpm_utilization_before=view.rpm_utilization if view else None,
                tpm_utilization_before=view.tpm_utilization if view else None,
                concurrency_utilization_before=view.concurrency_utilization if view else None,
                scheduling_path=record.scheduling_path, endpoint_scope=record.endpoint_scope,
                system_busy_at_arrival=record.system_busy_at_arrival,
                busy_endpoints_at_arrival=list(record.busy_endpoints_at_arrival),
                routing_candidate_ids=list(decision.candidate_ids) if decision else [])
            rows.append(row)
        batches = scheduler.batches
        states = scheduler.states.export(now)
        heavy_rows = [r for r in rows if r["heavy"]]
        completed_heavy = [r for r in heavy_rows if r["status"] == "completed"]
        completed = [r for r in rows if r["status"] == "completed"]
        completed_light = [r for r in completed if not r["heavy"]]
        dispatched = [r for r in rows if r["dispatch_at_ms"] is not None]

        summary = {"clock": "virtual_ms", "output_length_mode": "oracle_recorded_response",
                   "arrival_mode": config.arrival_mode,
                   "output_classification": config.output_classification,
                   "priority_assignment": config.priority_assignment,
                   "strategy": config.strategy, "strategy_class": type(self.strategy).__module__ + "." + type(self.strategy).__qualname__,
                   "batch_order": config.batch_order,
                   "batch_order_class": type(self.batch_order).__module__ + "." + type(self.batch_order).__qualname__,
                   "total_requests": len(rows), "light_requests": len(rows)-len(heavy_rows),
                   "heavy_requests": len(heavy_rows), "input_heavy_requests": sum(r["input_heavy"] for r in rows),
                   "output_heavy_requests": sum(r["output_heavy"] for r in rows),
                   "both_heavy_requests": sum(r["input_heavy"] and r["output_heavy"] for r in rows),
                   "completed_requests": sum(r["status"] == "completed" for r in rows),
                   "rejected_requests": sum(r["status"] == "rejected" for r in rows),
                   "batch_count": len(batches), "batch_triggers": dict(Counter(b["trigger"] for b in batches)),
                   "last_arrival_ms": rows[-1]["arrival_at_ms"] if rows else None,
                   "simulation_end_ms": now, "heavy_queue_wait_ms": statistics([r["queue_wait_ms"] for r in completed_heavy]),
                   "heavy_latency_ms": statistics([r["latency_ms"] for r in completed_heavy]),
                   "all_latency_ms": statistics([r["latency_ms"] for r in completed]),
                   "light_latency_ms": statistics([r["latency_ms"] for r in completed_light]),
                   "all_queue_wait_ms": statistics([r["queue_wait_ms"] for r in completed]),
                   "all_batch_wait_ms": statistics([r["batch_wait_ms"] for r in completed]),
                   "all_capacity_wait_ms": statistics([r["capacity_wait_ms"] for r in completed]),
                   "capacity_wait_requests": sum(r["capacity_wait_ms"] > 0 for r in completed),
                   "endpoint_executed_requests": len(dispatched),
                   "endpoint_executed_light_requests": sum(not r["heavy"] for r in dispatched),
                   "endpoint_dispatch_counts": {e["endpoint_id"]: e["total_requests"] for e in states}}
        summary.update(scheduling_mode="adaptive",
                       scheduling_paths=dict(Counter(row["scheduling_path"] for row in rows)),
                       busy_thresholds={"rpm": config.busy_rpm_threshold, "tpm": config.busy_tpm_threshold,
                                        "concurrency_reserve": config.busy_concurrency_reserve})
        summary["priority_counts"] = {str(p): sum(r["priority"] == p for r in rows) for p in (0, 1)}
        summary["failed_requests"] = sum(r["status"] == "failed" for r in rows)
        if config.busy_concurrency_threshold is not None:
            summary["busy_thresholds"]["concurrency_fraction"] = config.busy_concurrency_threshold
        summary["priority_levels"] = {}
        for level in range(1, 5):
            members = [row for row in rows if row["priority_level"] == level]
            finished = [row for row in members if row["status"] == "completed"]
            summary["priority_levels"][str(level)] = {
                "requests": len(members), "completed": len(finished),
                "rejected": sum(row["status"] == "rejected" for row in members),
                "latency_ms": statistics([row["latency_ms"] for row in finished]),
                "queue_wait_ms": statistics([row["queue_wait_ms"] for row in finished])}
        if self.classification_policy is not None:
            summary["output_percentile_reference_samples"] = self.classification_policy.reference.sample_count
            summary["threshold_update_count"] = sum(s["threshold_changed"] for s in self.classification_policy.trace)
            summary["threshold_values_used"] = sorted({s["threshold"] for s in self.classification_policy.trace})
        artifacts = self.classification_policy.artifacts() if self.classification_policy is not None else None
        history = {key: asdict(value) for key, value in scheduler.states.history.snapshot(
            scheduler.states.configs, now).items()}
        return RunResult(rows, events, batches, states, summary, artifacts, history)
