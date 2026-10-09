"""Simple contracts shared by admission, routing, execution and feedback."""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..common.validation import finite_number, positive_integer


@dataclass(frozen=True)
class SLO:
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    e2e_ms: float | None = None

    def __post_init__(self):
        for name in ("ttft_ms", "tpot_ms", "e2e_ms"):
            if getattr(self, name) is not None:
                finite_number(getattr(self, name), name, allow_zero=True)


@dataclass(frozen=True)
class SchedulerRequest:
    request_id: str
    target_model: str
    input_tokens: int
    predicted_output_tokens: int
    priority: int = 0
    arrival_time: int = 0
    messages: tuple[dict, ...] = ()
    max_tokens: int | None = None
    stream: bool = False
    api_type: str = "chat"
    slo: SLO | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("request_id", "target_model", "api_type"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be nonempty")
        for name in ("input_tokens", "predicted_output_tokens", "arrival_time", "priority"):
            positive_integer(getattr(self, name), name, allow_zero=True)
        if self.priority not in (0, 1):
            raise ValueError("priority must be 0 or 1")
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be bool")
        if self.max_tokens is not None:
            positive_integer(self.max_tokens, "max_tokens")
        if isinstance(self.messages, (str, dict)) or any(not isinstance(m, dict) for m in self.messages):
            raise ValueError("messages must be a sequence of objects")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be an object")
        object.__setattr__(self, "messages", tuple(deepcopy(self.messages)))
        object.__setattr__(self, "metadata", deepcopy(self.metadata))
        if isinstance(self.slo, dict):
            object.__setattr__(self, "slo", SLO(**self.slo))
        elif self.slo is not None and not isinstance(self.slo, SLO):
            raise ValueError("slo must be an SLO or null")

    @property
    def total_tokens(self):
        return self.input_tokens + self.predicted_output_tokens

    @property
    def output_tokens(self):
        """Read-only estimate alias for existing ranking/classification plugins."""
        return self.predicted_output_tokens


@dataclass(frozen=True)
class RequestBatch:
    batch_id: int
    requests: tuple[SchedulerRequest, ...]
    released_at_ms: int
    trigger: str


@dataclass(frozen=True)
class RouteDecision:
    request: SchedulerRequest
    selected_endpoint_id: str
    dispatched_at_ms: int
    candidate_ids: tuple[str, ...]
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    request_id: str
    endpoint_id: str
    success: bool
    started_at_ms: int
    finished_at_ms: int
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    failure_kind: str | None = None
    e2e_ms: float | None = None

    def __post_init__(self):
        if (any(not isinstance(value, str) or not value.strip() for value in (self.request_id, self.endpoint_id))
                or not isinstance(self.success, bool)):
            raise ValueError("Feedback requires IDs and boolean success")
        for name in ("started_at_ms", "finished_at_ms"):
            positive_integer(getattr(self, name), name, allow_zero=True)
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("Feedback finish precedes start")
        for name in ("actual_input_tokens", "actual_output_tokens"):
            if getattr(self, name) is not None:
                positive_integer(getattr(self, name), name, allow_zero=True)
        for name in ("ttft_ms", "tpot_ms", "e2e_ms"):
            if getattr(self, name) is not None:
                finite_number(getattr(self, name), name, allow_zero=True)
        if self.failure_kind not in (None, "endpoint", "rate_limit", "request", "cancelled"):
            raise ValueError("Unknown failure_kind")
        if self.success and self.failure_kind is not None:
            raise ValueError("Successful feedback cannot have failure_kind")


@dataclass
class SchedulingRecord:
    request: SchedulerRequest
    scheduling_path: str = ""
    endpoint_scope: str = "all"
    system_busy_at_arrival: bool = False
    busy_endpoints_at_arrival: tuple[str, ...] = ()
    classification: dict = field(default_factory=dict)
    batch_id: int | None = None
    batch_position: int | None = None
    batch_trigger: str | None = None
    batch_released_at_ms: int | None = None
    decision: RouteDecision | None = None
    endpoint_before: Any = None
    result: ExecutionResult | None = None
    status: str = "queued"
    rejection_reason: str | None = None
    rejected_at_ms: int | None = None
