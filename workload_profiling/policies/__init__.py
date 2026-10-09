"""Replaceable policies; heavy percentile dependencies load only when needed."""
from .congestion import CongestionState

__all__ = ["CongestionState", "OutputHeavyPolicy", "PercentileReference"]


def __getattr__(name):
    if name == "OutputHeavyPolicy":
        from .output_heavy_policy import OutputHeavyPolicy
        return OutputHeavyPolicy
    if name == "PercentileReference":
        from .percentile_reference import PercentileReference
        return PercentileReference
    raise AttributeError(name)
