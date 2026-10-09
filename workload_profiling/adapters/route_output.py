"""Stream dispatched LiteLLM handoffs to one atomically published JSONL file."""
import json
import os
from pathlib import Path
import tempfile

from .litellm_adapter import LiteLLMAdapter


class RouteOutputRecorder:
    """Synchronous on_dispatch/on_route callback; publish only on clean exit.

    Each line contains an execution handoff, not a simulated response. Pending
    and rejected requests have no route decision and therefore no output line.
    Use one recorder per serial scheduling run. No network calls are made.
    """

    def __init__(self, path, endpoints, *, adapter=None):
        self.path = Path(path).resolve()
        self.endpoints = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
        self.adapter = adapter if adapter is not None else LiteLLMAdapter()
        self.count = 0
        self._stream = None
        self._temporary = None
        self._used = False

    def __enter__(self):
        if self._used:
            raise RuntimeError("Create a new RouteOutputRecorder for each run")
        self._used = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, delete=False)
        self._temporary = Path(stream.name)
        self._stream = stream
        return self

    def __call__(self, decision):
        if self._stream is None or self._stream.closed:
            raise RuntimeError("RouteOutputRecorder must be used inside its context")
        try:
            endpoint = self.endpoints[decision.selected_endpoint_id]
        except KeyError as error:
            raise ValueError("Route decision selected an unknown endpoint") from error
        record = {
            "request_id": decision.request.request_id,
            "selected_endpoint_id": decision.selected_endpoint_id,
            "dispatched_at_ms": decision.dispatched_at_ms,
            "litellm_params": self.adapter.build_params(decision, endpoint),
        }
        self._stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self.count += 1

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self._stream.flush()
                os.fsync(self._stream.fileno())
            self._stream.close()
            if error_type is None:
                os.replace(self._temporary, self.path)
        finally:
            if not self._stream.closed:
                self._stream.close()
            self._temporary.unlink(missing_ok=True)
        return False
