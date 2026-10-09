"""Choose immediate or window scheduling, independently of endpoint scoring."""
from dataclasses import dataclass

from .busy_detector import LoadMonitor


@dataclass(frozen=True)
class AdmissionDecision:
    scheduling_path: str
    endpoint_scope: str


class AdmissionPolicy:
    def __init__(self, config):
        self.load_monitor = LoadMonitor(config)

    def decide(self, request, load):
        if request.priority == 1:
            return AdmissionDecision("priority_immediate", "all")
        if load.busy:
            return AdmissionDecision("busy_window", "all")
        return AdmissionDecision("idle_immediate", "non_busy")

    def routing_endpoints(self, endpoints, scope):
        if scope == "all":
            return tuple(endpoints)
        if scope != "non_busy":
            raise ValueError("Endpoint scope must be all or non_busy")
        return tuple(endpoint for endpoint in endpoints
                     if not self.load_monitor.endpoint_load(endpoint).busy)
