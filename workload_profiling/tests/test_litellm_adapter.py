"""Endpoint binding and real feedback formats tested without network calls."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from ..adapters.litellm_adapter import LiteLLMAdapter
from ..core import EndpointConfig, SchedulerRequest, RouteDecision, Scheduler, SchedulerConfig


class LiteLLMAdapterTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = EndpointConfig("a", 100, 10000, 4, api_base="https://endpoint.example/v1",
            deployment_model="openai/model")
        self.request = SchedulerRequest("r", "logical-model", 5, 10,
            messages=({"role": "user", "content": "hi"},), max_tokens=100,
            metadata={"force_endpoint": "wrong", "temperature": .2})
        self.decision = RouteDecision(self.request, "a", 20, ("a",))

    def test_build_params_preserves_payload_and_binds_selected_deployment(self):
        params = LiteLLMAdapter().build_params(replace(self.decision, metadata={"route_tag": "baseline"}), self.endpoint)
        self.assertEqual(params["model"], "openai/model")
        self.assertEqual(params["api_base"], "https://endpoint.example/v1")
        self.assertEqual(params["metadata"]["force_endpoint"], "a")
        self.assertEqual(params["max_tokens"], 100)
        self.assertEqual(params["temperature"], .2)
        self.assertEqual(params["metadata"]["route_tag"], "baseline")
        params["messages"][0]["content"] = "changed"
        self.assertEqual(self.request.messages[0]["content"], "hi")
        self.assertEqual(self.request.metadata["force_endpoint"], "wrong")

    def test_metadata_alone_never_performs_an_unbound_call(self):
        unbound = replace(self.endpoint, api_base=None, deployment_model=None)
        called = []
        with self.assertRaises(ValueError):
            LiteLLMAdapter().execute(self.decision, unbound, lambda **kw: called.append(kw))
        self.assertFalse(called)
        params = LiteLLMAdapter().build_params(self.decision, unbound)
        self.assertEqual(params["model"], "logical-model")

    def test_explicit_resolver_for_gateway_endpoint_selection(self):
        endpoint = replace(self.endpoint, api_base=None, deployment_model=None)
        adapter = LiteLLMAdapter(lambda ep, req: {"model": "endpoint-a-alias", "api_key": "test"})
        params = adapter.build_params(self.decision, endpoint)
        self.assertEqual(params["model"], "endpoint-a-alias")
        self.assertEqual(params["metadata"]["force_endpoint"], "a")
        with self.assertRaises(ValueError):
            LiteLLMAdapter(lambda ep, req: {}).build_params(self.decision, endpoint)

    def test_nonstream_execution_measures_e2e_and_actual_usage(self):
        times = iter((1, 1.05))
        result = LiteLLMAdapter().execute(self.decision, self.endpoint,
            lambda **kw: SimpleNamespace(usage=SimpleNamespace(prompt_tokens=6, completion_tokens=7)),
            clock=lambda: next(times))
        self.assertTrue(result.success)
        self.assertEqual((result.started_at_ms, result.finished_at_ms, result.e2e_ms), (20, 70, 70))
        self.assertEqual((result.actual_input_tokens, result.actual_output_tokens), (6, 7))
        self.assertIsNone(result.ttft_ms)

    def test_stream_ttft_ignores_role_chunk_and_tpot_uses_known_token_count(self):
        decision = replace(self.decision, request=replace(self.request, stream=True))
        chunks = [{"choices": [{"delta": {"role": "assistant"}}]},
                  {"choices": [{"delta": {"content": "hello"}}]},
                  {"choices": [{"delta": {"content": "world"}}]},
                  {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}]
        times = iter((0, .01, .02, .04, .05, .06))
        forwarded = []
        result = LiteLLMAdapter().execute(decision, self.endpoint, lambda **kw: iter(chunks),
            clock=lambda: next(times), on_chunk=forwarded.append)
        self.assertTrue(result.success)
        self.assertEqual(forwarded, chunks)
        self.assertEqual(result.ttft_ms, 20)
        self.assertEqual(result.tpot_ms, 10)
        self.assertEqual(result.actual_output_tokens, 3)
        self.assertEqual(LiteLLMAdapter().build_params(decision, self.endpoint)["stream_options"], {"include_usage": True})

    def test_stream_missing_usage_does_not_invent_tokens_or_tpot(self):
        decision = replace(self.decision, request=replace(self.request, stream=True))
        times = iter((0, .01, .02))
        result = LiteLLMAdapter().execute(decision, self.endpoint,
            lambda **kw: iter([{"choices": [{"delta": {"tool_calls": [{"id": "call"}]}}]}]),
            clock=lambda: next(times))
        self.assertEqual(result.ttft_ms, 10)
        self.assertIsNone(result.tpot_ms)
        self.assertIsNone(result.actual_output_tokens)

    def test_error_feedback_distinguishes_request_rate_limit_and_endpoint(self):
        class ApiError(Exception):
            def __init__(self, code):
                self.status_code = code
        for error, kind in ((ApiError(400), "request"), (ApiError(422), "request"),
                            (ApiError(429), "rate_limit"), (ApiError(503), "endpoint"),
                            (TimeoutError(), "endpoint"), (ValueError(), None)):
            with self.subTest(kind=kind):
                def fail(**kw):
                    raise error
                times = iter((0, .01))
                result = LiteLLMAdapter().execute(self.decision, self.endpoint, fail, clock=lambda: next(times))
                self.assertFalse(result.success)
                self.assertEqual(result.failure_kind, kind)

    def test_adapter_feedback_updates_same_scheduler_state(self):
        endpoint = replace(self.endpoint, supported_models=("logical-model",))
        scheduler = Scheduler((endpoint,), SchedulerConfig())
        scheduler.submit_request(self.request)
        decision = scheduler.take_decisions()[0]
        times = iter((0, .01))
        result = LiteLLMAdapter().execute(decision, endpoint,
            lambda **kw: {"usage": {"prompt_tokens": 5, "completion_tokens": 2}}, clock=lambda: next(times))
        scheduler.accept_feedback(result)
        self.assertEqual(scheduler.states.states["a"].concurrency, 0)
        self.assertEqual(scheduler.states.states["a"].tokens_in_window, 7)
        self.assertEqual(scheduler.records["r"].status, "completed")

    def test_wrong_endpoint_and_unsupported_interface_fail_before_call(self):
        with self.assertRaises(ValueError):
            LiteLLMAdapter().build_params(self.decision, replace(self.endpoint, endpoint_id="wrong"))
        with self.assertRaises(ValueError):
            LiteLLMAdapter().build_params(replace(self.decision, request=replace(self.request, api_type="embedding")), self.endpoint)
