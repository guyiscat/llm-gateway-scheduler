"""Deterministic burst schedule, preserving count, order and first/last arrival."""


def burst_arrival_times(count, config):
    if count <= 1:
        return tuple(0 for _ in range(count))
    raw = []
    for start in range(0, count, config.burst_size):
        size = min(config.burst_size, count - start)
        span = min(config.burst_span_ms, (size - 1) * config.arrival_interval_ms)
        raw.extend(start * config.arrival_interval_ms + i * span // max(1, size - 1)
                   for i in range(size))
    # Last partial group must not reduce the whole observation span.
    target_end = (count - 1) * config.arrival_interval_ms
    if raw[-1] == 0:
        # All requests in one instantaneous burst: preserve the last endpoint.
        return tuple([0] * (count - 1) + [target_end])
    return tuple(time * target_end // raw[-1] for time in raw)
