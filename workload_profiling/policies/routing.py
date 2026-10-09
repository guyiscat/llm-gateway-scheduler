"""Replaceable routing: select from feasible immutable endpoint snapshots."""
from __future__ import annotations
import importlib
import inspect
from typing import Protocol

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..core.endpoint_state import EndpointView
    from ..core.request import SchedulerRequest as WorkloadRequest


class RoutingStrategy(Protocol):
    def select(self, request: WorkloadRequest, endpoints: tuple[EndpointView, ...], now_ms: int) -> str:
        """Return one candidate endpoint_id. Candidates already satisfy all limits."""
        ...


class EndpointSelectionPolicy(Protocol):
    def select_endpoint(self, request, endpoints, context) -> str:
        """Select using candidates, static capabilities/prices and recent history."""
        ...


class MinRpmStrategy:
    def select_endpoint(self, request, endpoints, context):
        return self.select(request, endpoints, context.now_ms)

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
    if inspect.isclass(strategy) or not any(hasattr(strategy, n) for n in ("select", "select_endpoint")):
        strategy = strategy()
    select = getattr(strategy, "select_endpoint", None) or getattr(strategy, "select", None)
    if not callable(select):
        raise TypeError("Routing strategy must implement select_endpoint(request, endpoints, context) or select")
    if inspect.iscoroutinefunction(select):
        raise TypeError("Routing strategy must be synchronous")
    return strategy
