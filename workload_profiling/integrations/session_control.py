"""Private loopback control channel; no model credentials are stored on disk."""
import ctypes
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from ..common.paths import CACHE

SESSION_DIR = CACHE / "litellm_session"
SESSION_FILES = {".session.lock", "control.key", "session.json", "worker.log", "startup_error.json"}


def private_directory(directory):
    directory = Path(directory).resolve()
    if directory.exists() and any(item.name not in SESSION_FILES for item in directory.iterdir()):
        raise ValueError("Session directory must be empty or contain only session control files")
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user"], capture_output=True, check=True)
        sid = re.search(rb"S-1-\d+(?:-\d+)+", identity.stdout)
        if sid is None:
            raise OSError("Cannot determine current Windows user SID")
        result = subprocess.run(["icacls", str(directory), "/inheritance:r", "/grant:r",
                                 "*" + sid.group().decode("ascii") + ":(OI)(CI)F"], capture_output=True)
        if result.returncode:
            raise OSError("Cannot protect session control directory for the current user")
    else:
        directory.chmod(0o700)
    return directory


class TunnelLease:
    def __init__(self):
        self.process = None
        self.detached = False


class WindowsProcessOwner:
    """Retain an SSH process HANDLE, avoiding termination of a reused PID."""
    def __init__(self, pid):
        if os.name != "nt":
            raise OSError("Background SSH session transfer currently requires Windows")
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.api.TerminateProcess.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.api.OpenProcess(0x00100001, False, pid)
        if not self.handle:
            raise OSError("Cannot adopt authenticated SSH process")
        if not self.alive():
            self.close()
            raise OSError("SSH process exited during login")

    def alive(self):
        return self.handle is not None and self.api.WaitForSingleObject(self.handle, 0) == 258

    def close(self):
        if self.handle is not None:
            try:
                if self.alive():
                    if not self.api.TerminateProcess(self.handle, 0):
                        raise OSError("Cannot close owned SSH process")
                    self.api.WaitForSingleObject(self.handle, 5000)
            finally:
                self.api.CloseHandle(self.handle)
                self.handle = None


def connection(directory):
    directory = Path(directory).resolve()
    try:
        state = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        token = (directory / "control.key").read_text(encoding="ascii")
    except (OSError, ValueError) as error:
        raise OSError("No ready LiteLLM session; run litellm-login first") from error
    if not isinstance(state, dict):
        raise ValueError("Invalid local session control state")
    port = state.get("control_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 or not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("Invalid local session control state")
    return HTTPConnection("127.0.0.1", port, timeout=5), token


def control(directory, action):
    client, token = connection(directory)
    try:
        client.request("POST" if action == "close" else "GET", "/" + action,
                       headers={"Authorization": "Bearer " + token})
        response = client.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            raise OSError(result.get("error", "Session control request failed"))
        return result
    except OSError as error:
        raise OSError("Session unavailable or closed; run litellm-login again") from error
    finally:
        client.close()


def send_batch(directory, options):
    """Consume live stdout/SSE and final status; never retry a disconnected job."""
    client, token = connection(directory)
    finished = False
    try:
        client.request("POST", "/replay", json.dumps(options, allow_nan=False).encode("utf-8"),
                       {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        # A replay can outlast the per-model HTTP timeout. Ctrl+C remains usable.
        if client.sock is not None:
            client.sock.settimeout(None)
        response = client.getresponse()
        if response.status != 200:
            finished = True  # Explicit rejection is not an uncertain delivery.
            raise OSError(json.loads(response.read().decode("utf-8")).get("error", "Batch rejected"))
        while line := response.readline():
            event = json.loads(line.decode("utf-8"))
            if event["event"] == "output":
                sys.stdout.write(event["text"])
                sys.stdout.flush()
            elif event["event"] == "error":
                finished = True
                raise RuntimeError(event["message"])
            elif event["event"] == "result":
                finished = True
                return event
        raise OSError("Session connection ended without a final batch result; not retried")
    except (OSError, ValueError) as error:
        if not finished:
            raise OSError("Batch connection interrupted; outcome may be partial. Check request logs; not retried") from error
        raise
    finally:
        client.close()
