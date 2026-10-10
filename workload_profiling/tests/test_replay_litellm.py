"""Real replay + fake HTTP: startup auth, dispatch order and failure cleanup."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import replace
from io import StringIO
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..integrations import litellm_gateway as gateway
from ..integrations import replay_litellm as bridge
from ..simulation.cli import execute
from .support import config, endpoint, temporary_directory
from .test_litellm_gateway import Response


class ReplayLiteLLMTests(unittest.TestCase):
    def fixtures(self, directory, *, count=5):
        source, output = directory / "source.jsonl", directory / "routes.jsonl"
        rows = [{"request_id": str(i), "input_tokens": 1, "output_tokens": 1,
                 "priority_level": level, "messages": [{"role": "user", "content": f"question-{i}"}]}
                for i, level in enumerate((2, 1, 3, 2, 4)[:count])]
        source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        settings = config(batch_size=3, batch_wait_ms=1000, batch_order="priority_then_light",
                          endpoints=(endpoint(concurrency=1, latency=99, tpm=1000),))
        return source, output, settings, rows

    @contextmanager
    def network(self, settings, *, statuses=(), failure_at=None, execute_override=None):
        trace, calls = [], []
        @contextmanager
        def tunnel(*args, **kwargs):
            trace.append("ssh-open")
            try:
                yield 1234
            finally:
                trace.append("ssh-close")
        def key(*args):
            trace.append("api-key")
            return "test-secret"
        def load(*args):
            self.assertEqual(trace, ["ssh-open", "api-key"])
            trace.append("config")
            return settings
        def replay(*args, **kwargs):
            trace.append("replay")
            if execute_override is not None:
                return execute_override(*args, **kwargs)
            return execute(*args, **kwargs)
        def opened(request, **kwargs):
            self.assertIn("replay", trace)
            trace.append("post")
            calls.append(request)
            if len(calls) == failure_at:
                raise TimeoutError("response unknown")
            status = statuses[len(calls) - 1] if len(calls) <= len(statuses) else 200
            return Response(b'{"choices":[{"message":{"content":"answer"}}]}', status=status)
        with patch.object(gateway, "ssh_tunnel", side_effect=tunnel) as ssh, \
                patch.object(gateway.getpass, "getpass", side_effect=key) as prompt, \
                patch.dict("os.environ", {}, clear=True), \
                patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)), \
                patch.object(bridge, "load_config", side_effect=load), \
                patch.object(bridge, "execute", side_effect=replay), redirect_stdout(StringIO()):
            yield SimpleNamespace(trace=trace, calls=calls, ssh=ssh, prompt=prompt)

    def run_bridge(self, source, output, **overrides):
        options = {"log_file": output.parent / "run_log.jsonl"} | overrides
        return bridge.replay_and_send(ssh_target="ubuntu@example.com",
            source=source, source_format="lengths", output=output, **options)

    def test_three_or_n_requests_login_before_replay_and_follow_dispatch_order(self):
        for count, expected in ((3, ["0", "2", "1"]), (5, ["0", "4", "2", "3", "1"])):
            with self.subTest(count=count), temporary_directory() as directory:
                source, output, settings, _ = self.fixtures(directory)
                baseline = execute(settings, source=source, source_format="lengths", limit=count,
                                   output=directory / "baseline.jsonl")
                responses = []
                with self.network(settings) as network:
                    result = self.run_bridge(source, output, limit=count,
                        on_response=lambda route, response: responses.append((route["request_id"], response)))
                self.assertEqual(network.trace, ["ssh-open", "api-key", "config", "replay"] + ["post"] * count + ["ssh-close"])
                network.ssh.assert_called_once()
                network.prompt.assert_called_once()
                self.assertEqual([r[0] for r in responses], expected)
                self.assertEqual(result.simulation.requests, baseline.requests)
                self.assertEqual(result.simulation.events, baseline.events)
                self.assertEqual(result.sent_count, count)
                self.assertEqual(result.prepared_count, count)
                self.assertTrue(all(json.loads(call.data)["model"] == "deepseek-flash" for call in network.calls))
                self.assertEqual([json.loads(call.data)["messages"][0]["content"] for call in network.calls],
                                 [f"question-{request_id}" for request_id in expected])
                self.assertTrue(all("metadata" not in json.loads(call.data) for call in network.calls))
                self.assertEqual(output.read_bytes(), (directory / "baseline.jsonl").read_bytes())

    def test_dry_run_uses_real_replay_without_ssh_or_api_key_prompt(self):
        with temporary_directory() as directory:
            source, output, settings, _ = self.fixtures(directory)
            with patch.object(bridge, "load_config", return_value=settings), \
                    patch.object(bridge, "LiteLLMSession") as session, \
                    patch.object(gateway.getpass, "getpass") as prompt, redirect_stdout(StringIO()):
                result = self.run_bridge(source, output, dry_run=True)
            session.assert_not_called()
            prompt.assert_not_called()
            self.assertEqual(result.prepared_count, 3)
            self.assertEqual(result.sent_count, 0)
            self.assertEqual(len(list(gateway.iter_routes(output))), 3)

    def test_http_error_or_timeout_stops_after_two_calls_and_preserves_routes(self):
        for mode in ("http", "timeout"):
            with self.subTest(mode=mode), temporary_directory() as directory:
                source, output, settings, _ = self.fixtures(directory)
                responses = []
                with self.network(settings, statuses=(200, 429), failure_at=2 if mode == "timeout" else None) as network:
                    with self.assertRaises(bridge.DeliveryError) as error:
                        self.run_bridge(source, output,
                            on_response=lambda route, response: responses.append(response))
                self.assertEqual(len(network.calls), 2)
                self.assertEqual(network.trace[-1], "ssh-close")
                self.assertEqual(error.exception.request_id, "2")
                self.assertEqual(error.exception.successful_count, 1)
                self.assertEqual(error.exception.pending_count, 1)
                self.assertEqual(len(list(gateway.iter_routes(output))), 3)
                self.assertEqual(len(responses), 1 if mode == "timeout" else 2)

    def test_invalid_later_payload_is_caught_before_any_post(self):
        with temporary_directory() as directory:
            source, output, settings, rows = self.fixtures(directory, count=3)
            rows[-1]["messages"] = []
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with self.network(settings) as network:
                with self.assertRaisesRegex(ValueError, "messages"):
                    self.run_bridge(source, output)
            self.assertEqual(network.calls, [])
            self.assertEqual(network.trace[-1], "ssh-close")
            self.assertEqual(len(list(gateway.iter_routes(output))), 3)

    def test_bad_source_preserves_previous_output_and_does_not_send_stale_routes(self):
        with temporary_directory() as directory:
            source, output, settings, _ = self.fixtures(directory)
            source.write_text("invalid json", encoding="utf-8")
            output.write_text("previous output", encoding="utf-8")
            with self.network(settings) as network:
                with self.assertRaises(ValueError):
                    self.run_bridge(source, output)
            self.assertEqual(network.calls, [])
            self.assertEqual(network.trace[-1], "ssh-close")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output")

    def test_rejected_requests_are_never_sent(self):
        with temporary_directory() as directory:
            source, output, settings, rows = self.fixtures(directory, count=3)
            rows[1]["input_tokens"] = 1001
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with self.network(settings) as network:
                result = self.run_bridge(source, output)
            self.assertEqual(result.simulation.summary["rejected_requests"], 1)
            self.assertEqual(result.sent_count, 2)
            self.assertEqual(len(network.calls), 2)

    def test_startup_login_failure_prevents_data_or_config_loading(self):
        with temporary_directory() as directory, patch.object(bridge, "LiteLLMSession") as session, \
                patch.object(bridge, "load_config") as load, patch.object(bridge, "execute") as replay, \
                redirect_stdout(StringIO()):
            session.return_value.__enter__.side_effect = OSError("login failed")
            with self.assertRaisesRegex(OSError, "login failed"):
                bridge.replay_and_send(ssh_target="ubuntu@example.com", log_file=directory / "run_log.jsonl")
            load.assert_not_called()
            replay.assert_not_called()

    def test_interrupt_during_replay_closes_startup_session(self):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
        with temporary_directory() as directory:
            source, output, settings, _ = self.fixtures(directory)
            with self.network(settings, execute_override=interrupted) as network:
                with self.assertRaises(KeyboardInterrupt):
                    self.run_bridge(source, output)
            self.assertEqual(network.calls, [])
            self.assertEqual(network.trace[-1], "ssh-close")

    def test_cli_defaults_to_three_and_passes_delivery_controls(self):
        simulated = SimpleNamespace(summary={"rejected_requests": 0})
        result = bridge.ReplayDeliveryResult(simulated, 3, 3, False)
        argv = ["replay-send", "--ssh-target", "ubuntu@example.com",
                "--endpoint-group", "official", "--no-max-tokens"]
        with patch.object(sys, "argv", argv), patch.object(bridge, "replay_and_send", return_value=result) as run, \
                redirect_stdout(StringIO()):
            bridge.main()
        self.assertEqual(run.call_args.kwargs["limit"], 3)
        self.assertEqual(run.call_args.kwargs["endpoint_groups"], ["official"])
        self.assertTrue(run.call_args.kwargs["no_max_tokens"])
        self.assertNotIn("model", run.call_args.kwargs)

    def test_invalid_limit_and_missing_target_do_not_start_session(self):
        with patch.object(bridge, "LiteLLMSession") as session:
            for options in ({"limit": 0, "ssh_target": "ubuntu@example.com"}, {"limit": 3}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    bridge.replay_and_send(**options)
            session.assert_not_called()

    def test_unified_command_dispatches_to_integrated_entry(self):
        from .. import __main__ as entry
        with patch.object(sys, "argv", ["workload_profiling", "replay-send", "--limit", "5"]), \
                patch.object(bridge, "main") as main:
            entry.main()
            main.assert_called_once_with()

    def test_mixed_request_models_route_to_compatible_endpoints_and_are_sent_individually(self):
        with temporary_directory() as directory:
            source, output, _, rows = self.fixtures(directory, count=3)
            rows[0]["target_model"] = "other-model"
            rows[1]["target_model"] = "deepseek-flash"
            # Third request intentionally lacks a model: use simulation default.
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            settings = config(endpoints=(endpoint("flash"), replace(endpoint("other"), supported_models=("other-model",))))
            with self.network(settings) as network:
                result = self.run_bridge(source, output)
            routes = list(gateway.iter_routes(output))
            self.assertEqual({r["request_id"]: (r["target_model"], r["selected_endpoint_id"]) for r in routes},
                             {"0": ("other-model", "other"), "1": ("deepseek-flash", "flash"),
                              "2": ("deepseek-flash", "flash")})
            self.assertEqual([json.loads(call.data)["model"] for call in network.calls],
                             [route["target_model"] for route in routes])
            self.assertEqual(result.sent_count, 3)
            network.ssh.assert_called_once()

    def test_unsupported_request_model_is_rejected_without_silent_default_substitution(self):
        with temporary_directory() as directory:
            source, output, settings, rows = self.fixtures(directory, count=3)
            rows[1]["target_model"] = "unsupported"
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with self.network(settings) as network:
                result = self.run_bridge(source, output)
            self.assertEqual(result.sent_count, 2)
            self.assertEqual(result.simulation.summary["rejected_requests"], 1)
            rejected = next(row for row in result.simulation.requests if row["request_id"] == "1")
            self.assertEqual(rejected["target_model"], "unsupported")
            self.assertTrue(all(json.loads(call.data)["model"] == "deepseek-flash" for call in network.calls))
