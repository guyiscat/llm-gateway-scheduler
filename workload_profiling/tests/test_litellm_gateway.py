"""External bridge contract and persistent sessions, without paid calls."""
from copy import deepcopy
from contextlib import nullcontext, redirect_stdout
from io import BytesIO, StringIO
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ..integrations.litellm_gateway import build_payload, read_route, send_request, NoRedirect, LiteLLMSession
from ..integrations import litellm_gateway as gateway
from .support import temporary_directory


class Response:
    code = 200
    headers = {"X-Litellm-Model-Api-Base": "https://api.example/v1", "X-Other": "ignored"}

    def __init__(self, body=b"", *, headers=None, status=200):
        self.body = BytesIO(body)
        self.code = status
        self.headers = type(self).headers | (headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body.read(size)


class LiteLLMGatewayTests(unittest.TestCase):
    def route(self):
        return {"request_id": "r", "selected_endpoint_id": "endpoint_a", "dispatched_at_ms": 20,
                "litellm_params": {"model": "deepseek-flash", "api_base": "https://local-binding.invalid",
                    "messages": [{"role": "user", "content": "recorded question"}], "stream": False,
                    "temperature": .2, "metadata": {"force_endpoint": "endpoint_a", "user_id": "u"}}}

    def payload(self, **changes):
        return build_payload(self.route(), endpoint_groups=["official"], **changes)

    def test_explicit_group_mapping_preserves_messages_without_changing_route(self):
        route = self.route(); original = deepcopy(route)
        payload = build_payload(route, endpoint_groups=["official"])
        self.assertEqual(route, original)
        self.assertEqual(payload["messages"], original["litellm_params"]["messages"])
        self.assertEqual(payload["temperature"], .2)
        self.assertEqual(payload["metadata"], {"user_id": "u", "endpoint_group": ["official"]})
        self.assertNotIn("api_base", payload)
        self.assertNotIn("max_tokens", payload)

    def test_smoke_uses_only_hi_and_remote_binding(self):
        payload = self.payload(smoke=True)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(payload["metadata"], {"endpoint_group": ["official"]})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["num_retries"], 0)
        self.assertEqual(payload["fallbacks"], [])

    def test_unbound_empty_and_invalid_requests_fail_locally(self):
        route = self.route(); route["litellm_params"]["model"] = "default"
        with self.assertRaises(ValueError):
            build_payload(route, endpoint_groups=["official"])
        route = self.route(); route["litellm_params"]["messages"] = []
        with self.assertRaises(ValueError):
            build_payload(route, endpoint_groups=["official"])
        for limit in (0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.payload(max_tokens=limit)

    def test_recorded_output_limit_is_preserved_overridden_or_omitted(self):
        route = self.route()
        route["litellm_params"]["max_tokens"] = 200
        original = deepcopy(route)
        options = dict(endpoint_groups=["official"])
        self.assertEqual(build_payload(route, **options)["max_tokens"], 200)
        self.assertEqual(build_payload(route, max_tokens=100, **options)["max_tokens"], 100)
        self.assertNotIn("max_tokens", build_payload(route, no_max_tokens=True, **options))
        self.assertNotIn("max_tokens", build_payload(route, smoke=True, **options))
        self.assertEqual(build_payload(route, smoke=True, max_tokens=1, **options)["max_tokens"], 1)
        self.assertEqual(route, original)
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            build_payload(route, max_tokens=100, no_max_tokens=True, **options)

    def test_null_output_limit_is_omitted_and_invalid_recorded_limits_rejected(self):
        route = self.route()
        options = dict(endpoint_groups=["official"])
        route["litellm_params"]["max_tokens"] = None
        self.assertNotIn("max_tokens", build_payload(route, **options))
        for limit in (0, -1, True, "100"):
            route["litellm_params"]["max_tokens"] = limit
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                build_payload(route, **options)

    def test_single_record_selection_never_loads_or_sends_whole_replay(self):
        with temporary_directory() as directory:
            path = directory / "routes.jsonl"
            first = self.route(); second = deepcopy(first); second["request_id"] = "second"
            path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\ninvalid", encoding="utf-8")
            self.assertEqual(read_route(path), first)
            self.assertEqual(read_route(path, "second"), second)

    def test_repeated_explicit_calls_are_allowed_and_old_guard_is_ignored(self):
        calls = []
        def opened(request, **kwargs):
            calls.append(request)
            return Response()
        with temporary_directory() as directory:
            guard = directory / "litellm_send_once.json"
            guard.write_text('{"status":"attempted"}', encoding="utf-8")
            with patch.object(gateway, "CACHE", directory):
                result = send_request(self.payload(max_tokens=1), "test-secret", port=1234,
                                      opener=SimpleNamespace(open=opened))
                send_request(self.payload(), "test-secret", port=1234, opener=SimpleNamespace(open=opened))
            self.assertEqual(result, {"http_status": 200, "x-litellm-model-api-base": "https://api.example/v1",
                                      "response_format": "text", "response": ""})
            self.assertEqual(calls[0].get_method(), "POST")
            self.assertEqual(calls[0].get_header("X-litellm-num-retries"), "0")
            self.assertEqual(json.loads(calls[0].data)["max_tokens"], 1)
            self.assertEqual(guard.read_text(encoding="utf-8"), '{"status":"attempted"}')
            self.assertEqual(list(directory.iterdir()), [guard])
            self.assertEqual(len(calls), 2)

    def test_ambiguous_timeout_is_never_retried(self):
        calls = []
        def opened(request, **kwargs):
            calls.append(request)
            raise TimeoutError("response unknown")
        with self.assertRaises(TimeoutError):
            send_request(self.payload(), "test", port=1234, opener=SimpleNamespace(open=opened))
        self.assertEqual(len(calls), 1)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, "redirect", {}, "http://elsewhere"))

    def test_cli_displays_full_json_response_without_recording_it(self):
        response = {"id": "chat-id", "choices": [{"message": {"role": "assistant", "content": "你好！",
                    "reasoning_content": "思考过程"}, "finish_reason": "stop"}], "usage": {"total_tokens": 20}}
        opened = []
        def open_response(request, **kwargs):
            opened.append(request)
            return Response(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        with temporary_directory() as directory:
            output = StringIO()
            argv = ["gateway", "--endpoint-group", "official",
                    "--ssh-target", "ubuntu@example.com", "--send", "--log-file", str(directory / "run_log.jsonl")]
            with patch.object(sys, "argv", argv), patch.object(gateway, "read_route", return_value=self.route()), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                    patch.dict("os.environ", {"KEY": "test-secret"}), patch.object(gateway, "build_opener",
                    return_value=SimpleNamespace(open=open_response)), redirect_stdout(output):
                gateway.main()
            result = json.loads(output.getvalue())
            self.assertEqual(result["response"], response)
            self.assertEqual(result["response_format"], "json")
            self.assertIn("你好！", output.getvalue())
            self.assertNotIn("test-secret", output.getvalue())
            self.assertEqual(len(opened), 1)

    def test_http_error_body_and_non_json_response_are_preserved(self):
        with temporary_directory() as directory:
            error = {"error": {"message": "unknown model", "type": "invalid_request_error"}}
            def failed(request, **kwargs):
                raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(json.dumps(error).encode()))
            result = send_request(self.payload(), "test", port=1234,
                               opener=SimpleNamespace(open=failed))
            self.assertEqual(result["http_status"], 400)
            self.assertEqual(result["response"], error)
            result = send_request(self.payload(), "test", port=1234,
                               opener=SimpleNamespace(open=lambda *a, **kw: Response(b"upstream unavailable", status=502)))
            self.assertEqual(result["response_format"], "text")
            self.assertEqual(result["response"], "upstream unavailable")

    def test_sse_is_displayed_incrementally_with_split_unicode_and_usage(self):
        transcript = 'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n' \
                     'data: {"choices":[],"usage":{"total_tokens":5}}\n\ndata: [DONE]\n\n'
        encoded = transcript.encode("utf-8")
        boundary = encoded.index("你".encode("utf-8")) + 1
        chunks = [encoded[:boundary], encoded[boundary:], b""]
        received = []
        class Stream(Response):
            def read(self, size):
                raise AssertionError("SSE should use read1 for live display")
            def read1(self, size):
                if len(chunks) == 1:
                    self_test.assertEqual("".join(received), transcript)
                return chunks.pop(0)
        self_test = self
        with temporary_directory() as directory:
            result = send_request(self.payload(), "test", port=1234,
                opener=SimpleNamespace(open=lambda *a, **kw: Stream(headers={"Content-Type": "text/event-stream"})),
                on_response_chunk=received.append)
            self.assertEqual(result["response_format"], "sse")
            self.assertEqual(result["response"], transcript)
            self.assertEqual("".join(received), transcript)

    def test_partial_sse_remains_visible_without_retry(self):
        received = []
        class Interrupted(Response):
            def read1(self, size):
                if not received:
                    return b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise TimeoutError("stream interrupted")
        with temporary_directory() as directory:
            with self.assertRaises(TimeoutError):
                send_request(self.payload(), "test", port=1234,
                    opener=SimpleNamespace(open=lambda *a, **kw: Interrupted(headers={"Content-Type": "text/event-stream"})),
                    on_response_chunk=received.append)
            self.assertIn("partial", "".join(received))

    def test_groups_are_optional_and_explicit_or_recorded_groups_are_validated(self):
        route = self.route()
        payload = build_payload(route)
        self.assertEqual(payload["metadata"], {"user_id": "u"})
        self.assertNotIn("metadata", build_payload(route, smoke=True))
        route["litellm_params"]["metadata"] = {"force_endpoint": "endpoint_a"}
        self.assertNotIn("metadata", build_payload(route))
        route["litellm_params"]["metadata"]["endpoint_group"] = ["recorded"]
        self.assertEqual(build_payload(route)["metadata"]["endpoint_group"], ["recorded"])
        self.assertEqual(build_payload(route, endpoint_groups=["new", "second"])
                         ["metadata"]["endpoint_group"], ["new", "second"])
        for invalid in ([], "official", [""], [None], None):
            route["litellm_params"]["metadata"]["endpoint_group"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_payload(route)

    def test_session_authenticates_once_reuses_tunnel_and_clears_key_on_exit(self):
        from contextlib import contextmanager
        events, calls = [], []
        @contextmanager
        def tunnel(*args, **kwargs):
            events.append("open")
            try:
                yield 1234
            finally:
                events.append("close")
        def opened(request, **kwargs):
            calls.append(request)
            return Response(b'{"choices":[]}')
        session = LiteLLMSession("ubuntu@example.com")
        with self.assertRaisesRegex(RuntimeError, "not open"):
            session.send(self.payload())
        with patch.object(gateway, "ssh_tunnel", side_effect=tunnel) as ssh, \
                patch.dict("os.environ", {}, clear=True), \
                patch.object(gateway.getpass, "getpass", return_value="test-secret") as prompt, \
                patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)) as opener:
            with session:
                session.send(self.payload())
                session.send(self.payload())
                self.assertEqual(events, ["open"])
            ssh.assert_called_once()
            prompt.assert_called_once()
            opener.assert_called_once()
        self.assertEqual(events, ["open", "close"])
        self.assertEqual(len(calls), 2)
        self.assertIsNone(session._key)
        with self.assertRaisesRegex(RuntimeError, "not open"):
            session.send(self.payload())

    def test_session_cleanup_on_invalid_key_or_request_exception(self):
        for key in ("bad\nkey", "valid"):
            with self.subTest(key=key), patch.dict("os.environ", {"KEY": key}), \
                    patch.object(gateway, "ssh_tunnel") as tunnel:
                session = LiteLLMSession("ubuntu@example.com")
                tunnel.return_value.__enter__.return_value = 1234
                with self.assertRaises((ValueError, TimeoutError)):
                    with session:
                        with patch.object(gateway, "send_request", side_effect=TimeoutError("unknown")):
                            session.send(self.payload())
                tunnel.return_value.__exit__.assert_called_once()
                self.assertIsNone(session._key)

    def test_cli_session_selects_requests_without_reauth_and_continues_after_failures(self):
        with temporary_directory() as directory:
            path = directory / "routes.jsonl"
            first = self.route(); second = deepcopy(first); second["request_id"] = "second"
            path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
            output, errors, calls = StringIO(), StringIO(), []
            def opened(request, **kwargs):
                calls.append(request)
                if len(calls) == 1:
                    raise TimeoutError("unknown response")
                return Response(b'{"choices":[]}', status=400 if len(calls) == 2 else 200)
            argv = ["gateway", "--routes", str(path),
                    "--ssh-target", "ubuntu@example.com", "--send", "--session", "--log-file", str(directory / "run_log.jsonl")]
            with patch.object(sys, "argv", argv), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)) as tunnel, \
                    patch.dict("os.environ", {}, clear=True), \
                    patch.object(gateway.getpass, "getpass", return_value="test-secret") as prompt, \
                    patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)), \
                    patch("builtins.input", side_effect=["missing", "", "second", "second", "/quit"]), \
                    redirect_stdout(output), patch.object(sys, "stderr", errors):
                gateway.main()
            tunnel.assert_called_once()
            prompt.assert_called_once()
            self.assertEqual(len(calls), 3)
            self.assertIn("No matching route", errors.getvalue())
            self.assertIn("not retried", errors.getvalue())
            self.assertIn('"http_status": 400', output.getvalue())
            self.assertIn('"http_status": 200', output.getvalue())
            self.assertNotIn("test-secret", output.getvalue())
            self.assertTrue(all("endpoint_group" not in json.loads(call.data)["metadata"] for call in calls))

    def test_cli_preview_without_group_and_session_requires_send(self):
        argv = ["gateway", "--smoke"]
        output = StringIO()
        with patch.object(sys, "argv", argv), patch.object(gateway, "read_route", return_value=self.route()), \
                patch.object(gateway, "ssh_tunnel") as tunnel, redirect_stdout(output):
            gateway.main()
            tunnel.assert_not_called()
        self.assertNotIn("metadata", json.loads(output.getvalue())["payload"])
        with patch.object(sys, "argv", argv + ["--session"]), patch.object(sys, "stderr", StringIO()), \
                self.assertRaises(SystemExit) as error:
            gateway.main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_session_eof_closes_tunnel_without_sending(self):
        with temporary_directory() as directory:
            argv = ["gateway", "--ssh-target", "ubuntu@example.com", "--send", "--session", "--log-file", str(directory / "run_log.jsonl")]
            self.check_cli_session_eof(argv)

    def check_cli_session_eof(self, argv):
        with patch.object(sys, "argv", argv), patch.object(gateway, "read_route", return_value=self.route()), \
                patch.object(gateway, "ssh_tunnel") as tunnel, patch.dict("os.environ", {"KEY": "test"}), \
                patch.object(gateway, "send_request") as send, patch("builtins.input", side_effect=EOFError), \
                redirect_stdout(StringIO()):
            tunnel.return_value.__enter__.return_value = 1234
            gateway.main()
            send.assert_not_called()
            tunnel.return_value.__exit__.assert_called_once()

    def test_request_model_is_authoritative_and_legacy_valid_models_are_preserved(self):
        route = self.route()
        self.assertEqual(build_payload(route)["model"], "deepseek-flash")
        route["target_model"] = "another-model"
        original = deepcopy(route)
        self.assertEqual(build_payload(route)["model"], "another-model")
        self.assertEqual(build_payload(route, smoke=True)["model"], "another-model")
        self.assertEqual(route, original)
        for invalid in (None, "", " ", False, "default"):
            route["target_model"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "target_model"):
                build_payload(route)
