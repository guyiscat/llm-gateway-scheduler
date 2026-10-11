"""Send routed handoffs to LiteLLM over a reusable authenticated SSH session.

No SDK or third-party SSH dependency. Credentials are read from the terminal
or KEY environment variable, never stored in files or process arguments.
"""
import codecs
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import warnings
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from ..common.paths import CACHE

KNOWN_HOSTS = CACHE / "litellm_known_hosts"
CALL_FIELDS = {"model", "messages", "max_tokens", "stream", "metadata", "temperature",
    "top_p", "tools", "tool_choice", "parallel_tool_calls", "response_format", "stop",
    "seed", "user", "stream_options"}


def iter_routes(path):
    """Read validated handoffs in their recorded dispatch order."""
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Route on line {line_number} must be an object")
            if not isinstance(record.get("litellm_params"), dict):
                raise ValueError("Route must contain litellm_params")
            for field in ("request_id", "selected_endpoint_id"):
                if not isinstance(record.get(field), str) or not record[field].strip():
                    raise ValueError(f"Route must contain a nonempty {field}")
            yield record


def validate_payload_options(*, endpoint_groups=None, max_tokens=None, no_max_tokens=False):
    """Validate shared delivery controls without inventing a request model."""
    if not isinstance(no_max_tokens, bool):
        raise ValueError("no_max_tokens must be boolean")
    if no_max_tokens and max_tokens is not None:
        raise ValueError("max_tokens and no_max_tokens are mutually exclusive")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0):
        raise ValueError("max_tokens must be a positive integer")
    if endpoint_groups is not None and (not isinstance(endpoint_groups, (list, tuple)) or not endpoint_groups or
            any(not isinstance(g, str) or not g.strip() for g in endpoint_groups)):
        raise ValueError("endpoint_groups must be a nonempty list or tuple of nonempty strings")


def build_payload(route, *, endpoint_groups=None, max_tokens=None, no_max_tokens=False):
    """Use each request's model; optional groups do not bind local endpoint IDs."""
    validate_payload_options(endpoint_groups=endpoint_groups, max_tokens=max_tokens, no_max_tokens=no_max_tokens)
    params = route["litellm_params"]
    payload = {key: deepcopy(value) for key, value in params.items() if key in CALL_FIELDS}
    # Modern handoffs carry the exact SchedulerRequest model. Older handoffs
    # can use their recorded model, but a simulation placeholder is invalid.
    if "target_model" in route:
        payload["model"] = route["target_model"]
    if not isinstance(payload.get("model"), str) or not payload["model"].strip() or payload["model"] == "default":
        raise ValueError("Route must specify a valid target_model; regenerate legacy default routes")
    metadata = payload.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    if endpoint_groups is not None:
        metadata["endpoint_group"] = list(endpoint_groups)
    groups = metadata.get("endpoint_group")
    if "endpoint_group" in metadata and (not isinstance(groups, list) or not groups or
            any(not isinstance(g, str) or not g.strip() for g in groups)):
        raise ValueError("endpoint_group must be a nonempty list of nonempty strings")
    # Old ID-as-model handoffs need a public model and an explicit group before
    # they can safely be sent through the deployed policy shell.
    if params.get("model") == route.get("selected_endpoint_id") and (not route.get("target_model") or groups is None):
        raise ValueError("Legacy deployment-ID handoff lacks public-model/group binding; regenerate routes with replay-send")
    metadata.pop("force_endpoint", None)
    if not metadata:
        payload.pop("metadata", None)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
        raise ValueError("A real call requires a nonempty messages list")
    if not isinstance(payload.get("stream", False), bool):
        raise ValueError("stream must be boolean")
    if no_max_tokens:
        payload.pop("max_tokens", None)
    elif max_tokens is not None:
        payload["max_tokens"] = max_tokens
    elif payload.get("max_tokens") is None:
        payload.pop("max_tokens", None)
    limit = payload.get("max_tokens")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise ValueError("Route max_tokens must be a positive integer or null")
    # LiteLLM router controls: one attempt and no cross-group fallback chain.
    payload.update(num_retries=0, fallbacks=[], context_window_fallbacks=[], content_policy_fallbacks=[])
    json.dumps(payload, allow_nan=False)
    return payload


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def send_request(payload, api_key, *, port, timeout=60, opener=None, on_response_chunk=None):
    """One explicit POST per call, without automatic retries or persistent locks."""
    if on_response_chunk is not None and not callable(on_response_chunk):
        raise TypeError("on_response_chunk must be callable")
    validate_api_key(api_key)
    validate_timeout(timeout)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("Invalid local tunnel port")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "x-litellm-num-retries": "0"})
    # Ignore machine-wide HTTP proxy settings; traffic must go into this tunnel.
    client = opener if opener is not None else build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = client.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        status = response.code
        all_headers = {name.lower(): value for name, value in response.headers.items()}
        headers = {name: value for name, value in all_headers.items()
                   if name in {"x-litellm-model-api-base", "x-litellm-attempted-retries", "x-litellm-model-id"}}
        content_type = all_headers.get("content-type", "").lower()
        streaming = content_type.startswith("text/event-stream") or (
            payload.get("stream", False) and 200 <= status < 300 and not content_type)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts = []
        # read1 avoids buffering an entire SSE stream before showing the output.
        read = getattr(response, "read1", response.read)
        while True:
            chunk = read(65536)
            text = decoder.decode(chunk, final=not chunk)
            parts.append(text)
            if streaming and text and on_response_chunk is not None:
                on_response_chunk(text)
            if not chunk:
                break
    response_body = "".join(parts)
    response_format = "sse" if streaming else "text"
    if not streaming:
        try:
            response_body = json.loads(response_body)
            response_format = "json"
        except ValueError:
            pass
    return {"http_status": status, **headers, "response_format": response_format, "response": response_body}


