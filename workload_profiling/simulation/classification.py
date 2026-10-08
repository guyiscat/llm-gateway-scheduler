"""Frozen output ECDF, classified once per released batch using observable demand."""
from copy import deepcopy
from pathlib import Path

from .config import finite_number
from ..common.io import sha256_file
from ..common.paths import ARTIFACTS, PACKAGE
from ..policies import OutputHeavyPolicy, PercentileReference
from ..policies.congestion import CongestionState


class OutputPercentileClassifier:
    TRACE_COLUMNS = ("batch_id", "time_ms", "batch_size", "ready_before", "occupied_slots",
                     "total_slots", "free_slots", "projected_excess_requests", "concurrency_utilization",
                     "pressure_level", "previous_threshold", "threshold", "threshold_changed", "token_cutoff",
                     "input_heavy_requests", "output_heavy_requests", "heavy_requests")
    def __init__(self, config, reference, policy_path=None):
        self.config = config
        self.reference = reference
        path = Path(policy_path or config.output_policy_path or PACKAGE / "config/pressure_threshold_policy.json").resolve()
        self.policy = OutputHeavyPolicy(reference, config_path=path)
        self.profile = self.policy.configuration
        ordered = [self.profile["congestion_threshold_mapping"][s.value] for s in
                   (CongestionState.IDLE, CongestionState.NORMAL, CongestionState.BUSY, CongestionState.CRITICAL)]
        if any(a < b for a, b in zip(ordered, ordered[1:])):
            raise ValueError("Higher pressure must not increase the output percentile threshold")
        defaults = {"normal_concurrency_threshold": .60, "busy_concurrency_threshold": .85,
                    "critical_excess_ratio": 1.0}
        supplied = self.profile.get("pressure_rules", {})
        if not isinstance(supplied, dict) or not set(supplied) <= set(defaults):
            raise ValueError("Unknown or invalid pressure_rules")
        rules = defaults | supplied
        for name, value in rules.items():
            finite_number(value, name, allow_zero=name != "critical_excess_ratio")
        if not 0 <= rules["normal_concurrency_threshold"] < rules["busy_concurrency_threshold"] <= 1:
            raise ValueError("Pressure concurrency thresholds must satisfy 0 <= normal < busy <= 1")
        self.profile["pressure_rules"] = rules
        self.reference_source = {"kind": "injected", "sample_count": reference.sample_count}
        self.policy_source = {"path": str(path), "sha256": sha256_file(path)}
        if config.output_classification == "percentile_fixed":
            self.policy.set_threshold(config.output_percentile_threshold)
        self.current_threshold = config.output_percentile_threshold
        self.decisions = {}
        self.trace = []
        self.trace_columns = self.TRACE_COLUMNS + ("classification_context",)

    @classmethod
    def load_default(cls, config):
        reference_path = Path(config.output_reference_path or ARTIFACTS / "output_percentile_reference.parquet").resolve()
        metadata_path = Path(config.output_reference_metadata_path or ARTIFACTS / "output_reference_metadata.json").resolve()
        reference = PercentileReference.load(reference_path, metadata_path)
        classifier = cls(config, reference)
        classifier.reference_source = {"kind": "file", "path": str(reference_path),
            "sha256": sha256_file(reference_path), "metadata_path": str(metadata_path),
            "metadata_sha256": sha256_file(metadata_path), "sample_count": reference.sample_count}
        return classifier

    def artifacts(self):
        """Self-contained snapshots for CLI replay and review of actual rules."""
        return deepcopy({"trace": self.trace, "trace_columns": list(self.trace_columns), "policy": self.profile,
            "metadata": {"mode": self.config.output_classification,
                         "initial_threshold": self.config.output_percentile_threshold,
                         "policy_source": self.policy_source, "reference_source": self.reference_source,
                         "classification_time": "immediate_admission_or_batch_release; frozen afterwards",
                         "pressure_inputs": "current concurrency + ready + this arrived batch; no future arrivals"},
            "reference": {"values": self.reference.values.tolist(), "counts": self.reference.counts.tolist()}})

    def classify_batch(self, requests, endpoints, ready_count, now_ms, batch_id):
        slots = sum(e.concurrency_limit for e in endpoints)
        occupied = sum(e.concurrency for e in endpoints)
        free = slots - occupied
        # Already arrived work only: existing ready plus the batch about to release.
        excess = max(0, ready_count + len(requests) - free)
        utilization = occupied / slots
        rules = self.profile["pressure_rules"]
        if excess >= slots * rules["critical_excess_ratio"]:
            state = CongestionState.CRITICAL
        elif excess > 0 or utilization >= rules["busy_concurrency_threshold"]:
            state = CongestionState.BUSY
        elif utilization >= rules["normal_concurrency_threshold"]:
            state = CongestionState.NORMAL
        else:
            state = CongestionState.IDLE
        self.policy.update_from_congestion(state)
        threshold = self.policy.get_threshold()
        previous = self.current_threshold
        self.current_threshold = threshold
        self.decisions = {}
        for request in requests:
            result = self.policy.evaluate(request.output_tokens)
            input_heavy = request.input_tokens >= self.config.input_threshold_tokens
            self.decisions[request.request_id] = {
                "input_heavy": input_heavy, "output_heavy": result["output_heavy"],
                "heavy": input_heavy or result["output_heavy"],
                "output_percentile": result["output_percentile"],
                "output_percentile_threshold": threshold,
                "output_token_cutoff": self.reference.minimum_length_at(threshold),
                "threshold_source": result["threshold_source"],
                "pressure_level": state.value, "classified_at_ms": now_ms}
        snapshot = {"batch_id": batch_id, "time_ms": now_ms, "batch_size": len(requests),
                    "ready_before": ready_count, "occupied_slots": occupied, "total_slots": slots,
                    "free_slots": free, "projected_excess_requests": excess,
                    "concurrency_utilization": utilization, "pressure_level": state.value,
                    "previous_threshold": previous, "threshold": threshold,
                    "threshold_changed": threshold != previous,
                    "token_cutoff": self.reference.minimum_length_at(threshold),
                    "input_heavy_requests": sum(v["input_heavy"] for v in self.decisions.values()),
                    "output_heavy_requests": sum(v["output_heavy"] for v in self.decisions.values()),
                    "heavy_requests": sum(v["heavy"] for v in self.decisions.values())}
        self.trace.append(snapshot)
        return self.decisions, snapshot
