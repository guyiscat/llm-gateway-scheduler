"""Shared millisecond summaries; P95 uses the nearest-rank convention."""
import math


def summarize_ms(values):
    values = sorted(values)
    if not values:
        return {"mean": 0, "p95": 0, "max": 0}
    return {"mean": sum(values) / len(values),
            "p95": values[math.ceil(.95 * len(values)) - 1], "max": values[-1]}
