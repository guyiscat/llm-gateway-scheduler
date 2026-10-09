"""Bounded rolling cache with incrementally maintained performance aggregates."""
from dataclasses import dataclass, field
import heapq

from ..common.validation import positive_integer


@dataclass(frozen=True)
class EndpointPerformance:
    endpoint_id: str
    samples: int
    successes: int
    failures: int
    mean_e2e_ms: float | None
    mean_ttft_ms: float | None
    mean_tpot_ms: float | None


@dataclass
class _HistoryWindow:
    records: list = field(default_factory=list)
    successes: int = 0
    sums: list = field(default_factory=lambda: [0, 0, 0])
    counts: list = field(default_factory=lambda: [0, 0, 0])

    def adjust(self, result, sign):
        if result.success:
            self.successes += sign
            for i, value in enumerate((result.e2e_ms if result.e2e_ms is not None else result.finished_at_ms - result.started_at_ms,
                                       result.ttft_ms, result.tpot_ms)):
                if value is not None:
                    self.sums[i] += sign * value
                    self.counts[i] += sign

    def remove_oldest(self):
        self.adjust(heapq.heappop(self.records)[2], -1)


class EndpointHistory:
    def __init__(self, window_ms, max_samples):
        positive_integer(window_ms, "history_window_ms")
        positive_integer(max_samples, "history_max_samples")
        self.window_ms = window_ms
        self.max_samples = max_samples
        self._windows = {}
        self._sequence = 0
        self.now_ms = 0

    def _expire(self, window):
        while window.records and window.records[0][0] <= self.now_ms - self.window_ms:
            window.remove_oldest()

    def add(self, result, now_ms=None):
        self.now_ms = max(self.now_ms, result.finished_at_ms, now_ms or 0)
        window = self._windows.setdefault(result.endpoint_id, _HistoryWindow())
        self._expire(window)
        if result.finished_at_ms <= self.now_ms - self.window_ms:
            return
        self._sequence += 1
        heapq.heappush(window.records, (result.finished_at_ms, self._sequence, result))
        window.adjust(result, 1)
        if len(window.records) > self.max_samples:
            window.remove_oldest()

    def snapshot(self, endpoint_ids, now_ms):
        self.now_ms = max(self.now_ms, now_ms)
        output = {}
        for endpoint_id in endpoint_ids:
            window = self._windows.setdefault(endpoint_id, _HistoryWindow())
            self._expire(window)
            means = [total / count if count else None for total, count in zip(window.sums, window.counts)]
            output[endpoint_id] = EndpointPerformance(
                endpoint_id, len(window.records), window.successes,
                len(window.records) - window.successes, *means)
        return output
