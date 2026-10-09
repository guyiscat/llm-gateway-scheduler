"""Validated settings; all clock values use integer simulated milliseconds."""
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path

from ..common.paths import PACKAGE

CONFIG_PATH = PACKAGE / "config/simulation_adaptive.json"


from ..common.validation import positive_integer, finite_number


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: str
    rpm_limit: int
    tpm_limit: int
    concurrency_limit: int
    base_latency_ms: int = 5
    input_tokens_per_ms: float = 2000
    output_tokens_per_ms: float = 20
    service_jitter_fraction: float = 0
    service_jitter_seed: int = 20261005
    supported_models: tuple[str, ...] = ("default",)
    api_types: tuple[str, ...] = ("chat",)
    api_base: str | None = None
    deployment_model: str | None = None
    input_price_per_million: float = 0
    output_price_per_million: float = 0
    context_limit: int | None = None

    def __post_init__(self):
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id.strip():
            raise ValueError("endpoint_id must be a nonempty string")
        for name in ("rpm_limit", "tpm_limit", "concurrency_limit"):
            positive_integer(getattr(self, name), name)
        positive_integer(self.base_latency_ms, "base_latency_ms", allow_zero=True)
        for name in ("input_tokens_per_ms", "output_tokens_per_ms"):
            finite_number(getattr(self, name), name)
        finite_number(self.service_jitter_fraction, "service_jitter_fraction", allow_zero=True)
        if self.service_jitter_fraction >= 1:
            raise ValueError("service_jitter_fraction must satisfy 0 <= fraction < 1")
        positive_integer(self.service_jitter_seed, "service_jitter_seed", allow_zero=True)
        core = self.to_core()
        object.__setattr__(self, "supported_models", core.supported_models)
        object.__setattr__(self, "api_types", core.api_types)

    def to_core(self):
        from ..core.config import EndpointConfig as CoreEndpointConfig
        fields = CoreEndpointConfig.__dataclass_fields__
        return CoreEndpointConfig(**{name: getattr(self, name) for name in fields})


