"""Scalar validation shared by core settings and simulation adapters."""
import math
from numbers import Real


def positive_integer(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'} integer")


def finite_number(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
