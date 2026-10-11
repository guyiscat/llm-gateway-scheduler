"""Live endpoint replay through an existing connection, without paid calls."""
from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

from ..integrations import replay_litellm as bridge, session_control
from ..simulation.config import load_config
from ..simulation.replay import execute
from .support import temporary_directory
from . import test_persistent_session as sessions


class ReplayLiteLLMTests(unittest.TestCase):
    running = sessions.PersistentSessionTests.running
    batch = sessions.PersistentSessionTests.batch

    def test_cli_defaults_to_three_and_reuses_session(self):
        argv = ["replay-send", "--endpoint-group", "official", "--no-max-tokens"]
        with patch.object(sys, "argv", argv), patch.object(session_control, "send_batch",
                return_value={"sent_count": 3, "rejected_count": 0}) as run, redirect_stdout(StringIO()):
            bridge.main()
        options = run.call_args.args[1]
        self.assertEqual(options["limit"], 3)
        self.assertEqual(options["endpoint_groups"], ["official"])
        self.assertTrue(options["no_max_tokens"])
        self.assertNotIn("model", options)
        self.assertNotIn("ssh_target", options)

    def test_real_replay_matches_existing_virtual_flow_and_dispatch_order(self):
        with temporary_directory() as directory, self.running(directory) as running:
            config = load_config(running.broker.config_path, endpoint_configs=running.monitor.endpoint_configs())
            baseline = execute(config, source=running.options["source"], source_format="lengths", limit=3,
                               output=directory / "baseline.jsonl", route_adapter=running.monitor.route_adapter())
            responses = []
            with redirect_stdout(StringIO()):
                result = bridge.replay_and_send(**running.options, config_path=running.broker.config_path,
                    connected_session=running.session, connected_monitor=running.monitor,
                    log_dir=directory / "logs", on_response=lambda route, response: responses.append(route["request_id"]))
            self.assertEqual(result.simulation.requests, baseline.requests)
            self.assertEqual(result.simulation.events, baseline.events)
            self.assertEqual(result.sent_count, 3)
            self.assertEqual(responses, [row["request_id"] for row in baseline.requests if row["status"] == "completed"])
            self.assertEqual((directory / "routes.jsonl").read_bytes(), (directory / "baseline.jsonl").read_bytes())

    def test_invalid_later_payload_stops_before_first_post(self):
        with temporary_directory() as directory, self.running(directory) as running:
            source = directory / "source.jsonl"
            rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
            rows[-1]["messages"] = []
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            _, events = self.batch(running, log_dir=str(directory / "logs"))
            self.assertEqual(events[-1]["event"], "error")
            self.assertIn("messages", events[-1]["message"])
            self.assertEqual(running.calls, [])

    def test_bad_source_preserves_previous_output_and_never_sends_stale_routes(self):
        with temporary_directory() as directory, self.running(directory) as running:
            (directory / "source.jsonl").write_text("invalid json", encoding="utf-8")
            output = directory / "routes.jsonl"
            output.write_text("previous output", encoding="utf-8")
            _, events = self.batch(running, log_dir=str(directory / "logs"))
            self.assertEqual(events[-1]["event"], "error")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous output")
            self.assertEqual(running.calls, [])

    def test_unsupported_model_is_logged_rejected_and_not_sent(self):
        with temporary_directory() as directory, self.running(directory) as running:
            source = directory / "source.jsonl"
            rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
            rows[1]["target_model"] = "unsupported"
            source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            _, events = self.batch(running, log_dir=str(directory / "logs"))
            self.assertEqual(events[-1]["sent_count"], 2)
            self.assertEqual(events[-1]["rejected_count"], 1)
            self.assertEqual(len(running.calls), 2)
            self.assertEqual(len(list((directory / "logs/requests").glob("*.jsonl"))), 3)

    def test_changed_scheduler_config_rejected_without_calls(self):
        with temporary_directory() as directory, self.running(directory) as running:
            scheduler = directory / "scheduler.json"
            settings = json.loads(scheduler.read_text(encoding="utf-8"))
            settings["max_batch_size"] = 99
            scheduler.write_text(json.dumps(settings), encoding="utf-8")
            _, events = self.batch(running, log_dir=str(directory / "logs"))
            self.assertIn("close and log in again", events[-1]["message"])
            self.assertEqual(running.calls, [])

    def test_static_endpoint_config_refused_even_when_live_endpoints_exist(self):
        with temporary_directory() as directory, self.running(directory) as running:
            config_path = directory / "simulation.json"
            settings = json.loads(config_path.read_text(encoding="utf-8"))
            settings["endpoint_config"] = "old_endpoints.json"
            config_path.write_text(json.dumps(settings), encoding="utf-8")
            _, events = self.batch(running, log_dir=str(directory / "logs"))
            self.assertIn("Static endpoint configuration was removed", events[-1]["message"])
            self.assertEqual(running.calls, [])