def validate_api_key(api_key):
    if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
        raise ValueError("KEY must be a nonempty single-line credential")


def validate_timeout(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be finite and positive")


class LiteLLMSession:
    """Own one tunnel and credential for repeated sequential requests.

    The session neither reconnects nor resends automatically after failure.
    """
    def __init__(self, target, *, proxy_port=4000, timeout=60, run_log=None, tunnel_lease=None):
        validate_timeout(timeout)
        self.target, self.proxy_port, self.timeout = target, proxy_port, timeout
        self.run_log = run_log
        self.tunnel_lease = tunnel_lease
        self._stack = self._port = self._key = self._opener = None

    def __enter__(self):
        if self._stack is not None:
            raise RuntimeError("Session is already open")
        stack = ExitStack()
        try:
            options = {"proxy_port": self.proxy_port, "timeout": self.timeout}
            if self.tunnel_lease is not None:
                options["lease"] = self.tunnel_lease
            port = stack.enter_context(ssh_tunnel(self.target, **options))
            key = os.environ.get("KEY") or getpass.getpass("LiteLLM API key (hidden): ")
            validate_api_key(key)
            if self.run_log is not None:
                self.run_log.register_secret(key)
            opener = build_opener(ProxyHandler({}), NoRedirect())
        except BaseException:
            stack.close()
            raise
        self._stack, self._port, self._key, self._opener = stack, port, key, opener
        return self

    def adopt_connection(self, port, api_key):
        """Use an authenticated tunnel whose process is owned by the caller."""
        if self._stack is not None:
            raise RuntimeError("Session is already open")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Invalid local tunnel port")
        validate_api_key(api_key)
        self._stack, self._port, self._key = ExitStack(), port, api_key
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        return self

    @contextmanager
    def recording(self, run_log):
        """Associate a reusable connection with one fresh batch log."""
        previous = self.run_log
        self.run_log = run_log
        if self._key is not None:
            run_log.register_secret(self._key)
        try:
            yield self
        finally:
            self.run_log = previous

    def __exit__(self, *exception):
        stack = self._stack
        self._stack = self._port = self._key = self._opener = None
        if stack is not None:
            return stack.__exit__(*(exception or (None, None, None)))

    def get_model_info(self):
        """Authenticated metadata GET through the already logged-in tunnel."""
        if self._stack is None:
            raise RuntimeError("Session is not open")
        request = Request(f"http://127.0.0.1:{self._port}/model/info", method="GET",
                          headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
        with self._opener.open(request, timeout=self.timeout) as response:
            if not 200 <= response.code < 300:
                raise OSError(f"LiteLLM model info HTTP {response.code}")
            return json.loads(response.read().decode("utf-8"))

    def send(self, payload, *, on_response_chunk=None, request_id=None, selected_endpoint_id=None):
        if self._stack is None:
            raise RuntimeError("Session is not open")
        if on_response_chunk is not None and not callable(on_response_chunk):
            raise TypeError("on_response_chunk must be callable")
        if self.run_log is None:
            return send_request(payload, self._key, port=self._port, timeout=self.timeout,
                                opener=self._opener, on_response_chunk=on_response_chunk)
        if request_id is None:
            request_id = f"request_{uuid4().hex}"
        self.run_log.record("litellm_send_started", request_id=request_id,
                            selected_endpoint_id=selected_endpoint_id, payload=payload)
        def chunk_received(text):
            self.run_log.record("litellm_stream_chunk", request_id=request_id, text=text)
            if on_response_chunk is not None:
                on_response_chunk(text)
        try:
            result = send_request(payload, self._key, port=self._port, timeout=self.timeout,
                                  opener=self._opener, on_response_chunk=chunk_received)
        except BaseException as error:
            self.run_log.record("litellm_send_failed", request_id=request_id,
                                error_type=type(error).__name__, error=str(error))
            raise
        self.run_log.record("litellm_response", request_id=request_id,
                            selected_endpoint_id=selected_endpoint_id, result=result)
        return result


def display_result(result):
    # SSE was already displayed live; don't print its full transcript twice.
    output = {k: v for k, v in result.items() if k != "response"} if result["response_format"] == "sse" else result
    print(json.dumps(output, ensure_ascii=False, indent=2))


def display_response_chunk(text):
    """Print raw SSE events immediately, including content, usage and DONE."""
    sys.stdout.write(text)
    sys.stdout.flush()


def ssh_executable():
    if os.name == "nt":
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/OpenSSH/ssh.exe"
        if executable.is_file():
            return str(executable)
    executable = shutil.which("ssh")
    if executable is None:
        raise OSError("OpenSSH client is required")
    return executable


@contextmanager
def ssh_tunnel(target, *, proxy_port=4000, timeout=60, lease=None):
    """Let OpenSSH authenticate interactively; bind only local loopback."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*", target):
        raise ValueError("SSH target must be user@host")
    validate_timeout(timeout)
    if not isinstance(proxy_port, int) or isinstance(proxy_port, bool) or not 1 <= proxy_port <= 65535:
        raise ValueError("Invalid remote proxy port")
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [ssh_executable(), "-N", "-T", "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{port}:127.0.0.1:{proxy_port}", target]
    # Preserve interactive password entry, but protect a transferred tunnel
    # from Ctrl+C in later batch commands in the same Windows console.
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if lease is not None and os.name == "nt" else {}
    process = subprocess.Popen(command, **options)
    if lease is not None:
        lease.process = process
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OSError(f"SSH exited before tunnel startup (code {process.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise TimeoutError("SSH tunnel startup timed out")
        yield port
    finally:
        if lease is not None and lease.detached:
            # The worker already owns a process HANDLE; release parent wrappers.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                lease.process = None
                process = None
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
