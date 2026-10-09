"""Real local HTTP requests through the mainline console and replay service."""
from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..simulation import load_config
from ..web.simulation_console import SimulationService, create_server
from .support import FakeTokenizer, temporary_directory


class SimulationConsoleTests(unittest.TestCase):
    def test_http_defaults_replay_trace_and_csv(self):
        with temporary_directory() as directory, patch("workload_profiling.web.simulation_console.RESULTS", directory):
            service = SimulationService(tokenizer=FakeTokenizer())
            server = create_server(service, port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(base + "/api/simulation/config", timeout=5) as response:
                    settings = json.load(response)
                self.assertEqual(settings, json.loads(json.dumps(load_config().to_dict())))
                self.assertNotIn("batch_scope", settings)
                self.assertNotIn("scheduling_mode", settings)
                settings.update(busy_rpm_threshold=.90, busy_tpm_threshold=.85, busy_concurrency_reserve=2)
                payload = json.dumps({"config": settings, "limit": 48}).encode()
                request = Request(base + "/api/simulation/run", payload, {"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with urlopen(base + "/api/simulation/status", timeout=5) as response:
                        state = json.load(response)
                    if state["status"] != "running":
                        break
                    time.sleep(.01)
                self.assertEqual(state["status"], "completed", state)
                self.assertEqual(state["summary"]["completed_requests"], 48)
                self.assertEqual(state["summary"]["busy_thresholds"], {"rpm": .9, "tpm": .85, "concurrency_reserve": 2})
                with urlopen(base + "/api/simulation/requests?limit=48", timeout=5) as response:
                    rows = json.load(response)["requests"]
                self.assertEqual(len(rows), 48)
                self.assertTrue(all(r["batch_id"] is None for r in rows if r["priority_level"] == 4))
                with urlopen(base + "/simulation/requests.csv", timeout=5) as response:
                    self.assertTrue(response.read().startswith(b"\xef\xbb\xbf"))
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(base + "/api/simulation/requests?limit=201", timeout=5)
                self.assertEqual(rejected.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

    def test_web_rejects_removed_options_and_external_imports_before_start(self):
        service = SimulationService()
        for changes in ({"batch_order": "foreign.module:Order"}, {"strategy": "foreign.module:Route"},
                        {"batch_scope": "heavy_only"}, {"priority_assignment": "synthetic"},
                        {"heavy_priority_discount": .5}):
            settings = deepcopy(load_config().to_dict()) | changes
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                service.start({"config": settings, "limit": 1})
            self.assertEqual(service.snapshot()["status"], "idle")
