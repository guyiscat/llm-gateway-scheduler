"""Reusable CLI session lifecycle without SSH or paid model calls."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
import json
import os
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ..core.config import SchedulerConfig
from ..endpoint_monitor import EndpointMonitor
from ..integrations import litellm_gateway as gateway, replay_litellm as bridge
from ..integrations import session_control as client, session_service as service
from .support import temporary_directory
from .test_endpoint_monitor import catalog, background
from .test_litellm_gateway import Response


class PersistentSessionTests(unittest.TestCase):
    @contextmanager
    def running(self, directory, *, send=None, idle_timeout=600, clock=time.monotonic):
        config = directory / "simulation.json"
        scheduler = directory / "scheduler.json"
        scheduler.write_text(json.dumps(SchedulerConfig(busy_concurrency_reserve=0).to_dict()), encoding="utf-8")
        config.write_text(json.dumps({"scheduler_config": "scheduler.json",
            "arrival_mode": "fixed", "priority_assignment": "uniform"}), encoding="utf-8")
        source = directory / "source.jsonl"
        source.write_text('\n'.join(json.dumps({"input_tokens": 1, "output_tokens": 2,
            "messages": [{"role": "user", "content": "question"}]}) for _ in range(3)), encoding="utf-8")
        control_dir = directory / "control"
        control_dir.mkdir()
        token = "a" * 64
        (control_dir / "control.key").write_text(token, encoding="ascii")
        calls = []
        def opened(request, **kwargs):
            if request.get_method() == "GET":
                return SimpleNamespaceResponse(json.dumps(catalog()).encode())
            payload = json.loads(request.data)
            calls.append(payload)
            if send is not None:
                return send(payload, len(calls))
            return Response(b'{"usage":{"prompt_tokens":5,"completion_tokens":3},"choices":[{"message":{"content":"answer"}}]}')
        with patch.object(gateway, "build_opener", return_value=SimpleNamespace(open=opened)):
            session = gateway.LiteLLMSession("ubuntu@example.com").adopt_connection(1234, "test-secret")
            try:
                with EndpointMonitor(session, directory / "endpoints", SchedulerConfig(busy_concurrency_reserve=0), periodic=False) as monitor:
                    broker = service.SessionBroker(session, monitor, control_dir, token, config, idle_timeout, clock=clock)
                    owner = SimpleNamespace(alive=lambda: True)
                    task = background(lambda: broker.serve(owner))
                    deadline = time.monotonic() + 3
                    while not (control_dir / "session.json").exists():
                        if task.done():
                            task.result()
                        if time.monotonic() > deadline:
                            self.fail("Local session did not start")
                        time.sleep(.01)
                    yield SimpleNamespace(broker=broker, task=task, directory=control_dir, monitor=monitor,
                        calls=calls, session=session, options={"source": str(source), "source_format": "lengths",
                        "limit": 3, "output": str(directory / "routes.jsonl")})
            finally:
                if "broker" in locals():
                    broker.stop.set()
                    task.result(timeout=5)
                session.__exit__()

    def batch(self, running, **options):
        connection, token = client.connection(running.directory)
        try:
            connection.request("POST", "/replay", json.dumps(running.options | options),
                               {"Authorization": "Bearer " + token})
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            return response.status, [json.loads(line) for line in body.splitlines()]
        finally:
            connection.close()

    def test_two_commands_reuse_session_state_and_separate_request_logs(self):
        with temporary_directory() as directory, self.running(directory) as running:
            with patch.object(gateway, "ssh_tunnel") as ssh, patch.object(gateway.getpass, "getpass") as key:
                first = self.batch(running, log_dir=str(directory / "logs1"))
                second = self.batch(running, log_dir=str(directory / "logs2"))
            self.assertEqual(first[1][-1]["sent_count"], 3)
            self.assertEqual(second[1][-1]["sent_count"], 3)
            self.assertEqual(len(running.calls), 6)
            self.assertEqual(sum(s.total_requests for s in running.monitor.states.states.values()), 6)
            self.assertEqual(sum(s.tokens_in_window for s in running.monitor.states.states.values()), 48)
            self.assertEqual(sum(len(s.window) for s in running.monitor.states.states.values()), 6)
            ssh.assert_not_called()
            key.assert_not_called()
            self.assertEqual(client.control(running.directory, "status")["status"], "ready")
            for name in ("logs1", "logs2"):
                logs = list((directory / name / "requests").glob("*.jsonl"))
                self.assertEqual(len(logs), 3)
                self.assertTrue(all('"litellm_response"' in path.read_text(encoding="utf-8") for path in logs))
            self.assertNotIn("test-secret", ''.join(path.read_text(encoding="utf-8") for path in running.directory.iterdir()))

    def test_failed_batch_stops_without_closing_or_retrying_and_later_batch_is_allowed(self):
        def response(payload, number):
            return Response(b'{"error":{"message":"bad request"}}', status=400) if number == 1 else Response(b'{}')
        with temporary_directory() as directory, self.running(directory, send=response) as running:
            _, events = self.batch(running, log_dir=str(directory / "failed"))
            self.assertEqual(events[-1]["event"], "error")
            self.assertIn("HTTP 400", events[-1]["message"])
            self.assertEqual(len(running.calls), 1)
            self.assertEqual(client.control(running.directory, "status")["status"], "ready")
            self.assertEqual(self.batch(running, limit=1, log_dir=str(directory / "next"))[1][-1]["sent_count"], 1)
            self.assertEqual(len(running.calls), 2)

    def test_manual_close_drains_active_request_and_cancels_remaining_sends(self):
        started, release = Event(), Event()
        def response(payload, number):
            started.set()
            if not release.wait(3):
                raise TimeoutError("Test request not released")
            return Response(b'{}')
        with temporary_directory() as directory, self.running(directory, send=response) as running:
            task = background(lambda: self.batch(running, log_dir=str(directory / "logs")))
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(client.control(running.directory, "close")["status"], "closing")
                self.assertFalse(running.task.done())
            finally:
                release.set()
            _, events = task.result(timeout=5)
            self.assertEqual(len(running.calls), 1)
            self.assertIn("Session closing", events[-1]["message"])
            running.task.result(timeout=5)
            self.assertFalse(running.monitor.states.inflight)

    def test_concurrent_batch_is_rejected_and_never_sends_extra_requests(self):
        started, release = Event(), Event()
        def response(payload, number):
            started.set()
            release.wait(3)
            return Response(b'{}')
        with temporary_directory() as directory, self.running(directory, send=response) as running:
            task = background(lambda: self.batch(running, limit=1, log_dir=str(directory / "logs")))
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaisesRegex(OSError, "Another batch is running"):
                    client.send_batch(running.directory, running.options)
            finally:
                release.set()
            self.assertEqual(task.result(timeout=5)[1][-1]["sent_count"], 1)
            self.assertEqual(len(running.calls), 1)

    def test_idle_timeout_ignores_active_batch_and_restarts_after_completion(self):
        now, started, release = [0.0], Event(), Event()
        def response(payload, number):
            started.set()
            release.wait(3)
            return Response(b'{}')
        with temporary_directory() as directory, self.running(directory, send=response, clock=lambda: now[0], idle_timeout=600) as running:
            task = background(lambda: self.batch(running, limit=1, log_dir=str(directory / "logs")))
            try:
                self.assertTrue(started.wait(2))
                now[0] = 700
                self.assertFalse(running.broker.idle_expired())
                self.assertTrue(client.control(running.directory, "status")["busy"])
            finally:
                release.set()
            task.result(timeout=5)
            now[0] = 1299
            self.assertEqual(client.control(running.directory, "status")["idle_remaining_seconds"], 1)
            self.assertFalse(running.broker.idle_expired())
            now[0] = 1300
            self.assertTrue(running.broker.idle_expired())
            running.task.result(timeout=5)

    def test_unauthenticated_commands_do_not_close_or_send(self):
        with temporary_directory() as directory, self.running(directory) as running:
            connection = HTTPConnection("127.0.0.1", running.broker.server.server_address[1], timeout=3)
            try:
                connection.request("POST", "/close", headers={"Authorization": "Bearer wrong"})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 401)
                self.assertFalse(running.broker.stop.is_set())
                self.assertEqual(running.calls, [])
            finally:
                connection.close()

    def test_cli_without_login_does_not_load_data_or_authenticate(self):
        with temporary_directory() as directory, patch.object(sys, "argv", ["replay-send", "--session-dir", str(directory)]), \
                patch.object(bridge, "execute") as replay, patch.object(gateway, "ssh_tunnel") as ssh, \
                redirect_stderr(StringIO()) as errors:
            with self.assertRaises(SystemExit) as error:
                bridge.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("litellm-login first", errors.getvalue())
            replay.assert_not_called()
            ssh.assert_not_called()


    def test_login_handoff_uses_stdin_not_credential_files_or_arguments(self):
        with temporary_directory() as directory:
            session_dir = directory / "control"
            session_dir.mkdir()
            lease_seen, transferred = [], []
            class Pipe(BytesIO):
                def close(self):
                    transferred.append(json.loads(self.getvalue()))
                    super().close()
            class FakeSession:
                def __init__(self, target, **kwargs):
                    self._key, self._port = "test-secret", 1234
                    lease_seen.append(kwargs["tunnel_lease"])
                    lease_seen[-1].process = SimpleNamespace(pid=123)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self._key = None
            def popen(*args, **kwargs):
                (session_dir / "session.json").write_text('{}', encoding="utf-8")
                return SimpleNamespace(pid=456, stdin=Pipe(), poll=lambda: None)
            with patch.object(service, "private_directory", return_value=session_dir), \
                    patch.object(service, "LiteLLMSession", FakeSession), \
                    patch.object(service.subprocess, "Popen", side_effect=popen) as process, \
                    patch.object(service, "control", return_value={"status": "ready"}), \
                    patch.dict(os.environ, {"KEY": "test-secret"}):
                service.login(directory=session_dir, ssh_target="ubuntu@example.com")
            self.assertTrue(lease_seen[0].detached)
            self.assertEqual(transferred[0]["api_key"], "test-secret")
            self.assertNotIn("test-secret", str(process.call_args.args))
            self.assertNotIn("KEY", process.call_args.kwargs["env"])
            self.assertNotIn("test-secret", ''.join(path.read_text(encoding="utf-8") for path in session_dir.iterdir()))
            if os.name == "nt":
                self.assertEqual(process.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_login_failure_cleans_owned_control_files(self):
        with temporary_directory() as directory:
            with patch.object(service, "private_directory", return_value=directory), \
                    patch.object(service, "LiteLLMSession") as session:
                session.return_value.__enter__.side_effect = OSError("login failed")
                with self.assertRaisesRegex(OSError, "login failed"):
                    service.login(directory=directory, ssh_target="ubuntu@example.com")
            self.assertFalse((directory / ".session.lock").exists())
            self.assertFalse((directory / "control.key").exists())

    def test_invalid_timeout_or_limits_fail_before_authentication(self):
        with temporary_directory() as directory, patch.object(service, "LiteLLMSession") as session:
            for options in ({"idle_timeout": 0}, {"idle_timeout": float("nan")}, {"endpoint_limit_overrides": {"rpm_limit": 0}}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    service.login(directory=directory, ssh_target="ubuntu@example.com", **options)
            session.assert_not_called()

    def test_worker_discovery_failure_closes_owned_tunnel_and_cleans_locks(self):
        with temporary_directory() as directory:
            control_dir = directory / "control"
            control_dir.mkdir()
            owner = SimpleNamespace(close=Mock())
            bootstrap = dict(directory=str(control_dir), ssh_pid=123, ssh_target="ubuntu@example.com",
                timeout=60, tunnel_port=1234, api_key="test-secret", config_path=str(service.CONFIG_PATH),
                endpoint_state_dir=str(directory / "endpoints"), endpoint_update_interval=30,
                endpoint_limit_overrides=None, idle_timeout=600, token="a" * 64)
            with patch.object(service, "WindowsProcessOwner", return_value=owner), \
                    patch.object(gateway, "build_opener", return_value=SimpleNamespace(
                        open=lambda *a, **kw: SimpleNamespaceResponse(b'{"data":[]}'))), \
                    patch.object(gateway, "send_request") as send:
                service.worker(bootstrap)
            owner.close.assert_called_once()
            send.assert_not_called()
            self.assertFalse((control_dir / ".session.lock").exists())
            self.assertFalse((directory / "endpoints/.endpoint_monitor.lock").exists())
            self.assertNotIn("test-secret", (control_dir / "startup_error.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows tunnel process ownership")
    def test_real_background_process_survives_login_return_and_serves_two_commands(self):
        posts = []
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self, value):
                body = json.dumps(value).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self.respond(catalog())
            def do_POST(self):
                posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.respond({"usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        tunnel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
        worker_owner = None
        try:
            with temporary_directory() as directory:
                config = directory / "simulation.json"
                config.write_text(json.dumps({"arrival_mode": "fixed", "priority_assignment": "uniform",
                    "busy_concurrency_reserve": 0, "output_classification": "tokens"}), encoding="utf-8")
                source = directory / "source.jsonl"
                source.write_text(json.dumps({"input_tokens": 1, "output_tokens": 2,
                    "messages": [{"role": "user", "content": "question"}]}), encoding="utf-8")
                control_dir = directory / "control"
                @contextmanager
                def authenticated_tunnel(*args, lease=None, **kwargs):
                    lease.process = tunnel
                    try:
                        yield proxy.server_address[1]
                    finally:
                        if not lease.detached:
                            tunnel.terminate()
                def protected_directory(path):
                    # The agent filesystem sandbox denies WRITE_DAC. ACL setup
                    # is checked separately; the worker/IPC here are real.
                    path.mkdir()
                    return path
                with patch.object(gateway, "ssh_tunnel", side_effect=authenticated_tunnel), \
                        patch.object(service, "private_directory", side_effect=protected_directory), \
                        patch.dict(os.environ, {"KEY": "test-secret"}), redirect_stdout(StringIO()):
                    status = service.login(directory=control_dir, ssh_target="ubuntu@example.com", config_path=config,
                        endpoint_state_dir=directory / "endpoints", idle_timeout=600)
                self.assertEqual(status["status"], "ready")
                worker_pid = json.loads((control_dir / "session.json").read_text(encoding="utf-8"))["pid"]
                worker_owner = client.WindowsProcessOwner(worker_pid)
                try:
                    self.assertIsNone(tunnel.poll())
                    for index in range(2):
                        with redirect_stdout(StringIO()):
                            result = client.send_batch(control_dir, {"source": str(source), "source_format": "lengths",
                                "limit": 1, "output": str(directory / "routes.jsonl"), "log_dir": str(directory / f"logs{index}")})
                        self.assertEqual(result["sent_count"], 1)
                    self.assertEqual(len(posts), 2)
                    state = json.loads((directory / "endpoints/endpoint_state.json").read_text(encoding="utf-8"))
                    self.assertEqual(sum(row["runtime"]["total_requests"] for row in state["endpoints"]), 2)
                    self.assertEqual(client.control(control_dir, "close")["status"], "closing")
                    deadline = time.monotonic() + 5
                    while worker_owner.alive() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(worker_owner.alive())
                    tunnel.wait(timeout=2)
                    self.assertFalse((control_dir / "session.json").exists())
                    self.assertFalse((control_dir / "control.key").exists())
                    self.assertFalse((directory / "endpoints/.endpoint_monitor.lock").exists())
                    self.assertFalse((control_dir / "startup_error.json").exists())
                    self.assertNotIn("test-secret", (control_dir / "worker.log").read_text(encoding="utf-8"))
                finally:
                    worker_owner.close()
                    worker_owner = None
        finally:
            if worker_owner is not None:
                worker_owner.close()
            if tunnel.poll() is None:
                tunnel.terminate()
            tunnel.wait(timeout=3)
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=3)

    @unittest.skipUnless(os.name == "nt", "Windows tunnel process ownership")
    def test_private_directory_requests_current_user_acl_and_fails_closed(self):
        with temporary_directory() as directory:
            identity = SimpleNamespace(stdout=b'user S-1-5-21-12345', returncode=0)
            for code in (0, 5):
                with patch.object(client.subprocess, "run", side_effect=[identity, SimpleNamespace(returncode=code)]) as run:
                    if code:
                        with self.assertRaisesRegex(OSError, "Cannot protect"):
                            client.private_directory(directory)
                    else:
                        client.private_directory(directory)
                    command = run.call_args.args[0]
                    self.assertIn("/inheritance:r", command)
                    self.assertIn("*S-1-5-21-12345:(OI)(CI)F", command)

    @unittest.skipUnless(os.name == "nt", "Windows tunnel process ownership")
    def test_windows_owner_keeps_handle_and_closes_only_the_owned_process(self):
        # Harmless local Python process: no SSH, network or model request.
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            owner = client.WindowsProcessOwner(process.pid)
            self.assertTrue(owner.alive())
            owner.close()
            self.assertFalse(owner.alive())
            process.wait(timeout=3)
            owner.close()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


class SimpleNamespaceResponse(Response):
    def read(self, size=-1):
        return super().read(size)
