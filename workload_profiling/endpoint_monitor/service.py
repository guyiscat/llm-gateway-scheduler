"""Refresh the deployment catalog without resetting observed traffic state.

Uses wall-clock milliseconds for real requests. Virtual replay has a separate
state owner and only consumes the discovered configurations.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Condition, Event, RLock, Thread
import time
from uuid import uuid4

from ..common.validation import finite_number
from ..adapters.litellm_adapter import LiteLLMAdapter
from ..core.config import EndpointConfig
from ..core.endpoint_state import EndpointState, EndpointStateManager
from ..core.request import ExecutionResult, RouteDecision
from .catalog import filter_catalog, load_state_configs, redact_credentials


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def response_usage(response):
    body = response.get("response")
    if response.get("response_format") == "sse" and isinstance(body, str):
        for line in reversed(body.splitlines()):
            if line.startswith("data:"):
                try:
                    chunk = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                    body = chunk
                    break
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    values = []
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        values.append(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None)
    return values


class EndpointMonitor:
    def __init__(self, session, directory, settings, *, update_interval_seconds=30,
                 limit_overrides=None, clock=time.time, periodic=True):
        finite_number(update_interval_seconds, "endpoint_update_interval")
        self.session, self.directory, self.settings = session, Path(directory).resolve(), settings
        self.raw_path = self.directory / "endpoint_catalog_raw.json"
        self.state_path = self.directory / "endpoint_state.json"
        self.interval, self.overrides, self.clock = update_interval_seconds, limit_overrides, clock
        self.periodic = periodic
        self.lock, self.stop = RLock(), Event()
        self._condition = Condition(self.lock)
        self._refresh_pending = 0
        self._refresh_active = False
        self._refresh_phase = "idle"
        self.thread = None
        self.states = None
        self.catalog = {}
        self.requests = {}
        self.fetched_at = None
        self.last_error = None
        self.uncertain_requests = []
        self._lock_path = self.directory / ".endpoint_monitor.lock"
        self._running = False
        self._removed_health = {}

    def _require_running(self):
        if not self._running:
            raise RuntimeError("EndpointMonitor must be used inside its context")

    def now_ms(self):
        now = max(0, int(self.clock() * 1000))
        return max(now, self.states.now_ms) if self.states is not None else now

    @contextmanager
    def scheduler_guard(self, *, admission=True):
        """Gate submissions/ticks; use admission=False for completion feedback."""
        with self._condition:
            if admission:
                self._wait_for_admission()
            else:
                self._require_running()
            try:
                yield self.states
            finally:
                # Online Scheduler feedback may release its own reservations.
                self._condition.notify_all()

    def _wait_for_admission(self):
        # Caller holds the condition lock. Waiting releases it so feedback runs.
        self._require_running()
        self._condition.wait_for(lambda: self.stop.is_set() or not self._refresh_pending)
        if self.stop.is_set():
            raise RuntimeError("EndpointMonitor is closing; request was not sent")

    def _has_active_requests(self):
        return self.states is not None and (
            bool(self.states.inflight) or any(state.concurrency for state in self.states.states.values()))

    def _complete(self, result):
        # Caller holds the state lock; notify even if a later file write fails.
        try:
            self.states.complete(result)
        finally:
            self._condition.notify_all()

    def refresh(self):
        owns_refresh = False
        with self._condition:
            self._require_running()
            if self.stop.is_set():
                raise RuntimeError("EndpointMonitor is closing; refresh was cancelled")
            # Close admission before waiting, including concurrent refresh calls.
            self._refresh_pending += 1
        try:
            with self._condition:
                self._condition.wait_for(lambda: self.stop.is_set() or not self._refresh_active)
                if self.stop.is_set():
                    raise RuntimeError("EndpointMonitor is closing; refresh was cancelled")
                self._refresh_active = owns_refresh = True
                self._refresh_phase = "waiting_for_idle"
                if self.states is not None:
                    self.publish()
                self._condition.notify_all()
                self._condition.wait_for(lambda: self.stop.is_set() or not self._has_active_requests())
                if self.stop.is_set():
                    raise RuntimeError("EndpointMonitor is closing; refresh was cancelled")
                self._refresh_phase = "refreshing"
                if self.states is not None:
                    self.publish()
            return self._refresh()
        except Exception as error:
            with self.lock:
                if not self.stop.is_set():
                    self.last_error = type(error).__name__
            raise
        finally:
            with self._condition:
                self._refresh_pending -= 1
                if owns_refresh:
                    self._refresh_active = False
                if not self._refresh_active:
                    self._refresh_phase = "waiting_for_idle" if self._refresh_pending else "idle"
                try:
                    if self.states is not None and self._running:
                        self.publish()
                finally:
                    self._condition.notify_all()

    def _refresh(self):
        # Admission remains closed during HTTP; no state lock across network I/O.
        raw = self.session.get_model_info()
        rows = filter_catalog(raw, limit_overrides=self.overrides, allow_empty=self.states is not None)
        configs = [EndpointConfig(**{key: row[key] for key in EndpointConfig.__dataclass_fields__}) for row in rows]
        if self.settings.busy_concurrency_threshold is None and any(
                config.concurrency_limit is not None and config.concurrency_limit <= self.settings.busy_concurrency_reserve
                for config in configs):
            raise ValueError("Discovered concurrency limit must exceed busy_concurrency_reserve")
        with self.lock:
            if self.stop.is_set():
                raise RuntimeError("EndpointMonitor is closing; refresh was cancelled")
            write_json(self.raw_path, redact_credentials(raw))
            if self.states is None:
                self.states = EndpointStateManager(configs, self.settings)
            active = {config.endpoint_id for config in configs}
            for config in configs:
                if config.endpoint_id not in self.states.states:
                    self.states.states[config.endpoint_id] = EndpointState(config)
                state = self.states.states[config.endpoint_id]
                state.config = config
                if not self.catalog.get(config.endpoint_id, {}).get("catalog_available", True):
                    state.healthy = self._removed_health.pop(config.endpoint_id, True)
                self.states.configs[config.endpoint_id] = config
            for endpoint_id, row in self.catalog.items():
                if endpoint_id not in active:
                    if row["catalog_available"]:
                        self._removed_health[endpoint_id] = self.states.states[endpoint_id].healthy
                    row["catalog_available"] = False
                    self.states.states[endpoint_id].healthy = False
            self.catalog.update({row["endpoint_id"]: row for row in rows})
            self.states.epoch += 1
            self.fetched_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            self.publish()

    def publish(self):
        with self.lock:
            self._require_running()
            runtime = {row["endpoint_id"]: row for row in self.states.export(self.now_ms())}
            history = self.states.history.snapshot(self.catalog, self.states.now_ms)
            value = {"schema_version": 1, "source": "/model/info", "state_scope": "this_process_requests",
                     "clock": "unix_milliseconds", "remote_global_load_known": False, "catalog_fetched_at": self.fetched_at,
                     "updated_at": datetime.now(timezone.utc).isoformat(), "update_interval_seconds": self.interval,
                     "refresh_pending": bool(self._refresh_pending), "refresh_phase": self._refresh_phase,
                     "last_refresh_error": self.last_error, "uncertain_requests": self.uncertain_requests,
                     "endpoints": [{**row, "runtime": runtime[endpoint_id], "history": asdict(history[endpoint_id])}
                                   for endpoint_id, row in self.catalog.items()]}
            write_json(self.state_path, value)

    def _maintain(self):
        while not self.stop.wait(self.interval):
            try:
                self.refresh()
            except Exception as error:
                with self.lock:
                    if self.stop.is_set():
                        break
                    # Preserve the last valid catalog and expire the local rolling window.
                    self.last_error = type(error).__name__
                    try:
                        self.publish()
                    except OSError:
                        pass

    def __enter__(self):
        if self.states is not None:
            raise RuntimeError("Create one EndpointMonitor per session")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        self._running = True
        try:
            self.refresh()  # Mandatory success before any scheduling or paid request.
            if self.periodic:
                self.thread = Thread(target=self._maintain, name="endpoint-state-maintenance", daemon=True)
                self.thread.start()
        except BaseException:
            self.stop.set()
            if self.thread is not None and self.thread.is_alive():
                self.thread.join()
            self._running = False
            self._lock_path.unlink()
            raise
        return self

    def __exit__(self, *exception):
        try:
            with self._condition:
                self.stop.set()
                self._condition.notify_all()
                # Cancel waiting admissions/refreshes, but finish accepted sends.
                self._condition.wait_for(lambda: not self._has_active_requests() and not self._refresh_pending)
            if self.thread is not None:
                self.thread.join()
            self.publish()
        finally:
            self._running = False
            self._lock_path.unlink(missing_ok=True)

    def capture_request(self, request):
        with self.lock:
            self._require_running()
            self.requests[request.request_id] = request

    def endpoint_configs(self, *, endpoint_groups=None):
        self._require_running()
        # Explicitly consume the filtered file, never the large raw response.
        configurations = load_state_configs(self.state_path)
        if endpoint_groups is not None:
            configurations = tuple(config for config in configurations
                                   if set(endpoint_groups).intersection(self.catalog[config.endpoint_id]["tags"]))
            if not configurations:
                raise ValueError("No discovered endpoint matches endpoint_group")
        return configurations

    def bind_payload(self, route, payload):
        with self.lock:
            self._require_running()
            endpoint_id = route["selected_endpoint_id"]
            row = self.catalog.get(endpoint_id)
            if row is None or not row["catalog_available"]:
                raise ValueError("Selected LiteLLM endpoint is no longer in the catalog")
            if route["target_model"] not in row["supported_models"]:
                raise ValueError("Selected deployment does not support target_model")
            original_metadata = payload.get("metadata", {})
            if not isinstance(original_metadata, dict):
                raise ValueError("metadata must be an object")
            groups = original_metadata.get("endpoint_group")
            if groups is not None and (not isinstance(groups, list) or not groups or
                    any(not isinstance(group, str) or not group.strip() for group in groups)):
                raise ValueError("endpoint_group must be a nonempty list of strings")
            if groups is not None and not set(groups).intersection(row["tags"]):
                raise ValueError("Explicit endpoint_group does not match the selected deployment tags")
            # The deployed policy shell looks up public model names, then filters
            # by endpoint_group. Its model lookup does not accept deployment IDs.
            candidates = [item for item in self.catalog.values() if item["catalog_available"]
                          and route["target_model"] in item["supported_models"]]
            unique_tags = [tag for tag in row["tags"] if (groups is None or tag in groups)
                           and sum(tag in item["tags"] for item in candidates) == 1]
            metadata = dict(original_metadata)
            if unique_tags:
                metadata["endpoint_group"] = [unique_tags[0]]
            elif len(candidates) != 1 or groups is not None:
                raise ValueError("Selected deployment needs a unique endpoint_group tag for this target_model; not sent")
            bound = {**payload, "model": route["target_model"]}
            if metadata:
                bound["metadata"] = metadata
            else:
                bound.pop("metadata", None)
            return bound

    def route_adapter(self):
        """Record the same public-model/group binding used for real sends."""
        def resolve(endpoint, request):
            return self.bind_payload({"selected_endpoint_id": endpoint.endpoint_id,
                                      "target_model": request.target_model},
                                     {"metadata": request.metadata})
        return LiteLLMAdapter(endpoint_resolver=resolve)

    def send(self, route, payload, *, on_response_chunk=None):
        request_id, endpoint_id = route["request_id"], route["selected_endpoint_id"]
        with self.lock:
            self._wait_for_admission()
            payload = self.bind_payload(route, payload)
            if request_id not in self.requests:
                raise ValueError("Real execution requires the captured SchedulerRequest")
            original = self.requests[request_id]
            now = self.now_ms()
            request = replace(original, arrival_time=now, max_tokens=payload.get("max_tokens"), stream=payload.get("stream", False))
            decision = RouteDecision(request, endpoint_id, now, (endpoint_id,))
            try:
                self.states.reserve(decision)
            except RuntimeError as error:
                raise ValueError("Selected endpoint is unavailable or at its observed capacity; not sent") from error
            try:
                self.publish()
            except BaseException:
                self._complete(ExecutionResult(request_id, endpoint_id, False, now, self.now_ms(), failure_kind="request"))
                raise
        try:
            response = self.session.send(payload, request_id=request_id, selected_endpoint_id=endpoint_id,
                                         on_response_chunk=on_response_chunk)
            with self.lock:
                reported_id = response.get("x-litellm-model-id")
                if reported_id is not None and reported_id != endpoint_id:
                    self.uncertain_requests.append({"request_id": request_id, "endpoint_id": endpoint_id,
                                                    "reported_endpoint_id": reported_id})
                    raise ValueError("LiteLLM reported a different deployment; stopped without retry")
                success = 200 <= response["http_status"] < 300
                failure = None if success else ("rate_limit" if response["http_status"] == 429 else
                            "endpoint" if response["http_status"] >= 500 else "request")
                actual_input, actual_output = response_usage(response)
                self._complete(ExecutionResult(request_id, endpoint_id, success, now, self.now_ms(),
                    actual_input_tokens=actual_input, actual_output_tokens=actual_output, failure_kind=failure))
                self.publish()
        except BaseException:
            with self.lock:
                # Malformed responses/callback failures must also drain admission.
                if request_id in self.states.inflight:
                    self._complete(ExecutionResult(request_id, endpoint_id, False, now, self.now_ms(), failure_kind="endpoint"))
                    if not any(item["request_id"] == request_id for item in self.uncertain_requests):
                        self.uncertain_requests.append({"request_id": request_id, "endpoint_id": endpoint_id})
                    self.publish()
            raise
        return response
