"""Ready request ordering, without execution or endpoint selection algorithms."""
from collections import deque


class RequestDispatcher:
    def __init__(self):
        self.urgent = deque()
        self.immediate = deque()
        self.window = deque()

    def __bool__(self):
        return bool(self.urgent or self.immediate or self.window)

    def __len__(self):
        return len(self.urgent) + len(self.immediate) + len(self.window)

    def enqueue_immediate(self, request):
        (self.urgent if request.priority == 1 else self.immediate).append(request.request_id)

    def enqueue_window(self, ordered_ids):
        self.window.extend(ordered_ids)

    def drain(self, try_dispatch, *, include_window=True):
        for queue in (self.urgent, self.immediate):
            for _ in range(len(queue)):
                request_id = queue.popleft()
                if not try_dispatch(request_id):
                    queue.append(request_id)
        if include_window:
            while self.window:
                if not try_dispatch(self.window[0]):
                    break
                self.window.popleft()
