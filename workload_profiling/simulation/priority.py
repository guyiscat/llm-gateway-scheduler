"""Reproducible high/low priorities, independent of request length."""
from dataclasses import replace
import hashlib
import json


def assign_priority(request, config):
    if request.priority is not None:
        return request
    if config.priority_assignment == "binary":
        key = json.dumps([config.priority_seed, request.request_id], separators=(",", ":")).encode("utf-8")
        draw = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
        priority = int(draw < config.high_priority_ratio * 2 ** 64)
        return replace(request, priority=priority)
    return replace(request, priority=0)


def priority_fields(row):
    """Preserve explicit binary priority and reject obsolete multi-level input."""
    if "priority_level" in row or "priority_level_source" in row:
        raise ValueError("Legacy priority levels are unsupported; use priority=0 or priority=1")
    return {"priority": row["priority"]} if "priority" in row else {}