@dataclass(frozen=True)
class SimulationConfig:
    arrival_interval_ms: int = 1
    batch_size: int = 16
    batch_wait_ms: int = 20
    input_threshold_tokens: float = 40342.5
    output_threshold_tokens: float = 578
    window_ms: int = 60000
    strategy: str = "min_rpm"
    endpoints: tuple[EndpointConfig, ...] = field(default_factory=tuple)
    batch_order: str = "priority_then_light"
    arrival_mode: str = "fixed"
    burst_size: int = 512
    burst_span_ms: int = 20
    priority_assignment: str = "four_level"
    priority_seed: int = 20261008
    output_classification: str = "percentile_dynamic"
    output_percentile_threshold: float = 0.8
    output_policy_path: str | None = None
    output_reference_path: str | None = None
    output_reference_metadata_path: str | None = None
    busy_rpm_threshold: float = 0.95
    busy_tpm_threshold: float = 0.95
    busy_concurrency_reserve: int = 3
    busy_concurrency_threshold: float | None = None
    cooldown_ms: int = 1000
    history_window_ms: int = 60000
    history_max_samples: int = 10000
    input_weight: float = 1
    output_weight: float = 1
    ranking_direction: str = "ascending"
    high_priority_ratio: float = .1
    arrival_seed: int = 20261008
    random_min_interval_ms: int = 1
    random_max_interval_ms: int = 10
    target_model: str = "default"
    stream: bool = False
    max_tokens: int | None = None
    slo: dict | None = None
    prediction_mode: str = "oracle"
    predicted_output_tokens: int = 500

    def __post_init__(self):
        for name in ("arrival_interval_ms", "batch_size", "window_ms"):
            positive_integer(getattr(self, name), name)
        positive_integer(self.batch_wait_ms, "batch_wait_ms", allow_zero=True)
        if self.output_classification not in ("tokens", "percentile_fixed", "percentile_dynamic"):
            raise ValueError("Invalid output_classification")
        finite_number(self.output_percentile_threshold, "output_percentile_threshold")
        if self.output_percentile_threshold >= 1:
            raise ValueError("output_percentile_threshold must satisfy 0 < threshold < 1")
        for name in ("output_policy_path", "output_reference_path", "output_reference_metadata_path"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty path string or null")
        if (self.output_reference_path is None) != (self.output_reference_metadata_path is None):
            raise ValueError("Output reference and metadata paths must be supplied together")
        if self.priority_assignment not in ("uniform", "four_level", "binary"):
            raise ValueError("priority_assignment must be uniform, four_level or binary")
        positive_integer(self.priority_seed, "priority_seed", allow_zero=True)
        if self.arrival_mode not in ("fixed", "burst", "random"):
            raise ValueError("arrival_mode must be fixed, burst or random")
        positive_integer(self.burst_size, "burst_size")
        if self.burst_size < 2:
            raise ValueError("burst_size must be at least 2")
        positive_integer(self.burst_span_ms, "burst_span_ms", allow_zero=True)
        if self.arrival_mode == "burst" and self.burst_span_ms >= (self.burst_size - 1) * self.arrival_interval_ms:
            raise ValueError("burst_span_ms must be shorter than the original group span")
        if self.window_ms != 60000 or isinstance(self.window_ms, bool):
            raise ValueError("RPM/TPM use a fixed 60000 ms rolling window")
        for name in ("input_threshold_tokens", "output_threshold_tokens"):
            finite_number(getattr(self, name), name, allow_zero=True)
        if not isinstance(self.strategy, str) or not self.strategy:
            raise ValueError("strategy must be min_rpm or module:attribute")
        if not isinstance(self.batch_order, str) or not self.batch_order.strip():
            raise ValueError("batch_order must be a nonempty strategy name or module:attribute")
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        if not self.endpoints or any(not isinstance(e, EndpointConfig) for e in self.endpoints):
            raise ValueError("At least one valid endpoint is required")
        if len({e.endpoint_id for e in self.endpoints}) != len(self.endpoints):
            raise ValueError("endpoint_id values must be unique")
        for name in ("busy_rpm_threshold", "busy_tpm_threshold"):
            finite_number(getattr(self, name), name)
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must satisfy 0 < threshold <= 1")
        positive_integer(self.busy_concurrency_reserve, "busy_concurrency_reserve", allow_zero=True)
        if self.busy_concurrency_threshold is None and any(self.busy_concurrency_reserve >= e.concurrency_limit for e in self.endpoints):
            raise ValueError("busy_concurrency_reserve must be smaller than every endpoint concurrency_limit")

        finite_number(self.high_priority_ratio, "high_priority_ratio", allow_zero=True)
        if self.high_priority_ratio > 1:
            raise ValueError("high_priority_ratio must be <= 1")
        for name in ("arrival_seed", "random_min_interval_ms", "random_max_interval_ms", "predicted_output_tokens"):
            positive_integer(getattr(self, name), name, allow_zero=True)
        if self.random_max_interval_ms < self.random_min_interval_ms:
            raise ValueError("Random maximum interval must be >= minimum")
        if self.prediction_mode not in ("oracle", "fixed"):
            raise ValueError("prediction_mode must be oracle or fixed")
        if not isinstance(self.target_model, str) or not self.target_model.strip() or not isinstance(self.stream, bool):
            raise ValueError("Invalid simulation target_model or stream")
        if self.max_tokens is not None:
            positive_integer(self.max_tokens, "max_tokens")
        from ..core.request import SLO
        if self.slo is not None:
            SLO(**self.slo)
        self.scheduler_config()

    def scheduler_config(self):
        from ..core.config import SchedulerConfig
        return SchedulerConfig(max_batch_size=self.batch_size, max_wait_ms=self.batch_wait_ms,
            ranking_policy=self.batch_order, routing_policy=self.strategy,
            **{name: getattr(self, name) for name in SchedulerConfig.__dataclass_fields__
               if name not in {"max_batch_size", "max_wait_ms", "ranking_policy", "routing_policy"}})

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Simulation config must be an object")
        settings = dict(data)
        settings["endpoints"] = tuple(EndpointConfig(**e) for e in settings.get("endpoints", []))
        return cls(**settings)


def load_config(path=CONFIG_PATH):
    path = Path(path).resolve()
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise ValueError("Simulation config must be an object")
    if "endpoint_config" in settings or "scheduler_config" in settings:
        from ..core.config import load_endpoint_configs, load_scheduler_config
        settings = dict(settings)
        endpoint_path = settings.pop("endpoint_config", None)
        scheduler_path = settings.pop("scheduler_config", None)
        if not all(isinstance(p, str) and p.strip() for p in (endpoint_path, scheduler_path)):
            raise ValueError("Split config requires endpoint_config and scheduler_config paths")
        endpoints = load_endpoint_configs(path.parent / endpoint_path)
        scheduler = load_scheduler_config(path.parent / scheduler_path).to_dict()
        aliases = {"max_batch_size": "batch_size", "max_wait_ms": "batch_wait_ms",
                   "ranking_policy": "batch_order", "routing_policy": "strategy"}
        scheduler = {aliases.get(k, k): v for k, v in scheduler.items()}
        if set(settings) & (set(scheduler) | {"endpoints"}):
            raise ValueError("Put endpoint capabilities and scheduler parameters in their dedicated files")
        service = settings.pop("service_time", {})
        overrides = settings.pop("endpoint_service", {})
        service_fields = {"base_latency_ms", "input_tokens_per_ms", "output_tokens_per_ms",
                          "service_jitter_fraction", "service_jitter_seed"}
        if not isinstance(service, dict) or not set(service) <= service_fields:
            raise ValueError("Invalid simulation service_time fields")
        if not isinstance(overrides, dict) or not set(overrides) <= {e.endpoint_id for e in endpoints}:
            raise ValueError("Unknown endpoint_service endpoint")
        rows = []
        for endpoint in endpoints:
            extra = overrides.get(endpoint.endpoint_id, {})
            if not isinstance(extra, dict) or not set(extra) <= service_fields:
                raise ValueError("Invalid endpoint_service fields")
            rows.append({**asdict(endpoint), **service, **extra})
        settings.update(scheduler, endpoints=rows)
    config = SimulationConfig.from_dict(settings)
    resources = {}
    for name in ("output_policy_path", "output_reference_path", "output_reference_metadata_path"):
        value = getattr(config, name)
        if value is not None:
            resource = Path(value)
            resources[name] = str((resource if resource.is_absolute() else path.parent / resource).resolve())
    return replace(config, **resources) if resources else config
