"""Background owner of one authenticated tunnel, monitor and serial replay jobs."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Event, Lock, Thread
import time
import warnings

from ..common.paths import RESULTS, ROOT
from ..common.validation import positive_integer
from ..endpoint_monitor import EndpointMonitor
from ..endpoint_monitor.service import write_json
from ..simulation.config import CONFIG_PATH, load_config, load_scheduler_settings
from .litellm_gateway import LiteLLMSession, validate_timeout
from .session_control import TunnelLease, WindowsProcessOwner, control, private_directory


def cleanup_control(directory):
    # Only our three ephemeral files; preserve worker diagnostics and request logs.
    for name in ("session.json", "control.key", ".session.lock"):
        (Path(directory) / name).unlink(missing_ok=True)


class EventStream:
    def __init__(self, handler, secret):
        self.handler, self.secret = handler, secret

    def event(self, value):
        line = json.dumps(value, ensure_ascii=False, allow_nan=False).replace(self.secret, "[REDACTED]")
        self.handler.wfile.write((line + "\n").encode("utf-8"))
        self.handler.wfile.flush()

    def write(self, text):
        if text:
            self.event({"event": "output", "text": text})
        return len(text)

    def flush(self):
        pass


class SessionBroker:
    """Batch failures keep the session alive; close cancels future dispatches."""
    def __init__(self, session, monitor, directory, token, config_path, idle_timeout, *, clock=time.monotonic):
        self.session, self.monitor, self.directory = session, monitor, Path(directory)
        self.token, self.config_path, self.idle_timeout = token, str(config_path), idle_timeout
        self.clock, self.last_activity = clock, clock()
        self.stop, self.state_lock, self.job_lock = Event(), Lock(), Lock()
        self.busy = False
        self.server = None

    def status(self):
        with self.state_lock:
            return {"status": "closing" if self.stop.is_set() else "ready", "busy": self.busy,
                    "ssh_target": self.session.target, "idle_timeout": self.idle_timeout,
                    "idle_remaining_seconds": None if self.busy else max(0, self.idle_timeout - (self.clock() - self.last_activity)),
                    "config_path": self.config_path, "endpoint_state_dir": str(self.monitor.directory),
                    "endpoint_update_interval": self.monitor.interval}

    def idle_expired(self):
        with self.state_lock:
            if not self.busy and self.clock() - self.last_activity >= self.idle_timeout:
                self.stop.set()
            return self.stop.is_set()

    def handler(self):
        broker = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value):
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authenticated(self):
                if not secrets.compare_digest(self.headers.get("Authorization", "").encode("utf-8"),
                                              ("Bearer " + broker.token).encode("ascii")):
                    self.reply(401, {"error": "Unauthorized local session command"})
                    return False
                return True

            def do_GET(self):
                if self.authenticated():
                    self.reply(200, broker.status()) if self.path == "/status" else self.reply(404, {"error": "Unknown command"})

            def do_POST(self):
                if not self.authenticated():
                    return
                if self.path == "/close":
                    with broker.state_lock:
                        broker.stop.set()
                    self.reply(200, {"status": "closing", "message": "Closing after the active request; pending sends cancelled"})
                    return
                if self.path != "/replay":
                    self.reply(404, {"error": "Unknown command"})
                    return
                try:
                    self.connection.settimeout(5)
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        raise ValueError("Invalid command size")
                    options = json.loads(self.rfile.read(size).decode("utf-8"))
                    allowed = {"config_path", "source", "source_format", "limit", "output", "endpoint_groups",
                               "max_tokens", "no_max_tokens", "log_dir"}
                    if not isinstance(options, dict) or set(options) - allowed:
                        raise ValueError("Unsupported batch options")
                except (ValueError, UnicodeError, OSError) as error:
                    self.reply(400, {"error": str(error)})
                    return
                self.connection.settimeout(None)
                if not broker.job_lock.acquire(blocking=False):
                    self.reply(409, {"error": "Another batch is running; no new request was sent"})
                    return
                try:
                    with broker.state_lock:
                        if broker.stop.is_set():
                            self.reply(409, {"error": "Session is closing; no request was sent"})
                            return
                        broker.busy = True
                        broker.last_activity = broker.clock()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.end_headers()
                    stream = EventStream(self, broker.session._key)
                    try:
                        from .replay_litellm import replay_and_send
                        options.setdefault("config_path", broker.config_path)
                        with redirect_stdout(stream), redirect_stderr(stream):
                            result = replay_and_send(**options, connected_session=broker.session,
                                connected_monitor=broker.monitor, cancelled=broker.stop.is_set,
                                progress=lambda n: print(f"Profiled {n} original requests", flush=True))
                        stream.event({"event": "result", "sent_count": result.sent_count,
                                      "prepared_count": result.prepared_count, "log_path": str(result.log_path),
                                      "rejected_count": result.simulation.summary["rejected_requests"]})
                    except Exception as error:
                        try:
                            stream.event({"event": "error", "error_type": type(error).__name__, "message": str(error)})
                        except OSError:
                            pass  # Client disconnected: never restart or resend its job.
                finally:
                    # Complete request bodies are needed only during this batch;
                    # quota windows and performance history remain on the monitor.
                    with broker.monitor.lock:
                        broker.monitor.requests.clear()
                    with broker.state_lock:
                        broker.busy = False
                        broker.last_activity = broker.clock()
                    broker.job_lock.release()
        return Handler

    def serve(self, owner):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            write_json(self.directory / "session.json", {"schema_version": 1, "pid": os.getpid(),
                "control_port": self.server.server_address[1], "started_at": datetime.now(timezone.utc).isoformat(),
                **self.status()})
            while not self.stop.wait(.1):
                if not owner.alive() or self.idle_expired():
                    self.stop.set()
            # Keep monitor and tunnel alive until the accepted job has drained.
            with self.job_lock:
                pass
        finally:
            self.stop.set()
            self.server.shutdown()
            self.server.server_close()
            thread.join()


def worker(bootstrap):
    directory = Path(bootstrap["directory"])
    owner = session = None
    try:
        owner = WindowsProcessOwner(bootstrap["ssh_pid"])
        session = LiteLLMSession(bootstrap["ssh_target"], timeout=bootstrap["timeout"])
        session.adopt_connection(bootstrap["tunnel_port"], bootstrap.pop("api_key"))
        write_json(directory / ".session.lock", {"pid": os.getpid()})
        settings = load_scheduler_settings(bootstrap["config_path"])
        with EndpointMonitor(session, bootstrap["endpoint_state_dir"], settings,
                update_interval_seconds=bootstrap["endpoint_update_interval"],
                limit_overrides=bootstrap["endpoint_limit_overrides"]) as monitor:
            load_config(bootstrap["config_path"], endpoint_configs=monitor.endpoint_configs())
            SessionBroker(session, monitor, directory, bootstrap["token"], bootstrap["config_path"],
                          bootstrap["idle_timeout"]).serve(owner)
    except Exception as error:
        message = str(error)
        if session is not None and session._key:
            message = message.replace(session._key, "[REDACTED]")
        write_json(directory / "startup_error.json", {"error_type": type(error).__name__, "message": message})
    finally:
        try:
            try:
                if session is not None:
                    session.__exit__()
            finally:
                if owner is not None:
                    owner.close()
        finally:
            cleanup_control(directory)


def login(*, directory, ssh_target, config_path=CONFIG_PATH, idle_timeout=600, timeout=60,
          proxy_port=4000, endpoint_state_dir=RESULTS / "endpoints", endpoint_update_interval=30,
          endpoint_limit_overrides=None):
    validate_timeout(idle_timeout)
    validate_timeout(timeout)
    validate_timeout(endpoint_update_interval)
    load_scheduler_settings(config_path)  # Validate before interactive login.
    directory = Path(directory).resolve()
    state_directory = Path(endpoint_state_dir).resolve()
    if directory.is_relative_to(state_directory) or state_directory.is_relative_to(directory):
        raise ValueError("Session control and endpoint state directories must be separate, non-nested directories")
    if endpoint_limit_overrides is not None:
        if not isinstance(endpoint_limit_overrides, dict) or set(endpoint_limit_overrides) - {"rpm_limit", "tpm_limit", "concurrency_limit"}:
            raise ValueError("Invalid endpoint capacity overrides")
        for name, value in endpoint_limit_overrides.items():
            positive_integer(value, name)
    if Path(config_path).resolve() in {Path(endpoint_state_dir).resolve() / name
                                     for name in ("endpoint_catalog_raw.json", "endpoint_state.json")}:
        raise ValueError("Endpoint state must not overwrite configuration")
    directory = private_directory(directory)
    try:
        with (directory / ".session.lock").open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid()}, stream)
    except FileExistsError:
        try:
            result = control(directory, "status")
        except OSError as error:
            raise OSError("Session is starting or left a stale lock; verify the old process has exited before removing .session.lock") from error
        if result["status"] != "ready":
            raise OSError("Session is closing; wait for shutdown before logging in again")
        if result["ssh_target"] != ssh_target or result["config_path"] != str(Path(config_path).resolve()):
            raise ValueError("An existing session uses a different target/config; close it before logging in again")
        print("A session is already open; use litellm-status or litellm-close. Existing login settings remain in effect.")
        return result
    child = None
    lease = TunnelLease()
    try:
        (directory / "startup_error.json").unlink(missing_ok=True)
        token = secrets.token_hex(32)
        (directory / "control.key").write_text(token, encoding="ascii")
        with LiteLLMSession(ssh_target, proxy_port=proxy_port, timeout=timeout, tunnel_lease=lease) as session:
            bootstrap = {"directory": str(directory), "api_key": session._key, "token": token,
                "ssh_pid": lease.process.pid, "tunnel_port": session._port, "ssh_target": ssh_target,
                "timeout": timeout, "config_path": str(Path(config_path).resolve()), "idle_timeout": idle_timeout,
                "endpoint_state_dir": str(Path(endpoint_state_dir).resolve()),
                "endpoint_update_interval": endpoint_update_interval, "endpoint_limit_overrides": endpoint_limit_overrides}
            environment = dict(os.environ)
            environment.pop("KEY", None)
            environment["PYTHONIOENCODING"] = "utf-8"
            with (directory / "worker.log").open("wb") as output:
                child = subprocess.Popen([sys.executable, "-m", __name__, "--worker"], cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=output, stderr=output, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            child.stdin.write((json.dumps(bootstrap, ensure_ascii=False) + "\n").encode("utf-8"))
            child.stdin.close()
            bootstrap.pop("api_key")
            deadline = time.monotonic() + timeout + 5
            while time.monotonic() < deadline:
                if (directory / "session.json").exists():
                    result = control(directory, "status")
                    if child.poll() is None and result["status"] == "ready":
                        lease.detached = True
                        return result
                if child.poll() is not None:
                    error = directory / "startup_error.json"
                    detail = json.loads(error.read_text(encoding="utf-8"))["message"] if error.exists() else "See worker.log"
                    raise OSError("Session startup failed: " + detail)
                time.sleep(.1)
            raise TimeoutError("Session startup timed out; no model request was sent")
    finally:
        if lease.detached:
            # The background owner intentionally outlives this Popen object.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                child = None
        else:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            if child is not None:
                lock = Path(endpoint_state_dir) / ".endpoint_monitor.lock"
                if lock.exists() and lock.read_text(encoding="utf-8") == str(child.pid):
                    lock.unlink()
            cleanup_control(directory)


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("Use python -m workload_profiling litellm-login")
    worker(json.loads(sys.stdin.buffer.readline(65536).decode("utf-8")))
