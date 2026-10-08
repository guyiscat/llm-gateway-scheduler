"""固定历史分布的右连续 ECDF；不随运行时状态更新，不计算 EVT probability。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from numbers import Integral, Real
from pathlib import Path

import numpy as np

from ..common.io import sha256_file, write_parquet

PERCENTILE_DEFINITION = "empirical_cdf_right"


def validate_token_count(value) -> int:
    # 不把小数、字符串或 bool 静默转换为 token 数，避免隐藏调用方错误。
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral):
        raise ValueError("output_tokens must be a nonnegative integer")
    value = int(value)
    if value < 0 or value > np.iinfo(np.int64).max:
        raise ValueError("output_tokens must be a nonnegative int64 value")
    return value


def integer_vector(values) -> np.ndarray:
    values = np.asarray(values)
    if values.ndim != 1 or not len(values) or values.dtype.kind not in "iu":
        raise ValueError("Reference lengths must be a nonempty one-dimensional integer array")
    if np.any(values < 0) or np.any(values > np.iinfo(np.int64).max):
        raise ValueError("Reference lengths must be nonnegative int64 values")
    return values.astype(np.int64, copy=True)


@dataclass(frozen=True)
class PercentileReference:
    # 每个 unique length 存频数即可恢复完整历史 ECDF，无需保存 response 文本。
    values: np.ndarray
    counts: np.ndarray

    def __post_init__(self):
        values = integer_vector(self.values)
        counts = integer_vector(self.counts)
        if len(values) != len(counts) or np.any(counts <= 0) or np.any(values[1:] <= values[:-1]):
            raise ValueError("Reference values must be strictly increasing and have positive matching counts")
        sample_count = sum(int(count) for count in counts)
        if sample_count > np.iinfo(np.int64).max:
            raise ValueError("Reference sample count exceeds int64")
        cumulative = np.cumsum(counts, dtype=np.int64)
        # 属性不可替换，数组也不可写：动态阈值改变分类，不改变 reference distribution。
        for array in (values, counts, cumulative):
            array.setflags(write=False)
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "counts", counts)
        object.__setattr__(self, "cumulative_counts", cumulative)
        object.__setattr__(self, "sample_count", sample_count)

    @classmethod
    def from_lengths(cls, output_tokens):
        values, counts = np.unique(integer_vector(output_tokens), return_counts=True)
        return cls(values, counts)

    def percentile(self, output_tokens) -> float:
        length = validate_token_count(output_tokens)
        # side='right' 包含所有等于 x 的历史值：P(x) = count(reference <= x) / N。
        # 因而重复长度始终获得相同 percentile，而不是按请求顺序拆分 ties。
        index = int(np.searchsorted(self.values, length, side="right"))
        return float(self.cumulative_counts[index - 1] / self.sample_count) if index else 0.0

    def percentiles(self, output_tokens) -> np.ndarray:
        lengths = integer_vector(output_tokens)
        indices = np.searchsorted(self.values, lengths, side="right")
        cumulative = np.concatenate(([0], self.cumulative_counts))
        return cumulative[indices] / self.sample_count

    def minimum_length_at(self, percentile_threshold) -> int:
        # 返回 ECDF 首次达到阈值的已有长度；ties 使它不必等于 linear quantile。
        if isinstance(percentile_threshold, bool) or not isinstance(percentile_threshold, Real) or not math.isfinite(percentile_threshold) or not 0 < percentile_threshold < 1:
            raise ValueError("percentile_threshold must be finite and satisfy 0 < threshold < 1")
        cumulative = self.cumulative_counts / self.sample_count
        index = int(np.searchsorted(cumulative, percentile_threshold, side="left"))
        return int(self.values[index])

    def save(self, path: Path):
        import pandas as pd
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_parquet(path, pd.DataFrame({"output_tokens": self.values, "count": self.counts,
                      "cumulative_count": self.cumulative_counts,
                      "output_percentile": self.cumulative_counts / self.sample_count}))

    @classmethod
    def load(cls, artifact_path: Path, metadata_path: Path):
        import pandas as pd
        metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        if metadata.get("percentile_definition") != PERCENTILE_DEFINITION:
            raise ValueError("Unsupported percentile definition")
        if metadata.get("artifact_sha256") != sha256_file(artifact_path):
            raise ValueError("Percentile reference artifact checksum does not match metadata")
        table = pd.read_parquet(artifact_path)
        reference = cls(table.output_tokens.to_numpy(), table["count"].to_numpy())
        if reference.sample_count != metadata.get("reference_sample_count"):
            raise ValueError("Percentile reference sample count does not match metadata")
        np.testing.assert_array_equal(table.cumulative_count.to_numpy(), reference.cumulative_counts)
        np.testing.assert_allclose(table.output_percentile.to_numpy(), reference.cumulative_counts / reference.sample_count)
        return reference
