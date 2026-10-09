"""Stateful orchestration shared by real ingress and simulated arrivals.

Call submit_request for ingress, accept_feedback for completion, and tick at
next_wakeup_ms for timers. This module generates no traffic or service times.
"""
from collections import deque
from dataclasses import asdict
import inspect

from ..policies.ranking import load_batch_order, validate_batch_order
from .admission import AdmissionPolicy
from .batch_window import BatchWindow
from .busy_detector import SystemLoad
from .config import SchedulerConfig
from .endpoint_filter import EndpointFilter
from .endpoint_state import EndpointStateManager
from .request import SchedulerRequest, SchedulingRecord, ExecutionResult
from .request_dispatcher import RequestDispatcher
from .router import Router


class Scheduler:
    def __init__(self, endpoints, config=None, *, state_manager=None, endpoint_filter=None,
                 routing_policy=None, ranking_policy=None, classifier=None, on_dispatch=None, on_event=None):
        self.config = config or (state_manager.settings if state_manager is not None else SchedulerConfig())
        if state_manager is not None and self.config != state_manager.settings:
            raise ValueError("Scheduler and shared state manager must use the same settings")
        self.states = state_manager or EndpointStateManager(endpoints, self.config)
        if self.config.busy_concurrency_threshold is None and any(
                self.config.busy_concurrency_reserve >= e.concurrency_limit for e in self.states.configs.values()):
            raise ValueError("busy_concurrency_reserve must be smaller than every endpoint concurrency_limit")
        self.endpoint_filter = endpoint_filter or EndpointFilter()
        self.admission = AdmissionPolicy(self.config)
        self.router = Router(self.states, routing_policy if routing_policy is not None else self.config.routing_policy)
        if classifier is None and self.config.output_classification != "tokens":
            from ..policies.classification import OutputPercentileClassifier
            classifier = OutputPercentileClassifier.load_default(self.config)
        self.classifier = classifier
        self.ranking = ranking_policy if ranking_policy is not None else load_batch_order(
            self.config.ranking_policy, config=self.config, classification_policy=classifier)
        rank = getattr(self.ranking, "rank_requests", None) or getattr(self.ranking, "order_batch", None)
        if not callable(rank) or inspect.iscoroutinefunction(rank):
            raise TypeError("Ranking must implement a synchronous policy")
        self.window = BatchWindow(self.config.max_batch_size, self.config.max_wait_ms)
        self.dispatcher = RequestDispatcher()
        self.records = {}
        self.batches = []
        self.outbox = deque()
        self.on_dispatch = on_dispatch
        self.on_event = on_event
        self.now_ms = self.states.now_ms
        self._last_load_key = None
        self._failed_attempts = {}

    def emit(self, kind, **fields):
        if self.on_event is not None:
            self.on_event({"time_ms": self.now_ms, "event": kind, **fields})

    def observe_load(self):
        load = self.admission.load_monitor.snapshot(self.states.snapshots(self.now_ms))
        key = tuple((e.endpoint_id, e.busy, e.reasons) for e in load.endpoints)
        if key != self._last_load_key:
            self.emit("load_changed", **load.record())
            self._last_load_key = key
        return load

    def advance_to(self, now_ms, *, observe=True):
        self.states.advance(now_ms)
        self.now_ms = now_ms
        if observe:
            self.observe_load()

    def _pool(self, request):
        return self.endpoint_filter.filter(request, self.states.configs,
                                           self.states.snapshots(self.now_ms), self.now_ms)

    def submit_request(self, request):
        if isinstance(request, dict):
            request = SchedulerRequest(**request)
        if not isinstance(request, SchedulerRequest):
            raise TypeError("Ingress expects SchedulerRequest")
        request = SchedulerRequest(**asdict(request))
        if request.request_id in self.records:
            raise ValueError(f"Duplicate request_id: {request.request_id}")
        self.advance_to(request.arrival_time)
        record = SchedulingRecord(request)
        self.records[request.request_id] = record
        input_heavy = request.input_tokens >= self.config.input_threshold_tokens
        output_heavy = request.predicted_output_tokens >= self.config.output_threshold_tokens
        record.classification = {"input_heavy": input_heavy, "output_heavy": output_heavy,
                                 "heavy": input_heavy or output_heavy}
        if self.classifier is not None:
            record.classification.update(output_heavy=None, heavy=None)
        self.emit("arrived", request_id=request.request_id, heavy=record.classification["heavy"],
                  **({"classification_pending": True} if self.classifier is not None else {}))
        pool = self._pool(request)
        if not pool.compatible:
            self._classify((request,), None, "immediate")
            self._reject(record, "no_compatible_endpoint")
            return record
        load = self.admission.load_monitor.snapshot(pool.eligible) if pool.eligible else SystemLoad(())
        decision = self.admission.decide(request, load)
        record.scheduling_path = decision.scheduling_path
        record.endpoint_scope = decision.endpoint_scope
        record.system_busy_at_arrival = load.busy
        record.busy_endpoints_at_arrival = tuple(e.endpoint_id for e in load.endpoints if e.busy)
        self.emit("admission_decided", request_id=request.request_id, priority=request.priority,
                  scheduling_path=decision.scheduling_path, endpoint_scope=decision.endpoint_scope, **load.record())
        if decision.scheduling_path == "busy_window":
            self._release(self.window.add(request, self.now_ms))
        else:
            self._classify((request,), None, "immediate")
            self.dispatcher.enqueue_immediate(request)
            self.dispatcher.drain(self._try_dispatch, include_window=False)
        return record

    def _classify(self, requests, batch_id, context):
        if self.classifier is None:
            return None
        decisions, snapshot = self.classifier.classify_batch(
            tuple(requests), self.states.snapshots(self.now_ms), len(self.dispatcher), self.now_ms, batch_id)
        if set(decisions) != {r.request_id for r in requests}:
            raise ValueError("Classifier must classify every request")
        for request in requests:
            self.records[request.request_id].classification.update(decisions[request.request_id])
        snapshot["classification_context"] = context
        self.emit("classification_updated", **snapshot)
        return snapshot

    def _release(self, batch):
        if batch is None:
            return
        snapshot = self._classify(batch.requests, batch.batch_id, "window")
        views = self.states.snapshots(self.now_ms)
        rank = getattr(self.ranking, "rank_requests", None)
        if rank:
            ordered = rank(batch.requests, {"endpoints": views, "now_ms": self.now_ms,
                                           "classification": {r.request_id: self.records[r.request_id].classification for r in batch.requests}})
            ordered_ids = [r.request_id for r in ordered]
        else:
            ordered_ids = self.ranking.order_batch(batch.requests, views, self.now_ms)
        ordered_ids = validate_batch_order(batch.requests, ordered_ids)
        info = {"batch_id": batch.batch_id, "trigger": batch.trigger, "released_at_ms": batch.released_at_ms,
                "size": len(batch.requests), "request_ids": [r.request_id for r in batch.requests],
                "dispatch_order": list(ordered_ids)}
        if snapshot is not None:
            info["classification"] = snapshot
        self.batches.append(info)
        self.emit("batch_released", **info)
        for position, request_id in enumerate(ordered_ids):
            record = self.records[request_id]
            record.batch_id = batch.batch_id
            record.batch_position = position
            record.batch_trigger = batch.trigger
            record.batch_released_at_ms = batch.released_at_ms
        self.dispatcher.enqueue_window(ordered_ids)

    def _reject(self, record, reason):
        record.status = "rejected"
        record.rejection_reason = reason
        record.rejected_at_ms = self.now_ms
        self._failed_attempts.pop(record.request.request_id, None)
        self.emit("rejected", request_id=record.request.request_id, reason=reason)

    def _try_dispatch(self, request_id):
        record = self.records[request_id]
        if self._failed_attempts.get(request_id) == self.states.epoch:
            return False
        request = record.request
        pool = self._pool(request)
        if not pool.compatible:
            self._reject(record, "no_compatible_endpoint")
            return True
        if all(request.total_tokens > e.tpm_limit for e in pool.compatible):
            self._reject(record, "tokens_exceed_every_endpoint_tpm_limit")
            return True
        eligible = self.admission.routing_endpoints(pool.eligible, record.endpoint_scope)
        candidates = tuple(e for e in eligible if e.can_accept(request))
        if not candidates:
            self.emit("capacity_wait", request_id=request_id, endpoint_scope=record.endpoint_scope,
                      eligible_endpoint_ids=[e.endpoint_id for e in eligible])
            self._failed_attempts[request_id] = self.states.epoch
            return False
        decision = self.router.route(request, candidates, self.now_ms)
        record.decision = decision
        record.endpoint_before = next(e for e in candidates if e.endpoint_id == decision.selected_endpoint_id)
        self.states.reserve(decision)
        record.status = "running"
        self._failed_attempts.pop(request_id, None)
        if self.on_dispatch is None:
            self.outbox.append(decision)
        else:
            try:
                self.on_dispatch(decision)
            except Exception:
                self.accept_feedback(ExecutionResult(request_id, decision.selected_endpoint_id, False,
                                                     self.now_ms, self.now_ms, failure_kind="request"), drain=False)
                raise
        self.emit("dispatched", request_id=request_id, endpoint_id=decision.selected_endpoint_id,
                  batch_id=record.batch_id, batch_position=record.batch_position, priority=request.priority,
                  scheduling_path=record.scheduling_path,
                  endpoint_state_after=asdict(self.states.states[decision.selected_endpoint_id].view()))
        self.observe_load()
        return True

    def tick(self, now_ms):
        self.advance_to(now_ms)
        if self.window and self.window.deadline <= now_ms:
            self._release(self.window.release("timeout", now_ms))
        self.dispatcher.drain(self._try_dispatch)

    def flush(self):
        self._release(self.window.release("end_of_input", self.now_ms))

    def accept_feedback(self, result, *, drain=True):
        if not isinstance(result, ExecutionResult):
            raise TypeError("Feedback must be ExecutionResult")
        if result.request_id not in self.records or self.records[result.request_id].status != "running":
            raise ValueError("Feedback requires a running scheduler request")
        result = self.states.complete(result)
        self.now_ms = self.states.now_ms
        record = self.records[result.request_id]
        record.result = result
        record.status = "completed" if result.success else "failed"
        self.emit("completed" if result.success else "failed", request_id=result.request_id,
                  endpoint_id=result.endpoint_id, concurrency_after=self.states.states[result.endpoint_id].concurrency)
        if drain:
            self.tick(self.now_ms)

    @property
    def has_waiting(self):
        return bool(self.window or self.dispatcher)

    @property
    def next_wakeup_ms(self):
        times = [self.window.deadline] if self.window else []
        recovery = self.states.next_recovery() if self.dispatcher else None
        if recovery is not None:
            times.append(recovery)
        return min(times) if times else None

    def take_decisions(self):
        decisions = tuple(self.outbox)
        self.outbox.clear()
        return decisions

    def reject_waiting(self, reason="no_available_endpoint"):
        self.flush()
        for queue in (self.dispatcher.urgent, self.dispatcher.immediate, self.dispatcher.window):
            while queue:
                self._reject(self.records[queue.popleft()], reason)
