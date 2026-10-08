"""Replaceable routing: select from feasible immutable endpoint snapshots."""
import importlib
import inspect
from typing import Protocol

from .models import EndpointView, WorkloadRequest


class RoutingStrategy(Protocol):
    def select(self, request: WorkloadRequest, endpoints: tuple[EndpointView, ...], now_ms: int) -> str:
        """Return one candidate endpoint_id. Candidates already satisfy all limits."""
        ...


class MinRpmStrategy:
    def select(self, request, endpoints, now_ms):
        if not endpoints:
            raise ValueError("Routing requires at least one feasible endpoint")
        # Stable ties follow configuration order. State updates after every assignment.
        return min(endpoints, key=lambda endpoint: endpoint.rpm_utilization).endpoint_id


def load_strategy(spec):
    if spec == "min_rpm":
        return MinRpmStrategy()
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("strategy must be min_rpm or importable.module:attribute")
    try:
        strategy = getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as error:
        raise ValueError(f"Cannot load routing strategy {spec}: {type(error).__name__}") from None
    if inspect.isclass(strategy) or not hasattr(strategy, "select"):
        strategy = strategy()
    if not callable(getattr(strategy, "select", None)):
        raise TypeError("Routing strategy must implement select(request, endpoints, now_ms)")
    if inspect.iscoroutinefunction(strategy.select):
        raise TypeError("Routing strategy must be synchronous")
    return strategy
