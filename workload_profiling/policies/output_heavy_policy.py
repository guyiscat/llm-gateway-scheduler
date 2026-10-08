"""阈值来源优先级：MANUAL > CONGESTION > DEFAULT；历史 reference 保持固定。"""
from __future__ import annotations

import json
from copy import deepcopy
import math
from numbers import Real
from pathlib import Path

from .congestion import CongestionState, validate_congestion_state
from .percentile_reference import PERCENTILE_DEFINITION, PercentileReference, validate_token_count

from ..common.paths import POLICY_CONFIG as CONFIG_PATH


def validate_threshold(value) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not 0 < value < 1:
        raise ValueError("output_heavy_threshold must be finite and satisfy 0 < threshold < 1")
    return float(value)


def load_config(path=CONFIG_PATH):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_threshold(config["default_threshold"])
    if config.get("percentile_definition") != PERCENTILE_DEFINITION:
        raise ValueError("Unsupported percentile_definition in policy config")
    mapping = config["congestion_threshold_mapping"]
    if set(mapping) != {state.value for state in CongestionState}:
        raise ValueError("Configuration must map exactly IDLE, NORMAL, BUSY and CRITICAL")
    for threshold in mapping.values():
        validate_threshold(threshold)
    return config


class OutputHeavyPolicy:
    """classify 接收百分位，evaluate 接收长度；两者都返回可审计的决策字典。"""
    def __init__(self, reference: PercentileReference | None = None,
                 config_path=CONFIG_PATH):
        config = load_config(config_path)
        self._configuration = deepcopy(config)
        if reference is not None and not isinstance(reference, PercentileReference):
            raise ValueError("reference must be a PercentileReference")
        if reference is not None and config.get("reference_sample_count") not in (None, reference.sample_count):
            raise ValueError("Policy config and reference sample counts do not match")
        self.reference = reference
        self._default_threshold = float(config["default_threshold"])
        self._mapping = {CongestionState(key): float(value) for key, value in config["congestion_threshold_mapping"].items()}
        self._manual_threshold = None
        self._congestion_state = None

    @property
    def configuration(self):
        """Snapshot of the validated rules actually loaded, isolated from mutation."""
        return deepcopy(self._configuration)

    def _resolve_threshold(self):
        # MANUAL 时仍记录外部状态，但它不改变生效阈值；清除后恢复最新自动状态。
        if self._manual_threshold is not None:
            return self._manual_threshold, "MANUAL"
        if self._congestion_state is not None:
            return self._mapping[self._congestion_state], "CONGESTION"
        return self._default_threshold, "DEFAULT"

    def get_threshold(self):
        return self._resolve_threshold()[0]

    @property
    def threshold_source(self):
        return self._resolve_threshold()[1]

    def set_threshold(self, threshold):
        # 验证通过后才改变状态，非法值不会破坏当前有效 override。
        self._manual_threshold = validate_threshold(threshold)

    def clear_manual_override(self):
        self._manual_threshold = None

    def update_from_congestion(self, congestion_state):
        # 支持外部显式推送状态。此处不计算 score、不判断进入/退出 BUSY、不实现 hysteresis。
        self._congestion_state = validate_congestion_state(congestion_state)

    def classify(self, output_percentile, *, output_tokens=None):
        if isinstance(output_percentile, bool) or not isinstance(output_percentile, Real) or not math.isfinite(output_percentile) or not 0 <= output_percentile <= 1:
            raise ValueError("output_percentile must be finite and between 0 and 1")
        if output_tokens is not None:
            output_tokens = validate_token_count(output_tokens)
        # 一次判断只读一次 provider，用同一状态生成 threshold/source，避免决策字段不一致。
        threshold, source = self._resolve_threshold()
        return {"output_tokens": output_tokens, "output_percentile": float(output_percentile),
                "output_heavy": bool(output_percentile >= threshold),
                "output_heavy_threshold": threshold,
                "congestion_state": self._congestion_state.value if self._congestion_state is not None else None,
                "threshold_source": source}

    def evaluate(self, output_tokens):
        if self.reference is None:
            raise ValueError("evaluate(output_tokens) requires a fixed PercentileReference")
        length = validate_token_count(output_tokens)
        return self.classify(self.reference.percentile(length), output_tokens=length)
