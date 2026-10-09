"""One shared owner of quotas, in-flight requests, health and cooldown."""
from collections import deque
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path

from ..common.validation import positive_integer
from .endpoint_history import EndpointHistory
from .request import ExecutionResult


@dataclass(frozen=True)
class EndpointView:
    endpoint_id: str
    rpm_limit: int
    tpm_limit: int
    concurrency_limit: int
    requests_in_window: int
    tokens_in_window: int
    concurrency: int
    healthy: bool = True
    cooldown_until_ms: int = 0
    recent_failures: int = 0
    updated_at_ms: int = 0

    @property
    def rpm_utilization(self):
        return self.requests_in_window / self.rpm_limit

    @property
    def tpm_utilization(self):
        return self.tokens_in_window / self.tpm_limit

    @property
    def concurrency_utilization(self):
        return self.concurrency / self.concurrency_limit

    def can_accept(self, request):
        return (self.requests_in_window < self.rpm_limit
                and self.tokens_in_window + request.total_tokens <= self.tpm_limit
                and self.concurrency < self.concurrency_limit)


@dataclass
class EndpointState:
    config: object
    window: deque = field(default_factory=deque)
    tokens_in_window: int = 0
    concurrency: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    peak_concurrency: int = 0
    peak_requests_in_window: int = 0
    peak_tokens_in_window: int = 0
    healthy: bool = True
    cooldown_until_ms: int = 0
    recent_failures: int = 0
    updated_at_ms: int = 0

    def expire(self, now_ms, window_ms):
        changed = False
        while self.window and self.window[0][0] <= now_ms - window_ms:
            reservation = self.window.popleft()
            self.tokens_in_window -= reservation[1]
            reservation[3] = False
            changed = True
        if self.cooldown_until_ms and self.cooldown_until_ms <= now_ms:
            self.cooldown_until_ms = 0
            changed = True
        return changed

    def view(self):
        return EndpointView(self.config.endpoint_id, self.config.rpm_limit,
                            self.config.tpm_limit, self.config.concurrency_limit,
                            len(self.window), self.tokens_in_window, self.concurrency,
                            self.healthy, self.cooldown_until_ms, self.recent_failures, self.updated_at_ms)


