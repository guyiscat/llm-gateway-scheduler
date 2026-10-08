"""Behavioral checks for batching, deadlines, routing and capacity recovery."""
from pathlib import Path
import json
import unittest

from ..simulation import SimulationConfig, SimulationRunner, EndpointConfig, WorkloadRequest, load_config
from ..simulation.routing import load_strategy
from ..simulation.cli import execute
from ..simulation.source import read_prompt_requests
from .support import FakeTokenizer, config, endpoint, req, temporary_directory


class SimulationTests(unittest.TestCase):





    def test_concurrency_completion_unblocks_queue(self):
        result = SimulationRunner(config(batch_size=1, endpoints=(endpoint(concurrency=1, latency=9),))).run([req(0), req(1)])
        first, second = result.requests
        self.assertEqual(first["finished_at_ms"], 10)
        self.assertEqual(second["dispatch_at_ms"], 10)
        self.assertEqual(second["capacity_wait_ms"], 9)
        self.assertEqual(result.endpoints[0]["peak_concurrency"], 1)

    def test_rpm_recovers_at_exact_window_boundary(self):
        result = SimulationRunner(config(batch_size=1, endpoints=(endpoint(rpm=1),))).run([req(0), req(1), req(2)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 60000, 120000])
        self.assertEqual(result.endpoints[0]["total_requests"], 3)
        self.assertEqual(result.endpoints[0]["peak_requests_in_window"], 1)

    def test_tpm_reservation_and_window_recovery(self):
        result = SimulationRunner(config(batch_size=1, endpoints=(endpoint(tpm=15),))).run([req(0), req(1)])
        self.assertEqual([r["dispatch_at_ms"] for r in result.requests], [0, 60000])
        self.assertEqual(result.endpoints[0]["total_tokens"], 22)
        self.assertEqual(result.endpoints[0]["peak_tokens_in_window"], 11)

    def test_oversized_request_rejected_without_blocking_next(self):
        result = SimulationRunner(config(batch_size=1, endpoints=(endpoint(tpm=20),))).run([req(0, 21), req(1)])
        self.assertEqual(result.requests[0]["status"], "rejected")
        self.assertEqual(result.requests[1]["status"], "completed")
        self.assertEqual(result.summary["rejected_requests"], 1)

    def test_strategy_can_be_replaced(self):
        class LastEndpoint:
            def select(self, request, endpoints, now_ms):
                return endpoints[-1].endpoint_id
        settings = config(batch_size=1, endpoints=(endpoint("a"), endpoint("b")))
        result = SimulationRunner(settings, strategy=LastEndpoint()).run([req(0), req(1)])
        self.assertEqual([r["endpoint_id"] for r in result.requests], ["b", "b"])
        self.assertTrue(hasattr(load_strategy("workload_profiling.simulation.routing:MinRpmStrategy"), "select"))

    def test_invalid_strategy_selection_fails(self):
        class Invalid:
            def select(self, request, endpoints, now_ms):
                return "missing"
        with self.assertRaises(ValueError):
            SimulationRunner(config(batch_size=1), strategy=Invalid()).run([req(0)])

    def test_zero_wait_single_batch_and_empty_input(self):
        result = SimulationRunner(config(batch_wait_ms=0, batch_size=100)).run([req(0)])
        self.assertEqual(result.requests[0]["dispatch_at_ms"], 0)
        empty = SimulationRunner(config()).run([])
        self.assertEqual(empty.summary["total_requests"], 0)

    def test_duplicate_ids_and_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            SimulationRunner(config()).run([req(0), req(0)])
        for kwargs in ({"batch_size": 0}, {"arrival_interval_ms": 0}, {"batch_wait_ms": -1},
                       {"output_threshold_tokens": float("nan")}, {"endpoints": ()}, {"window_ms": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                config(**kwargs)
        self.assertEqual(load_config().batch_size, 16)

    def test_original_rows_not_history_expanded(self):
        row = {"prompt": {"messages": [{"role": "user", "content": "a"},
               {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]}, "response": "d"}
        with temporary_directory() as temporary:
            path = Path(temporary)/"source.jsonl"
            path.write_text(json.dumps(row)+"\n", encoding="utf-8")
            requests = list(read_prompt_requests(path, FakeTokenizer()))
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].output_tokens, 1)

    def test_cli_workflow_persists_complete_results(self):
        with temporary_directory() as temporary:
            directory = Path(temporary)
            path = directory/"source.jsonl"
            path.write_text('\n'.join(json.dumps({"input_tokens": n, "output_tokens": 1}) for n in [1, 10, 20])+"\n", encoding="utf-8")
            result, summary = execute(config(), source=path, source_format="lengths", output=directory/"results")
            self.assertEqual(summary["completed_requests"], 3)
            for name in ("requests.csv", "events.jsonl", "endpoints.json", "batches.json", "summary.json", "report.md", "config.json"):
                self.assertTrue((directory/"results"/name).exists())
            events = [json.loads(line) for line in (directory/"results/events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(e["event"] == "arrived" for e in events), 3)
            self.assertEqual(len(result.requests), 3)


if __name__ == "__main__":
    unittest.main()
