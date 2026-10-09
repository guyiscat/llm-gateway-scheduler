"""Output-classification pressure levels, distinct from system busy admission."""
from enum import Enum


class CongestionState(Enum):
    IDLE = "idle"
    NORMAL = "normal"
    BUSY = "busy"
    CRITICAL = "critical"


def validate_congestion_state(state):
    if not isinstance(state, CongestionState):
        raise ValueError("congestion_state must be a CongestionState enum value")
    return state


