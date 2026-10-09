"""Execution adapters, independent of routing algorithms and simulation."""
from .litellm_adapter import LiteLLMAdapter

__all__ = ["LiteLLMAdapter"]
