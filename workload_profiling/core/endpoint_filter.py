"""Capability/health eligibility and hard capacity are distinct candidate sets."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointPool:
    compatible: tuple
    eligible: tuple
    feasible: tuple
    exclusions: dict


def model_rule(request, config, state, now_ms):
    return None if request.target_model in config.supported_models or "*" in config.supported_models else "model_unsupported"


def interface_rule(request, config, state, now_ms):
    return None if request.api_type in config.api_types else "interface_unsupported"


def context_rule(request, config, state, now_ms):
    length = request.input_tokens + (request.max_tokens if request.max_tokens is not None else request.predicted_output_tokens)
    return "context_limit" if config.context_limit is not None and length > config.context_limit else None


class EndpointFilter:
    def __init__(self):
        self.rules = {"model": model_rule, "interface": interface_rule, "context": context_rule}

    def register(self, name, rule):
        if not isinstance(name, str) or not name or not callable(rule):
            raise ValueError("Filter rule requires a name and callable")
        self.rules[name] = rule

    def remove(self, name):
        del self.rules[name]

    def filter(self, request, configs, snapshots, now_ms):
        compatible, eligible, feasible, exclusions = [], [], [], {}
        for view in snapshots:
            config = configs[view.endpoint_id]
            reasons = tuple(reason for rule in self.rules.values() if (reason := rule(request, config, view, now_ms)))
            if reasons:
                exclusions[view.endpoint_id] = reasons
                continue
            compatible.append(view)
            reasons = []
            if not view.healthy:
                reasons.append("unhealthy")
            if view.cooldown_until_ms > now_ms:
                reasons.append("cooldown")
            if reasons:
                exclusions[view.endpoint_id] = tuple(reasons)
                continue
            eligible.append(view)
            if view.can_accept(request):
                feasible.append(view)
            else:
                exclusions[view.endpoint_id] = ("capacity",)
        return EndpointPool(tuple(compatible), tuple(eligible), tuple(feasible), exclusions)
