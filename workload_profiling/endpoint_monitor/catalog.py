"""Whitelist LiteLLM deployment fields; keep unknown capacity explicit."""
from dataclasses import asdict
import json
from pathlib import Path

from ..core.config import EndpointConfig
from ..common.validation import finite_number


def redact_credentials(value):
    if isinstance(value, dict):
        private = {"api_key", "apikey", "authorization", "password", "access_token", "secret_key"}
        return {key: "[REDACTED]" if key.lower() in private else redact_credentials(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_credentials(item) for item in value]
    return value


def filter_catalog(raw, *, limit_overrides=None, allow_empty=False):
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list) or (not raw["data"] and not allow_empty):
        raise ValueError("LiteLLM /model/info must contain a nonempty data list")
    overrides = limit_overrides or {}
    if not isinstance(overrides, dict):
        raise ValueError("Endpoint capacity overrides must be an object")
    allowed = {"rpm_limit", "tpm_limit", "concurrency_limit"}
    if set(overrides) - allowed:
        raise ValueError("Unknown endpoint capacity override")
    endpoints, ids = [], set()
    for row in raw["data"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid LiteLLM deployment entry")
        params, info = row.get("litellm_params") or {}, row.get("model_info") or {}
        if not isinstance(params, dict) or not isinstance(info, dict):
            raise ValueError("Invalid LiteLLM deployment parameters")
        endpoint_id, name = info.get("id"), row.get("model_name")
        if not isinstance(endpoint_id, str) or not endpoint_id.strip() or endpoint_id in ids:
            raise ValueError("Every LiteLLM deployment needs a unique model_info.id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Deployment model_name must be nonempty")
        ids.add(endpoint_id)

        def first(key):
            return params.get(key) if params.get(key) is not None else info.get(key)

        limits = {"rpm_limit": first("rpm"), "tpm_limit": first("tpm"),
                  "concurrency_limit": first("max_parallel_requests")}
        sources = {key: "litellm" if value is not None else "unknown" for key, value in limits.items()}
        for key, value in overrides.items():
            if value is not None:
                limits[key], sources[key] = value, "local_override"
        prices = {}
        for source, target in (("input_cost_per_token", "input_price_per_million"),
                               ("output_cost_per_token", "output_price_per_million")):
            value = first(source)
            if value is not None:
                finite_number(value, source, allow_zero=True)
            prices[target] = 0 if value is None else value * 1000000
        tags = params.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ValueError("Deployment tags must be a list of strings")
        config = EndpointConfig(endpoint_id, **limits, supported_models=(name,), api_types=("chat",),
            api_base=first("api_base"), deployment_model=name,
            context_limit=first("max_input_tokens"), **prices)
        endpoints.append({**asdict(config), "provider_model": params.get("model"), "tags": tags,
                          "limit_sources": sources, "health_source": "local_observations", "catalog_available": True})
    return endpoints


def load_state_configs(path):
    """Read only whitelisted configuration fields from the maintained state file."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    fields = EndpointConfig.__dataclass_fields__
    configs = tuple(EndpointConfig(**{key: row[key] for key in fields}) for row in data["endpoints"]
                    if row.get("catalog_available", True))
    if not configs or len({config.endpoint_id for config in configs}) != len(configs):
        raise ValueError("State file needs active, unique endpoint configurations")
    return configs
