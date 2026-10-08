"""Request and endpoint state; tokens are reserved in full at dispatch."""
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math

from .config import EndpointConfig, positive_integer, finite_number


@dataclass(frozen=True)
class WorkloadRequest:
    request_id: str
    input_tokens: int
    output_tokens: int
    source_line: int | None = None
    priority_level: int = 1
    priority_level_source: str = "default"

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

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class EndpointView:
    endpoint_id: str
    rpm_limit: int
    tpm_limit: int
    concurrency_limit: int
    requests_in_window: int
    tokens_in_window: int
    concurrency: int

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
    config: EndpointConfig
    window: deque = field(default_factory=deque)
    tokens_in_window: int = 0
    concurrency: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    peak_concurrency: int = 0
    peak_requests_in_window: int = 0
    peak_tokens_in_window: int = 0

    def expire(self, now_ms, window_ms):
        # Window is (now-60000, now]; an entry at the left boundary expires.
        while self.window and self.window[0][0] <= now_ms - window_ms:
            _, tokens = self.window.popleft()
            self.tokens_in_window -= tokens

    def view(self, now_ms, window_ms):
        self.expire(now_ms, window_ms)
        return EndpointView(self.config.endpoint_id, self.config.rpm_limit,
                            self.config.tpm_limit, self.config.concurrency_limit,
                            len(self.window), self.tokens_in_window, self.concurrency)

    def dispatch(self, request, now_ms, window_ms):
        if not self.view(now_ms, window_ms).can_accept(request):
            raise RuntimeError("Endpoint capacity exceeded")
        self.window.append((now_ms, request.total_tokens))
        self.tokens_in_window += request.total_tokens
        self.concurrency += 1
        self.total_requests += 1
        self.total_tokens += request.total_tokens
        self.peak_concurrency = max(self.peak_concurrency, self.concurrency)
        self.peak_requests_in_window = max(self.peak_requests_in_window, len(self.window))
        self.peak_tokens_in_window = max(self.peak_tokens_in_window, self.tokens_in_window)
        return now_ms + self.service_time_ms(request)

    def service_multiplier(self, request):
        """Stable per request/endpoint draw, independent of dispatch order and time."""
        c = self.config
        if c.service_jitter_fraction == 0:
            return 1.0
        key = json.dumps([c.service_jitter_seed, request.request_id, c.endpoint_id],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        draw = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") >> 11
        uniform = draw / (2 ** 53)
        return 1 + c.service_jitter_fraction * (2 * uniform - 1)

    def service_time_ms(self, request):
        c = self.config
        nominal = (c.base_latency_ms + request.input_tokens / c.input_tokens_per_ms
                   + request.output_tokens / c.output_tokens_per_ms)
        return max(1, math.ceil(nominal * self.service_multiplier(request)))

    def complete(self):
        if self.concurrency <= 0:
            raise RuntimeError("Completion without an in-flight request")
        self.concurrency -= 1

    def next_expiry(self, window_ms):
        return self.window[0][0] + window_ms if self.window else None
