"""Reproducible binary or legacy four-level priorities, outside the core."""
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
        return replace(request, priority=priority, priority_level=4 if priority else 1, priority_level_source="synthetic")
    if config.priority_assignment == "four_level":
        # Independent of token lengths and read order.
        if request.priority_level_source != "default" or request.priority_level != 1:
            return request
        key = json.dumps([config.priority_seed, request.request_id], separators=(",", ":")).encode("utf-8")
        level = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 4 + 1
        return replace(request, priority_level=level, priority_level_source="synthetic")
    return request


def priority_fields(row):
    """Preserve explicit numeric levels; never infer priority from length."""
    fields = {"priority": row["priority"]} if "priority" in row else {}
    if "priority_level" in row:
        fields.update(priority_level=row["priority_level"], priority_level_source="recorded")
    return fields
