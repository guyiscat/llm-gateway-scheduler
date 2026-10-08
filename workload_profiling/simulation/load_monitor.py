"""Observable endpoint load; system busy means every endpoint is busy."""
from dataclasses import asdict, dataclass

from .models import EndpointView


@dataclass(frozen=True)
class EndpointLoad:
    endpoint_id: str
    busy: bool
    reasons: tuple[str, ...]
    rpm_utilization: float
    tpm_utilization: float
    concurrency: int
    concurrency_busy_at: int


@dataclass(frozen=True)
class SystemLoad:
    endpoints: tuple[EndpointLoad, ...]

    @property
    def busy(self):
        return all(endpoint.busy for endpoint in self.endpoints)

    @property
    def non_busy_endpoint_ids(self):
        return tuple(endpoint.endpoint_id for endpoint in self.endpoints if not endpoint.busy)

    def record(self):
        return {"system_busy": self.busy, "endpoints": [asdict(endpoint) for endpoint in self.endpoints]}


class LoadMonitor:
    def __init__(self, config):
        self.rpm_threshold = config.busy_rpm_threshold
        self.tpm_threshold = config.busy_tpm_threshold
        self.concurrency_reserve = config.busy_concurrency_reserve

    def endpoint_load(self, endpoint: EndpointView):
        concurrency_busy_at = endpoint.concurrency_limit - self.concurrency_reserve
        reasons = []
        if endpoint.rpm_utilization >= self.rpm_threshold:
            reasons.append("rpm")
        if endpoint.tpm_utilization >= self.tpm_threshold:
            reasons.append("tpm")
        if endpoint.concurrency >= concurrency_busy_at:
            reasons.append("concurrency")
        return EndpointLoad(endpoint.endpoint_id, bool(reasons), tuple(reasons),
                            endpoint.rpm_utilization, endpoint.tpm_utilization,
                            endpoint.concurrency, concurrency_busy_at)

    def snapshot(self, endpoints):
        endpoints = tuple(endpoints)
        if not endpoints:
            raise ValueError("Load monitoring requires at least one endpoint")
        return SystemLoad(tuple(self.endpoint_load(endpoint) for endpoint in endpoints))
