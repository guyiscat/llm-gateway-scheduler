"""Durable JSONL events for normalized requests and real model deliveries."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import hashlib
import re
from uuid import uuid4

from .paths import RESULTS


class RunLog:
    """One exclusive directory per run, with a separate file for each request.

    Credentials are redacted; user messages and model responses are retained.
    This is an execution trace, not the atomically published route handoff.
    """
    def __init__(self, path=None, *, mode="replay", protected_paths=()):
        self.run_id = uuid4().hex
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        supplied = Path(path).resolve() if path is not None else None
        # Compatibility with the previous --log-file/API: use its stem as a
        # directory name, never create a directory disguised as a JSONL file.
        self.path = (supplied.with_suffix("") if supplied is not None and supplied.suffix.lower() == ".jsonl"
                     else supplied) or RESULTS / "logs" / f"{stamp}_{self.run_id}"
        self.path = self.path.resolve()
        for protected in protected_paths:
            protected = Path(protected).resolve()
            if (supplied == protected or protected == self.path or protected.is_relative_to(self.path)
                    or (supplied is not None and supplied.exists() and protected.exists() and os.path.samefile(supplied, protected))):
                raise ValueError("Run log must not overwrite the source or route output")
        if supplied is not None and supplied != self.path and supplied.exists():
            raise FileExistsError(f"Log path already exists: {supplied}")
        self.mode = mode
        self.run_file = self.path / "run.jsonl"
        self._active = False
        self._used = False
        self._secrets = []
        self._files = set()
        self._sequence = 0

    @staticmethod
    def request_filename(request_id):
        """Keep generated IDs readable; safely encode arbitrary upstream IDs."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a nonempty string")
        if re.fullmatch(r"request_[0-9]{1,80}", request_id):
            return f"{request_id}.jsonl"
        readable = re.sub(r"[^A-Za-z0-9_-]", "_", request_id)[:60] or "id"
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
        return f"request_{readable}_{digest}.jsonl"

    def request_path(self, request_id):
        return self.path / "requests" / self.request_filename(request_id)

    def register_secret(self, secret):
        if isinstance(secret, str) and secret and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def _redact(self, value):
        if isinstance(value, dict):
            credential_fields = {"api_key", "apikey", "authorization", "password", "ssh_password", "access_token", "api_token"}
            return {key: "[REDACTED]" if isinstance(key, str) and key.lower() in credential_fields else self._redact(item)
                    for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    def __enter__(self):
        if self._used:
            raise RuntimeError("Create a new RunLog for each run")
        self._used = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never reuse an existing run directory or append to earlier runs.
        self.path.mkdir()
        (self.path / "requests").mkdir()
        self._active = True
        try:
            self.record("run_started", mode=self.mode)
        except BaseException:
            self._active = False
            raise
        return self

    def record(self, event, *, request_id=None, **fields):
        if not self._active:
            raise RuntimeError("RunLog must be used inside its context")
        row = {"schema_version": 2, "run_id": self.run_id,
               "timestamp": datetime.now(timezone.utc).isoformat(), "sequence": self._sequence, "event": event}
        if request_id is not None:
            row["request_id"] = request_id
        row.update(fields)
        path = self.run_file if request_id is None else self.request_path(request_id)
        line = json.dumps(self._redact(row), ensure_ascii=False, allow_nan=False) + "\n"
        # Close after every event so large replays do not exhaust file handles.
        with path.open("a" if path in self._files else "x", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self._files.add(path)
        self._sequence += 1

    def scheduler_request(self, request):
        """Capture the normalized, validated request before core submission."""
        self.record("scheduler_request", request_id=request.request_id, scheduler_request=asdict(request))

    def simulation_result(self, result):
        for row in result.requests:
            self.record("simulation_outcome", request_id=row["request_id"],
                        simulation_status=row["status"], selected_endpoint_id=row["endpoint_id"],
                        rejection_reason=row["rejection_reason"], dispatched_at_ms=row["dispatch_at_ms"])

    def __exit__(self, error_type, error, traceback):
        try:
            status = "completed" if error_type is None else (
                "cancelled" if issubclass(error_type, (KeyboardInterrupt, EOFError)) else "failed")
            fields = {"status": status, "request_log_count": len(self._files - {self.run_file})}
            if error_type is not None:
                fields.update(error_type=error_type.__name__, error=str(error))
            self.record("run_finished", **fields)
        finally:
            self._active = False
            self._secrets.clear()
        return False
