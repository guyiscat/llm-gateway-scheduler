"""统一哈希与原子写入：失败时不留下貌似成功的半个产物。"""
import hashlib
from pathlib import Path
from uuid import uuid4


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name(f".{path.name}.{uuid4().hex}.tmp")


def write_parquet(path, frame):
    path = Path(path)
    temporary = _temporary(path)
    try:
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
