"""Reproducible four-level request priorities, independent of token lengths."""
from dataclasses import replace
import hashlib
import json


def assign_priority(request, config):
    if config.priority_assignment == "four_level":
        # Independent of token lengths and read order.
        if request.priority_level_source != "default" or request.priority_level != 1:
            return request
        key = json.dumps([config.priority_seed, request.request_id], separators=(",", ":")).encode("utf-8")
        level = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 4 + 1
        return replace(request, priority_level=level, priority_level_source="synthetic")
    return request


def priority_fields(row):
    """Optional explicit source labels; never infer business priority from length."""
    fields = {}
    if "priority_level" in row:
        fields.update(priority_level=row["priority_level"], priority_level_source="recorded")
    return fields


def heavy_for(request, config):
    return (request.input_tokens >= config.input_threshold_tokens
            or request.output_tokens >= config.output_threshold_tokens)