class EndpointStateManager:
    def __init__(self, endpoints, config):
        endpoints = tuple(endpoints)
        if not endpoints or len({e.endpoint_id for e in endpoints}) != len(endpoints):
            raise ValueError("A nonempty set of unique endpoints is required")
        self.configs = {e.endpoint_id: e for e in endpoints}
        self.states = {e.endpoint_id: EndpointState(e) for e in endpoints}
        self.settings = config
        self.inflight = {}
        self.history = EndpointHistory(config.history_window_ms, config.history_max_samples)
        self.epoch = 0
        self.now_ms = 0

    def advance(self, now_ms):
        positive_integer(now_ms, "now_ms", allow_zero=True)
        if now_ms < self.now_ms:
            raise ValueError("Endpoint clock cannot move backwards")
        self.now_ms = now_ms
        for state in self.states.values():
            if state.expire(now_ms, self.settings.window_ms):
                self.epoch += 1

    def snapshots(self, now_ms):
        self.advance(now_ms)
        return tuple(state.view() for state in self.states.values())

    def reserve(self, decision):
        request, endpoint_id, now_ms = decision.request, decision.selected_endpoint_id, decision.dispatched_at_ms
        self.advance(now_ms)
        if request.request_id in self.inflight:
            raise ValueError("Request already in flight")
        state = self.states[endpoint_id]
        view = state.view()
        if not view.healthy or view.cooldown_until_ms > now_ms or not view.can_accept(request):
            raise RuntimeError("Endpoint unavailable or capacity exceeded")
        reservation = [now_ms, request.total_tokens, request.request_id, True]
        state.window.append(reservation)
        state.tokens_in_window += request.total_tokens
        state.concurrency += 1
        state.total_requests += 1
        state.total_tokens += request.total_tokens
        state.peak_concurrency = max(state.peak_concurrency, state.concurrency)
        state.peak_requests_in_window = max(state.peak_requests_in_window, len(state.window))
        state.peak_tokens_in_window = max(state.peak_tokens_in_window, state.tokens_in_window)
        state.updated_at_ms = now_ms
        self.inflight[request.request_id] = (decision, reservation)
        self.epoch += 1

    def complete(self, result):
        if not isinstance(result, ExecutionResult):
            raise TypeError("Feedback must be ExecutionResult")
        if result.request_id not in self.inflight:
            raise ValueError("Unknown or already completed request")
        decision, reservation = self.inflight[result.request_id]
        if result.endpoint_id != decision.selected_endpoint_id or result.started_at_ms != decision.dispatched_at_ms:
            raise ValueError("Feedback does not match the reserved request")
        if result.e2e_ms is None:
            result = replace(result, e2e_ms=result.finished_at_ms - decision.request.arrival_time)
        # Completion timestamps may precede delivery of the feedback message.
        self.advance(max(self.now_ms, result.finished_at_ms))
        state = self.states[result.endpoint_id]
        request = decision.request
        actual_tokens = ((request.input_tokens if result.actual_input_tokens is None else result.actual_input_tokens)
                         + (request.predicted_output_tokens if result.actual_output_tokens is None else result.actual_output_tokens))
        delta = actual_tokens - reservation[1]
        if reservation[3]:
            state.tokens_in_window += delta
            reservation[1] = actual_tokens
            state.peak_tokens_in_window = max(state.peak_tokens_in_window, state.tokens_in_window)
        state.total_tokens += delta
        state.concurrency -= 1
        if result.success:
            state.recent_failures = 0
        else:
            state.recent_failures += 1
            if result.failure_kind in ("endpoint", "rate_limit"):
                until = result.finished_at_ms + self.settings.cooldown_ms
                if until > self.now_ms:
                    state.cooldown_until_ms = max(state.cooldown_until_ms, until)
        state.updated_at_ms = self.now_ms
        del self.inflight[result.request_id]
        self.history.add(result, self.now_ms)
        self.epoch += 1
        return result

    def update_health(self, endpoint_id, healthy, now_ms, *, cooldown_until_ms=None):
        if not isinstance(healthy, bool):
            raise ValueError("healthy must be bool")
        if cooldown_until_ms is not None:
            positive_integer(cooldown_until_ms, "cooldown_until_ms", allow_zero=True)
        self.advance(now_ms)
        state = self.states[endpoint_id]
        state.healthy = healthy
        if cooldown_until_ms is not None:
            state.cooldown_until_ms = cooldown_until_ms
        state.updated_at_ms = now_ms
        self.epoch += 1

    def next_recovery(self):
        times = []
        for state in self.states.values():
            if state.window:
                times.append(state.window[0][0] + self.settings.window_ms)
            if state.cooldown_until_ms > self.now_ms:
                times.append(state.cooldown_until_ms)
        return min(times) if times else None

    def export(self, now_ms):
        views = self.snapshots(now_ms)
        return [{**asdict(view), "rpm_utilization": view.rpm_utilization,
                 "tpm_utilization": view.tpm_utilization, "concurrency_utilization": view.concurrency_utilization,
                 **{name: getattr(self.states[view.endpoint_id], name) for name in (
                     "total_requests", "total_tokens", "peak_concurrency", "peak_requests_in_window", "peak_tokens_in_window")}}
                for view in views]

    def load_health_state(self, path, now_ms=0):
        """Restore administrative health/cooldown, never invent in-flight reservations."""
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        rows = data.get("endpoints") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("Dynamic state must contain endpoints")
        updates = []
        for row in rows:
            if row["endpoint_id"] not in self.states or row.get("concurrency", 0) != 0:
                raise ValueError("Cannot restore unknown endpoints or unfinished executions")
            healthy = row.get("healthy", True)
            cooldown = row.get("cooldown_until_ms", 0)
            if not isinstance(healthy, bool):
                raise ValueError("healthy must be bool")
            positive_integer(cooldown, "cooldown_until_ms", allow_zero=True)
            updates.append((row["endpoint_id"], healthy, cooldown))
        for endpoint_id, healthy, cooldown in updates:
            self.update_health(endpoint_id, healthy, now_ms, cooldown_until_ms=cooldown)
