"""Offline actual observations plus retained public endpoint import names."""
from dataclasses import dataclass, field
from copy import deepcopy

from ..common.validation import positive_integer


@dataclass(frozen=True)
class WorkloadRequest:
    request_id: str
    input_tokens: int
    output_tokens: int
    source_line: int | None = None
    priority_level: int = 1
    priority_level_source: str = "default"
    priority: int | None = None
    predicted_output_tokens: int | None = None
    target_model: str | None = None
    messages: tuple[dict, ...] = ()
    max_tokens: int | None = None
    stream: bool | None = None
    slo: dict | None = None
    metadata: dict = field(default_factory=dict)
    api_type: str = "chat"

    def __post_init__(self):
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be nonempty")
        for name in ("input_tokens", "output_tokens"):
            positive_integer(getattr(self, name), name, allow_zero=True)
        if self.source_line is not None:
            positive_integer(self.source_line, "source_line", allow_zero=True)
        positive_integer(self.priority_level, "priority_level")
        if self.priority_level > 4:
            raise ValueError("priority_level must be an integer from 1 to 4")
        if self.priority_level_source not in ("default", "recorded", "synthetic"):
            raise ValueError("Invalid priority_level_source")

        if self.priority is not None:
            positive_integer(self.priority, "priority", allow_zero=True)
            if self.priority not in (0, 1):
                raise ValueError("priority must be 0 or 1")
        if self.predicted_output_tokens is not None:
            positive_integer(self.predicted_output_tokens, "predicted_output_tokens", allow_zero=True)
        if self.max_tokens is not None:
            positive_integer(self.max_tokens, "max_tokens")
        if self.stream is not None and not isinstance(self.stream, bool):
            raise ValueError("stream must be bool or null")
        if self.target_model is not None and (not isinstance(self.target_model, str) or not self.target_model.strip()):
            raise ValueError("target_model must be a nonempty string or null")
        object.__setattr__(self, "messages", tuple(deepcopy(self.messages)))
        object.__setattr__(self, "metadata", deepcopy(self.metadata))

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens
from ..core.endpoint_state import EndpointView
from .execution import SimulatedEndpoint as EndpointState
