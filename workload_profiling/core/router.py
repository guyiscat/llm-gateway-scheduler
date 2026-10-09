"""One routing entry for all scheduling paths; algorithms receive history context."""
from dataclasses import dataclass
import inspect

from ..policies.routing import load_strategy
from .request import RouteDecision


@dataclass(frozen=True)
class RoutingContext:
    now_ms: int
    endpoint_configs: dict
    performance: dict


class Router:
    def __init__(self, state_manager, policy="min_rpm"):
        self.states = state_manager
        self.policy = load_strategy(policy) if isinstance(policy, str) else policy
        selector = getattr(self.policy, "select_endpoint", None) or getattr(self.policy, "select", None)
        if not callable(selector) or inspect.iscoroutinefunction(selector):
            raise TypeError("Routing policy must implement a synchronous selector")

    def route(self, request, candidates, now_ms):
        candidates = tuple(candidates)
        if not candidates:
            return None
        ids = tuple(e.endpoint_id for e in candidates)
        context = RoutingContext(now_ms, {i: self.states.configs[i] for i in ids},
                                 self.states.history.snapshot(ids, now_ms))
        selector = getattr(self.policy, "select_endpoint", None)
        selected = (selector(request, candidates, context) if selector else
                    self.policy.select(request, candidates, now_ms))
        if not isinstance(selected, str) or selected not in ids:
            raise ValueError("Routing strategy selected an unavailable or unknown endpoint")
        return RouteDecision(request, selected, now_ms, ids)
