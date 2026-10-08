"""Replaceable ordering of a released batch, independent of endpoint routing."""
from collections.abc import Sequence
import importlib
import inspect
from typing import Protocol

from .models import EndpointView, WorkloadRequest


class BatchOrderStrategy(Protocol):
    def order_batch(self, requests: tuple[WorkloadRequest, ...],
                    endpoints: tuple[EndpointView, ...], now_ms: int) -> Sequence[str]:
        """Return every batch request_id exactly once, in the desired order.

        Requests follow arrival order. Endpoints include ALL endpoint snapshots,
        including busy endpoints. Both tuples and their members are immutable.
        """
        ...


class FifoOrderStrategy:
    def order_batch(self, requests, endpoints, now_ms):
        return [request.request_id for request in requests]


class PriorityThenLightOrderStrategy:
    def __init__(self, config, classification_policy=None):
        self.config = config
        self.classification_policy = classification_policy

    def order_batch(self, requests, endpoints, now_ms):
        from .priority import heavy_for
        def key(request):
            heavy = (heavy_for(request, self.config) if self.classification_policy is None
                     else self.classification_policy.decisions[request.request_id]["heavy"])
            return -request.priority_level, heavy
        # Stable ties retain arrival order within the same level and type.
        return [r.request_id for r in sorted(requests, key=key)]


BUILTIN_BATCH_ORDERS = {
    "fifo": FifoOrderStrategy,
    "priority_then_light": PriorityThenLightOrderStrategy,
}


def load_batch_order(spec, *, config=None, classification_policy=None):
    if spec == "priority_then_light":
        if config is None:
            raise ValueError("Priority ordering requires a SimulationConfig")
        return PriorityThenLightOrderStrategy(config, classification_policy)
    if spec in BUILTIN_BATCH_ORDERS:
        return BUILTIN_BATCH_ORDERS[spec]()
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("batch_order must be fifo, priority_then_light or module:attribute")
    try:
        order = getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as error:
        raise ValueError(f"Cannot load batch order {spec}: {type(error).__name__}") from None
    if inspect.isclass(order) or not hasattr(order, "order_batch"):
        order = order()
    if not callable(getattr(order, "order_batch", None)):
        raise TypeError("Batch order must implement order_batch(requests, endpoints, now_ms)")
    if inspect.iscoroutinefunction(order.order_batch):
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
