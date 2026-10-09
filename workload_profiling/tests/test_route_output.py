"""Actual handoff payloads, dispatch order and failure-safe local output."""
import json
import unittest
from unittest.mock import patch

from ..adapters import LiteLLMAdapter, RouteOutputRecorder
from ..core import EndpointConfig, RouteDecision, SchedulerRequest
from ..simulation import SimulationRunner, WorkloadRequest
from ..simulation.cli import execute
from ..common.io import sha256_file
from .support import config, endpoint, FakeTokenizer, temporary_directory


class RouteOutputTests(unittest.TestCase):
    def test_payload_matches_adapter_and_selected_deployment(self):
        ep = EndpointConfig("chosen", 100, 10000, 4, api_base="https://endpoint.example/v1",
                            deployment_model="openai/deployment")
        request = SchedulerRequest("r", "logical", 5, 10,
            messages=({"role": "user", "content": "你好"},), stream=True, max_tokens=50,
            metadata={"temperature": .2, "force_endpoint": "wrong"})
        decision = RouteDecision(request, ep.endpoint_id, 30, (ep.endpoint_id,))
        with temporary_directory() as directory:
            output = directory / "routes.jsonl"
            with RouteOutputRecorder(output, (ep,)) as recorder:
                recorder(decision)
                self.assertFalse(output.exists())
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(row["litellm_params"], LiteLLMAdapter().build_params(decision, ep))
            self.assertEqual(row["litellm_params"]["model"], "openai/deployment")
            self.assertEqual(row["litellm_params"]["messages"][0]["content"], "你好")
            self.assertEqual(row["dispatched_at_ms"], 30)
            self.assertEqual(recorder.count, 1)

    def test_window_output_follows_dispatch_order_and_omits_rejected(self):
        settings = config(batch_size=3, batch_wait_ms=1000, batch_order="priority_then_light",
                          endpoints=(endpoint(concurrency=1, latency=99, tpm=1000),))
        with temporary_directory() as directory:
            source = directory / "source.jsonl"
            rows = [{"request_id": str(i), "input_tokens": 1, "output_tokens": 1,
                     "priority_level": level} for i, level in enumerate((2, 1, 3, 2))]
            rows.append({"request_id": "oversize", "input_tokens": 1001, "output_tokens": 1})
            source.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            output = directory / "routes.jsonl"
            result = execute(settings, source=source, source_format="lengths", output=output)
            routes = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["request_id"] for r in routes], ["0", "2", "3", "1"])
            self.assertEqual([r["dispatched_at_ms"] for r in routes], [0, 100, 200, 300])
            self.assertEqual(result.summary["rejected_requests"], 1)

    def test_prompt_messages_and_controls_reach_handoff_without_answer(self):
        with temporary_directory() as directory:
            source = directory / "source.jsonl"
            prompt = {"messages": [{"role": "user", "content": "question"}],
                      "temperature": .3, "max_tokens": 50, "stream": True}
            source.write_text(json.dumps({"prompt": prompt, "response": "recorded-answer"}), encoding="utf-8")
            output = directory / "routes.jsonl"
            execute(config(), source=source, tokenizer=FakeTokenizer(), output=output)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(row["litellm_params"]["messages"], prompt["messages"])
            self.assertEqual(row["litellm_params"]["temperature"], .3)
            self.assertEqual(row["litellm_params"]["max_tokens"], 50)
            self.assertNotIn("recorded-answer", output.read_text(encoding="utf-8"))

    def test_bad_input_and_changed_source_preserve_previous_output(self):
        with temporary_directory() as directory:
            source = directory / "source.jsonl"
            output = directory / "routes.jsonl"
            output.write_text("previous output\n", encoding="utf-8")
            source.write_text('{"input_tokens":1,"output_tokens":1}\ninvalid\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                execute(config(), source=source, source_format="lengths", output=output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output\n")
            source.write_text('{"input_tokens":1,"output_tokens":1}\n', encoding="utf-8")
            actual_hash = sha256_file(source)
            with patch("workload_profiling.simulation.cli.sha256_file", side_effect=(actual_hash, "changed")):
                with self.assertRaisesRegex(ValueError, "Source changed"):
                    execute(config(), source=source, source_format="lengths", output=output)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output\n")
            self.assertEqual({p.name for p in directory.iterdir()}, {"source.jsonl", "routes.jsonl"})

    def test_failed_publish_preserves_previous_output_and_cleans_temporary(self):
        with temporary_directory() as directory:
            output = directory / "routes.jsonl"
            output.write_text("previous", encoding="utf-8")
            with patch("workload_profiling.adapters.route_output.os.replace", side_effect=OSError("locked")):
                with self.assertRaisesRegex(OSError, "locked"):
                    with RouteOutputRecorder(output, ()):
                        pass
            self.assertEqual(output.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(directory.iterdir()), [output])

    def test_source_cannot_be_output_and_empty_input_has_empty_log(self):
        with temporary_directory() as directory:
            source = directory / "source.jsonl"
            source.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overwrite the source"):
                execute(config(), source=source, source_format="lengths", output=source)
            output = directory / "routes.jsonl"
            execute(config(), source=source, source_format="lengths", output=output)
            self.assertEqual(output.read_bytes(), b"")

    def test_async_route_hook_is_rejected_and_synchronous_errors_propagate(self):
        async def callback(decision):
            return None
        with self.assertRaises(TypeError):
            SimulationRunner(config(), on_route=callback)
        with self.assertRaisesRegex(TypeError, "awaitable"):
            SimulationRunner(config(), on_route=lambda d: callback(d)).run([WorkloadRequest("r", 1, 1)])
        def fail(decision):
            raise OSError("recorder failed")
        with self.assertRaisesRegex(OSError, "recorder failed"):
            SimulationRunner(config(), on_route=fail).run([WorkloadRequest("r", 1, 1)])
