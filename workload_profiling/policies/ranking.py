"""Replaceable ordering of a released batch, independent of endpoint routing."""
from __future__ import annotations
from collections.abc import Sequence
import importlib
import inspect
from typing import Protocol

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..core.endpoint_state import EndpointView
    from ..core.request import SchedulerRequest as WorkloadRequest


class BatchOrderStrategy(Protocol):
    def order_batch(self, requests: tuple[WorkloadRequest, ...],
                    endpoints: tuple[EndpointView, ...], now_ms: int) -> Sequence[str]:
        """Return every batch request_id exactly once, in the desired order.

        Requests follow arrival order. Endpoints include ALL endpoint snapshots,
        including busy endpoints. Both tuples and their members are immutable.
        """
        ...


class FifoOrderStrategy:
    def rank_requests(self, requests, context=None):
        return tuple(requests)

    def order_batch(self, requests, endpoints, now_ms):
        return [request.request_id for request in requests]


class RankingPolicy(Protocol):
    def rank_requests(self, requests, context):
        """Return every request once, preserving request contents."""
        ...


class PriorityThenLightOrderStrategy:
    def __init__(self, config, classification_policy=None):
        self.config = config
        self.classification_policy = classification_policy

    def rank_requests(self, requests, context):
        by_id = {r.request_id: r for r in requests}
        return tuple(by_id[i] for i in self.order_batch(requests, context["endpoints"], context["now_ms"]))

    def order_batch(self, requests, endpoints, now_ms):
        def key(request):
            heavy = ((request.input_tokens >= self.config.input_threshold_tokens or request.output_tokens >= self.config.output_threshold_tokens) if self.classification_policy is None
                     else self.classification_policy.decisions[request.request_id]["heavy"])
            return -getattr(request, "priority_level", getattr(request, "priority", 0)), heavy
        # Stable ties retain arrival order within the same level and type.
        return [r.request_id for r in sorted(requests, key=key)]


class WeightedLengthOrderStrategy:
    def __init__(self, config):
        self.config = config

    def rank_requests(self, requests, context=None):
        return tuple(sorted(requests, key=lambda r: self.config.input_weight * r.input_tokens
                            + self.config.output_weight * r.output_tokens,
                            reverse=self.config.ranking_direction == "descending"))

    def order_batch(self, requests, endpoints, now_ms):
        return [r.request_id for r in self.rank_requests(requests)]


BUILTIN_BATCH_ORDERS = {
    "fifo": FifoOrderStrategy,
    "weighted_length": WeightedLengthOrderStrategy,
    "priority_then_light": PriorityThenLightOrderStrategy,
}


def load_batch_order(spec, *, config=None, classification_policy=None):
    if spec == "weighted_length":
        if config is None:
            raise ValueError("Weighted ordering requires settings")
        return WeightedLengthOrderStrategy(config)
    if spec == "priority_then_light":
        if config is None:
            raise ValueError("Priority ordering requires a SimulationConfig")
        return PriorityThenLightOrderStrategy(config, classification_policy)
    if spec in BUILTIN_BATCH_ORDERS:
        return BUILTIN_BATCH_ORDERS[spec]()
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("batch_order must be fifo, priority_then_light, weighted_length or module:attribute")
    try:
        order = getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as error:
        raise ValueError(f"Cannot load batch order {spec}: {type(error).__name__}") from None
    if inspect.isclass(order) or not any(hasattr(order, n) for n in ("order_batch", "rank_requests")):
        order = order()
    rank = getattr(order, "rank_requests", None) or getattr(order, "order_batch", None)
    if not callable(rank):
        raise TypeError("Batch order must implement rank_requests(requests, context) or order_batch")
    if inspect.iscoroutinefunction(rank):
        raise TypeError("Batch order must be synchronous")
    return order


def validate_batch_order(requests, ordered_ids):
    """Reject omissions, duplicates and foreign IDs before enqueuing the batch."""
    if not isinstance(ordered_ids, Sequence) or isinstance(ordered_ids, (str, bytes)):
        raise TypeError("order_batch must return a sequence of request_id strings (for example list or tuple)")
    ordered_ids = tuple(ordered_ids)
    if (len(ordered_ids) != len(requests)
            or any(not isinstance(request_id, str) for request_id in ordered_ids)
            or len(set(ordered_ids)) != len(ordered_ids)
            or set(ordered_ids) != {request.request_id for request in requests}):
        raise ValueError("order_batch must return every batch request_id exactly once, with no foreign IDs")
    return ordered_ids
