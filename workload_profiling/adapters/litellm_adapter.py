"""Chat-completion parameter conversion and injected synchronous execution.

No SDK import or network call occurs on import. force_endpoint is gateway
metadata; api_base/deployment_model or an explicit resolver must bind execution
to the selected endpoint. Credentials can be supplied by the resolver or SDK
environment; they are never written to scheduler configuration.
"""
from copy import deepcopy
import time

from ..core.request import ExecutionResult


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _usage(response):
    usage = _get(response, "usage")
    return _get(usage, "prompt_tokens"), _get(usage, "completion_tokens")


def _has_output(chunk):
    for choice in _get(chunk, "choices", ()) or ():
        delta = _get(choice, "delta")
        if _get(delta, "content") or _get(delta, "tool_calls") or _get(delta, "function_call"):
            return True
    return False


def classify_error(error):
    status = _get(error, "status_code")
    if status in (400, 404, 422):
        return "request"
    if status == 429:
        return "rate_limit"
    if isinstance(status, int) and status >= 500:
        return "endpoint"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "endpoint"
    return None


class LiteLLMAdapter:
    def __init__(self, endpoint_resolver=None, *, error_classifier=classify_error):
        self.endpoint_resolver = endpoint_resolver
        self.error_classifier = error_classifier

    def build_params(self, decision, endpoint):
        if endpoint.endpoint_id != decision.selected_endpoint_id:
            raise ValueError("Execution endpoint does not match route decision")
        request = decision.request
        if request.api_type != "chat":
            raise ValueError("LiteLLMAdapter currently supports chat completion only")
        params = {}
        if self.endpoint_resolver is not None:
            resolved = self.endpoint_resolver(endpoint, request)
            if not isinstance(resolved, dict) or not resolved:
                raise ValueError("Endpoint resolver must return a nonempty deployment parameter mapping")
            params.update(deepcopy(resolved))
        params.setdefault("model", endpoint.deployment_model or request.target_model)
        if endpoint.api_base is not None:
            params.setdefault("api_base", endpoint.api_base)
        params.update(messages=deepcopy(list(request.messages)), stream=request.stream)
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        metadata = deepcopy(request.metadata)
        metadata.update(deepcopy(decision.metadata))
        metadata["force_endpoint"] = decision.selected_endpoint_id
        params["metadata"] = metadata
        for name in ("temperature", "top_p", "tools", "tool_choice", "parallel_tool_calls",
                     "response_format", "stop", "seed", "user"):
            if name in metadata:
                params[name] = deepcopy(metadata[name])
        if request.stream:
            params.setdefault("stream_options", {"include_usage": True})
        return params

    def execute(self, decision, endpoint, completion, *, clock=time.perf_counter, on_chunk=None):
        """Return feedback; caller passes it to Scheduler.accept_feedback.

        completion may be litellm.completion or a gateway callable. The callable
        must honor the resolver's endpoint binding. Streaming output is consumed
        here and may be forwarded through on_chunk. Unknown usage/timing stays null.
        """
        if not (endpoint.api_base or endpoint.deployment_model or self.endpoint_resolver):
            raise ValueError("Execution requires api_base, a unique deployment_model or an endpoint resolver")
        params = self.build_params(decision, endpoint)
        started = clock()
        actual_input = actual_output = None
        first_output = last_output = None
        output_chunks = 0
        failure_kind = None
        success = False
        callback_error = False
        try:
            response = completion(**params)
            if decision.request.stream:
                for chunk in response:
                    elapsed = max(0, (clock() - started) * 1000)
                    prompt, output = _usage(chunk)
                    if prompt is not None:
                        actual_input = prompt
                    if output is not None:
                        actual_output = output
                    if _has_output(chunk):
                        if first_output is None:
                            first_output = elapsed
                        last_output = elapsed
                        output_chunks += 1
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk)
                        except Exception:
                            callback_error = True
                            raise
            else:
                actual_input, actual_output = _usage(response)
            success = True
        except Exception as error:
            failure_kind = "request" if callback_error else self.error_classifier(error)
        elapsed = max(0, (clock() - started) * 1000)
        finished = decision.dispatched_at_ms + round(elapsed)
        tpot = ((last_output - first_output) / (actual_output - 1)
                if output_chunks >= 2 and actual_output is not None and actual_output > 1 else None)
        return ExecutionResult(
            decision.request.request_id, decision.selected_endpoint_id, success,
            decision.dispatched_at_ms, finished, actual_input, actual_output,
            first_output, tpot, failure_kind,
            e2e_ms=finished - decision.request.arrival_time)
