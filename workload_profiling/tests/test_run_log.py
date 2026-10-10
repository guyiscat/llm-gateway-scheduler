"""Request/response tracing, incremental durability and redaction without network."""
from contextlib import nullcontext, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ..common.run_log import RunLog
from ..core.request import SchedulerRequest
from ..integrations import litellm_gateway as gateway
from ..integrations import replay_litellm as bridge
from ..simulation import SimulationRunner, WorkloadRequest
from ..simulation.cli import execute
from .support import config, endpoint, temporary_directory
from .test_litellm_gateway import Response


def read_log(path):
    # Reassemble a run only for tests that check the full event order.
    directory = path.with_suffix("") if path.suffix == ".jsonl" else path
    rows = [json.loads(line) for file in directory.rglob("*.jsonl")
            for line in file.read_text(encoding="utf-8").splitlines()]
    return sorted(rows, key=lambda row: row["sequence"])


class RunLogTests(unittest.TestCase):
    def test_complete_scheduler_request_is_captured_without_raw_recorded_answer(self):
        request = SchedulerRequest("r", "deepseek-flash", 11, 22, priority=1, arrival_time=4,
            messages=({"role": "user", "content": "问题"},), max_tokens=99, stream=True,
            slo={"ttft_ms": 100}, metadata={"temperature": .2})
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            with RunLog(path) as log:
                log.scheduler_request(request)
                # Readable immediately, before context close.
                row = read_log(path)[1]
                expected = json.loads(json.dumps(asdict(request)))
                self.assertEqual(row["scheduler_request"], expected)
                self.assertNotIn("output_tokens", row["scheduler_request"])
                self.assertEqual(row["request_id"], "r")
            rows = read_log(path)
            self.assertEqual({row["run_id"] for row in rows}, {log.run_id})
            self.assertEqual(rows[-1]["status"], "completed")

    def test_request_hook_captures_rejected_requests_before_routing_and_preserves_behavior(self):
        seen, dispatched = [], []
        settings = config(endpoints=(endpoint(tpm=15),))
        requests = [WorkloadRequest("ok", 1, 1), WorkloadRequest("rejected", 20, 1)]
        baseline = SimulationRunner(settings).run(requests)
        def routed(decision):
            self.assertIn(decision.request, seen)
            dispatched.append(decision.request.request_id)
        result = SimulationRunner(settings, on_request=seen.append, on_route=routed).run(requests)
        self.assertEqual([request.request_id for request in seen], ["ok", "rejected"])
        self.assertEqual(dispatched, ["ok"])
        self.assertEqual(result.requests, baseline.requests)
        self.assertEqual(result.events, baseline.events)

    def test_async_hook_rejected_and_hook_failure_preserves_route_output(self):
        async def asynchronous(request):
            pass
        with self.assertRaises(TypeError):
            SimulationRunner(config(), on_request=asynchronous)
        with self.assertRaisesRegex(TypeError, "awaitable"):
            SimulationRunner(config(), on_request=lambda request: asynchronous(request)).run([WorkloadRequest("r", 1, 1)])
        with temporary_directory() as directory:
            source, output = directory / "source.jsonl", directory / "routes.jsonl"
            source.write_text('{"input_tokens":1,"output_tokens":1}', encoding="utf-8")
            output.write_text("previous output", encoding="utf-8")
            def fail(request):
                raise OSError("log unavailable")
            with self.assertRaisesRegex(OSError, "log unavailable"):
                execute(config(), source=source, source_format="lengths", output=output, on_request=fail)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output")

    def test_existing_and_protected_files_are_never_overwritten(self):
        with temporary_directory() as directory:
            source = directory / "source.jsonl"
            source.write_text("original", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source or route"):
                RunLog(source, protected_paths=(source,))
            with self.assertRaises(FileExistsError):
                with RunLog(source):
                    pass
            self.assertEqual(source.read_text(encoding="utf-8"), "original")
            existing = directory / "existing"
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                with RunLog(existing):
                    pass

    def test_json_error_body_and_actual_payload_are_correlated_and_credentials_redacted(self):
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "question"}],
                       "metadata": {"api_key": "metadata-secret"}}
            error_body = {"error": {"message": "invalid: actual-secret", "type": "request_error"}}
            calls = []
            def opened(request, **kwargs):
                # Persist the attempted payload before sending the HTTP request.
                self.assertEqual(read_log(path)[-1]["event"], "litellm_send_started")
                calls.append(json.loads(request.data))
                return Response(json.dumps(error_body).encode(), status=400)
            with RunLog(path) as log, patch.dict(os.environ, {"KEY": "actual-secret"}), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                    patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)):
                with gateway.LiteLLMSession("ubuntu@example.com", run_log=log) as session:
                    result = session.send(payload, request_id="r", selected_endpoint_id="a")
            self.assertEqual(calls, [payload])
            self.assertEqual(result["response"], error_body)
            response = next(row for row in read_log(path) if row["event"] == "litellm_response")
            self.assertEqual(response["request_id"], "r")
            self.assertEqual(response["result"]["http_status"], 400)
            self.assertIn("[REDACTED]", response["result"]["response"]["error"]["message"])
            self.assertNotIn("actual-secret", json.dumps(read_log(path)))
            self.assertNotIn("metadata-secret", json.dumps(read_log(path)))

    def test_partial_stream_is_persisted_before_timeout_without_retry(self):
        received, calls = [], []
        class Interrupted(Response):
            def read1(self, size):
                if not received:
                    return 'data: {"choices":[{"delta":{"content":"部分回答"}}]}\n\n'.encode()
                raise TimeoutError("interrupted")
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            def opened(*args, **kwargs):
                calls.append(1)
                return Interrupted(headers={"Content-Type": "text/event-stream"})
            with self.assertRaises(TimeoutError), RunLog(path) as log, \
                    patch.dict(os.environ, {"KEY": "actual-secret"}), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                    patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)):
                with gateway.LiteLLMSession("ubuntu@example.com", run_log=log) as session:
                    def chunk(text):
                        self.assertEqual(read_log(path)[-1]["text"], text)
                        received.append(text)
                    session.send({"model": "deepseek-flash", "stream": True}, request_id="r", on_response_chunk=chunk)
            rows = read_log(path)
            self.assertEqual(calls, [1])
            self.assertEqual([row["event"] for row in rows],
                ["run_started", "litellm_send_started", "litellm_stream_chunk", "litellm_send_failed", "run_finished"])
            self.assertIn("部分回答", rows[2]["text"])
            self.assertEqual(rows[-1]["status"], "failed")

    def test_complete_sse_transcript_is_logged_with_usage_and_done(self):
        transcript = 'data: {"usage":{"total_tokens":5}}\n\ndata: [DONE]\n\n'
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            with RunLog(path) as log, patch.dict(os.environ, {"KEY": "actual-secret"}), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                    patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=lambda *a, **kw:
                        Response(transcript.encode(), headers={"Content-Type": "text/event-stream"}))):
                with gateway.LiteLLMSession("ubuntu@example.com", run_log=log) as session:
                    session.send({"model": "deepseek-flash", "stream": True}, request_id="r")
            response = next(row for row in read_log(path) if row["event"] == "litellm_response")
            self.assertEqual(response["result"]["response_format"], "sse")
            self.assertEqual(response["result"]["response"], transcript)

    def test_integrated_replay_logs_all_requests_and_real_responses_in_one_run(self):
        with temporary_directory() as directory:
            source, output, path = directory / "source.jsonl", directory / "routes.jsonl", directory / "run.jsonl"
            rows = [{"request_id": str(i), "input_tokens": tokens, "output_tokens": 1,
                     "messages": [{"role": "user", "content": f"question-{i}"}]} for i, tokens in enumerate((1, 20, 1))]
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            with patch.object(bridge, "load_config", return_value=config(endpoints=(endpoint(tpm=15),))), \
                    patch.dict(os.environ, {"KEY": "actual-secret"}), redirect_stdout(StringIO()), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)) as ssh, \
                    patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=lambda *a, **kw:
                        Response(b'{"choices":[{"message":{"content":"answer"}}]}'))):
                result = bridge.replay_and_send(ssh_target="ubuntu@example.com", source=source,
                    source_format="lengths", output=output, log_file=path)
            ssh.assert_called_once()
            events = read_log(path)
            requests = [row for row in events if row["event"] == "scheduler_request"]
            responses = [row for row in events if row["event"] == "litellm_response"]
            self.assertEqual([row["request_id"] for row in requests], ["0", "1", "2"])
            self.assertEqual([row["request_id"] for row in responses], ["0", "2"])
            self.assertEqual(result.log_path, path.with_suffix(""))
            self.assertEqual(responses[0]["result"]["response"]["choices"][0]["message"]["content"], "answer")
            self.assertEqual(len({row["run_id"] for row in events}), 1)

    def test_local_replay_cli_logs_requests_without_fabricated_model_responses(self):
        from ..simulation import cli
        with temporary_directory() as directory:
            source, output, log = directory / "source.jsonl", directory / "routes.jsonl", directory / "log.jsonl"
            source.write_text('{"input_tokens":1,"output_tokens":2}', encoding="utf-8")
            argv = ["replay", "--source", str(source), "--source-format", "lengths", "--output", str(output), "--log-file", str(log)]
            with patch.object(sys, "argv", argv), patch.object(cli, "load_config", return_value=config()), redirect_stdout(StringIO()):
                cli.main()
            events = read_log(log)
            self.assertEqual(sum(row["event"] == "scheduler_request" for row in events), 1)
            self.assertFalse(any(row["event"].startswith("litellm_") for row in events))

    def test_interrupt_keeps_prior_events_and_marks_cancelled_run(self):
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            with self.assertRaises(KeyboardInterrupt), RunLog(path) as log:
                log.record("checkpoint")
                raise KeyboardInterrupt
            self.assertEqual(read_log(path)[-1]["status"], "cancelled")

    def test_duplicate_id_logs_each_generated_request_before_ingress_rejection(self):
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            with self.assertRaisesRegex(ValueError, "Duplicate"), RunLog(path) as log:
                SimulationRunner(config(), on_request=log.scheduler_request).run([
                    WorkloadRequest("duplicate", 1, 1), WorkloadRequest("duplicate", 2, 2)])
            requests = [row["scheduler_request"] for row in read_log(path) if row["event"] == "scheduler_request"]
            self.assertEqual([request["input_tokens"] for request in requests], [1, 2])
            self.assertEqual(read_log(path)[-1]["status"], "failed")

    def test_failed_send_logging_prevents_post(self):
        with temporary_directory() as directory:
            path = directory / "run.jsonl"
            with RunLog(path) as log, patch.dict(os.environ, {"KEY": "actual-secret"}), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                    patch.object(gateway, "send_request") as send:
                with gateway.LiteLLMSession("ubuntu@example.com", run_log=log) as session:
                    with patch.object(log, "record", side_effect=OSError("disk full")), self.assertRaises(OSError):
                        session.send({"model": "deepseek-flash"}, request_id="r")
                send.assert_not_called()

    def test_each_request_has_its_own_file_and_run_events_are_separate(self):
        with temporary_directory() as directory:
            with RunLog(directory / "logs") as log:
                for request_id in ("request_000000", "request_000001"):
                    log.scheduler_request(SchedulerRequest(request_id, "deepseek-flash", 1, 2))
                    log.record("litellm_response", request_id=request_id, result={"response": request_id})
                    rows = [json.loads(line) for line in log.request_path(request_id).read_text(encoding="utf-8").splitlines()]
                    self.assertEqual({row["request_id"] for row in rows}, {request_id})
                    self.assertEqual([row["event"] for row in rows], ["scheduler_request", "litellm_response"])
            self.assertEqual({path.name for path in (log.path / "requests").iterdir()},
                             {"request_000000.jsonl", "request_000001.jsonl"})
            run_rows = [json.loads(line) for line in log.run_file.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all("request_id" not in row for row in run_rows))
            self.assertEqual(run_rows[-1]["request_log_count"], 2)

    def test_arbitrary_ids_cannot_escape_or_collide_in_request_directory(self):
        ids = ["../../CON", "a/b", "a\\b", "问题", "x" * 500, "CON", "request_000000"]
        with temporary_directory() as directory:
            with RunLog(directory / "logs") as log:
                for request_id in ids:
                    log.record("checkpoint", request_id=request_id)
                    path = log.request_path(request_id)
                    self.assertEqual(path.resolve().parent, (log.path / "requests").resolve())
                    self.assertLess(len(path.name), 120)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["request_id"], request_id)
                self.assertEqual(len(list((log.path / "requests").iterdir())), len(ids))

    def test_send_without_request_id_still_gets_a_separate_request_log(self):
        with temporary_directory() as directory:
            with RunLog(directory / "logs") as log, patch.dict(os.environ, {"KEY": "actual-secret"}), \
                    patch.object(gateway, "ssh_tunnel", return_value=nullcontext(1234)), \
                    patch.object(gateway, "send_request", return_value={"http_status": 200}) as send:
                with gateway.LiteLLMSession("ubuntu@example.com", run_log=log) as session:
                    session.send({"model": "deepseek-flash"})
                    session.send({"model": "deepseek-flash"})
                self.assertEqual(send.call_count, 2)
            request_files = list((log.path / "requests").iterdir())
            self.assertEqual(len(request_files), 2)
            for path in request_files:
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len({row["request_id"] for row in rows}), 1)
                self.assertEqual([row["event"] for row in rows], ["litellm_send_started", "litellm_response"])

    def test_local_cli_log_dir_option_creates_request_files(self):
        from ..simulation import cli
        with temporary_directory() as directory:
            source, output, log_dir = directory / "source.jsonl", directory / "routes.jsonl", directory / "logs"
            source.write_text('{"input_tokens":1,"output_tokens":2}', encoding="utf-8")
            argv = ["replay", "--source", str(source), "--source-format", "lengths", "--output", str(output), "--log-dir", str(log_dir)]
            with patch.object(sys, "argv", argv), patch.object(cli, "load_config", return_value=config()), redirect_stdout(StringIO()):
                cli.main()
            self.assertTrue((log_dir / "run.jsonl").is_file())
            path = log_dir / "requests" / "request_000000.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["request_id"] for row in rows}, {"request_000000"})
            self.assertEqual([row["event"] for row in rows], ["scheduler_request", "simulation_outcome"])
