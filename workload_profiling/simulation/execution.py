"""Recorded actual lengths and virtual completion timing, outside the scheduler."""
from dataclasses import dataclass
from typing import TYPE_CHECKING
import hashlib
import json
import math

from ..core.request import ExecutionResult, SchedulerRequest

if TYPE_CHECKING:
    from .models import WorkloadRequest


class SimulatedEndpoint:
    def __init__(self, config):
        self.config = config

    def service_multiplier(self, request):
        c = self.config
        if c.service_jitter_fraction == 0:
            return 1.0
        key = json.dumps([c.service_jitter_seed, request.request_id, c.endpoint_id],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        draw = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") >> 11
        return 1 + c.service_jitter_fraction * (2 * (draw / 2 ** 53) - 1)

    def service_time_ms(self, request):
        c = self.config
        nominal = c.base_latency_ms + request.input_tokens / c.input_tokens_per_ms + request.output_tokens / c.output_tokens_per_ms
        return max(1, math.ceil(nominal * self.service_multiplier(request)))


@dataclass(frozen=True)
class PreparedSimulationRequest:
    request: SchedulerRequest
    observation: "WorkloadRequest"


class SimulatedExecutor:
    def __init__(self, endpoints):
        self.endpoints = {e.endpoint_id: SimulatedEndpoint(e) for e in endpoints}

    def plan(self, decision, observation):
        service = self.endpoints[decision.selected_endpoint_id].service_time_ms(observation)
        return ExecutionResult(decision.request.request_id, decision.selected_endpoint_id, True,
                               decision.dispatched_at_ms, decision.dispatched_at_ms + service,
                               observation.input_tokens, observation.output_tokens)
