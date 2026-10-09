"""Collection deadline and batch membership; independent of request ranking."""
from .request import RequestBatch
from ..common.validation import positive_integer


class BatchWindow:
    def __init__(self, max_batch_size, max_wait_ms):
        positive_integer(max_batch_size, "max_batch_size")
        positive_integer(max_wait_ms, "max_wait_ms", allow_zero=True)
        self.max_batch_size = max_batch_size
        self.max_wait_ms = max_wait_ms
        self.requests = []
        self.next_batch_id = 0

    def __bool__(self):
        return bool(self.requests)

    @property
    def deadline(self):
        return self.requests[0].arrival_time + self.max_wait_ms if self.requests else None

    def add(self, request, now_ms):
        self.requests.append(request)
        if len(self.requests) >= self.max_batch_size:
            return self.release("batch_size", now_ms)
        return None

    def release(self, reason, now_ms):
        if not self.requests:
            return None
        batch = RequestBatch(self.next_batch_id, tuple(self.requests), now_ms, reason)
        self.requests.clear()
        self.next_batch_id += 1
        return batch
