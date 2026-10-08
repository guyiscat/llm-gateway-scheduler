"""统一哈希与原子写入：失败时不留下貌似成功的半个产物。"""
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_json(value, *, nonfinite_to_none=False):
    # NumPy 标量使用 item() 转为 JSON 类型；未定义统计量可显式选择写 null。
    if isinstance(value, dict):
        return {str(key): clean_json(item, nonfinite_to_none=nonfinite_to_none) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item, nonfinite_to_none=nonfinite_to_none) for item in value]
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return clean_json(value.item(), nonfinite_to_none=nonfinite_to_none)
    if isinstance(value, float) and not math.isfinite(value) and nonfinite_to_none:
        return None
    return value


def _temporary(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name(f".{path.name}.{uuid4().hex}.tmp")


def write_text(path, text, encoding="utf-8"):
    """Atomically replace a text file, removing partial output on failure."""
    path = Path(path)
    temporary = _temporary(path)
    try:
        temporary.write_text(text, encoding=encoding)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value, *, nonfinite_to_none=False):
    text = json.dumps(clean_json(value, nonfinite_to_none=nonfinite_to_none), ensure_ascii=False,
                      indent=2, allow_nan=False) + "\n"
    write_text(path, text)


def write_parquet(path, frame):
    path = Path(path)
    temporary = _temporary(path)
    try:
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
