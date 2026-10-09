"""Endpoint capabilities and scheduling parameters; no traffic or service simulation."""
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path

from ..common.validation import finite_number, positive_integer


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: str
    rpm_limit: int
    tpm_limit: int
    concurrency_limit: int
    supported_models: tuple[str, ...] = ("default",)
    api_types: tuple[str, ...] = ("chat",)
    api_base: str | None = None
    deployment_model: str | None = None
    input_price_per_million: float = 0
    output_price_per_million: float = 0
    context_limit: int | None = None

    def __post_init__(self):
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id.strip():
            raise ValueError("endpoint_id must be nonempty")
        for name in ("rpm_limit", "tpm_limit", "concurrency_limit"):
            positive_integer(getattr(self, name), name)
        for name in ("supported_models", "api_types"):
            values = getattr(self, name)
            if isinstance(values, str) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f"{name} must be a nonempty sequence of strings")
            object.__setattr__(self, name, tuple(values))
        for name in ("api_base", "deployment_model"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty string or null")
        for name in ("input_price_per_million", "output_price_per_million"):
            finite_number(getattr(self, name), name, allow_zero=True)
        if self.context_limit is not None:
            positive_integer(self.context_limit, "context_limit")


@dataclass(frozen=True)
class SchedulerConfig:
    max_batch_size: int = 16
    max_wait_ms: int = 20
    busy_rpm_threshold: float = .95
    busy_tpm_threshold: float = .95
    busy_concurrency_reserve: int = 3
    busy_concurrency_threshold: float | None = None
    cooldown_ms: int = 1000
    history_window_ms: int = 60000
    history_max_samples: int = 10000
    ranking_policy: str = "priority_then_light"
    routing_policy: str = "min_rpm"
    input_weight: float = 1
    output_weight: float = 1
    ranking_direction: str = "ascending"
    input_threshold_tokens: float = 40342.5
    output_threshold_tokens: float = 578
    window_ms: int = 60000
    output_classification: str = "tokens"
    output_percentile_threshold: float = .8
    output_policy_path: str | None = None
    output_reference_path: str | None = None
    output_reference_metadata_path: str | None = None

    def __post_init__(self):
        for name in ("max_batch_size", "history_window_ms", "history_max_samples", "window_ms"):
            positive_integer(getattr(self, name), name)
        for name in ("max_wait_ms", "cooldown_ms", "busy_concurrency_reserve"):
            positive_integer(getattr(self, name), name, allow_zero=True)
        for name in ("busy_rpm_threshold", "busy_tpm_threshold", "busy_concurrency_threshold"):
            value = getattr(self, name)
            if name != "busy_concurrency_threshold" or value is not None:
                finite_number(value, name)
                if value > 1:
                    raise ValueError(f"{name} must be <= 1")
        for name in ("input_weight", "output_weight"):
            finite_number(getattr(self, name), name, allow_zero=True)
        if self.input_weight + self.output_weight == 0:
            raise ValueError("At least one ranking weight must be positive")
        if self.ranking_direction not in ("ascending", "descending"):
            raise ValueError("ranking_direction must be ascending or descending")
        if self.window_ms != 60000 or isinstance(self.window_ms, bool):
            raise ValueError("RPM/TPM require a 60000 ms window")
        for name in ("ranking_policy", "routing_policy"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a nonempty policy name")
        for name in ("input_threshold_tokens", "output_threshold_tokens"):
            finite_number(getattr(self, name), name, allow_zero=True)
        if self.output_classification not in ("tokens", "percentile_fixed", "percentile_dynamic"):
            raise ValueError("Invalid output_classification")
        finite_number(self.output_percentile_threshold, "output_percentile_threshold")
        if self.output_percentile_threshold >= 1:
            raise ValueError("output_percentile_threshold must be < 1")
        for name in ("output_policy_path", "output_reference_path", "output_reference_metadata_path"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty path string or null")
        if (self.output_reference_path is None) != (self.output_reference_metadata_path is None):
            raise ValueError("Output reference and metadata paths must be supplied together")

    def to_dict(self):
        return asdict(self)


def load_endpoint_configs(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = data.get("endpoints") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError("Endpoint config must contain a nonempty endpoints list")
    configs = tuple(EndpointConfig(**row) for row in rows)
    if len({e.endpoint_id for e in configs}) != len(configs):
        raise ValueError("Duplicate endpoint_id")
    return configs


def load_scheduler_config(path):
    path = Path(path).resolve()
    config = SchedulerConfig(**json.loads(path.read_text(encoding="utf-8-sig")))
    resources = {}
    for name in ("output_policy_path", "output_reference_path", "output_reference_metadata_path"):
        if getattr(config, name) is not None:
            resources[name] = str((path.parent / getattr(config, name)).resolve())
    return replace(config, **resources)
