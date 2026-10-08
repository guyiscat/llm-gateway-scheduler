"""实际文件验证原子写入失败后旧产物仍可用。"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from ..common.io import write_json, write_parquet, write_text
from ..common.paths import CACHE


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = CACHE / ("io_test_" + uuid4().hex)
        self.directory.mkdir(parents=True)

    def tearDown(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def test_nonfinite_json_failure_preserves_existing_file(self):
        path = self.directory / "result.json"
        write_json(path, {"n": 1})
        previous = path.read_bytes()
        with self.assertRaises(ValueError):
            write_json(path, {"n": float("nan")})
        self.assertEqual(path.read_bytes(), previous)
        write_json(path, {"n": float("nan")}, nonfinite_to_none=True)
        self.assertIsNone(json.loads(path.read_text())["n"])

    def test_interrupted_parquet_does_not_replace_previous_file(self):
        path = self.directory / "result.parquet"
        path.write_bytes(b"previous")

        class BrokenFrame:
            def to_parquet(self, temporary, **kwargs):
                Path(temporary).write_bytes(b"partial")
                raise RuntimeError("write interrupted")

        with self.assertRaises(RuntimeError):
            write_parquet(path, BrokenFrame())
        self.assertEqual(path.read_bytes(), b"previous")
        self.assertEqual([item.name for item in self.directory.iterdir()], ["result.parquet"])

    def test_interrupted_text_preserves_previous_file_and_removes_temporary(self):
        path = self.directory / "result.txt"
        write_text(path, "previous")

        def interrupt(temporary, text, **kwargs):
            temporary.write_bytes(b"partial")
            raise OSError("write interrupted")

        with patch.object(Path, "write_text", autospec=True, side_effect=interrupt):
            with self.assertRaises(OSError):
                write_text(path, "replacement", encoding="utf-8-sig")
        self.assertEqual(path.read_text(encoding="utf-8"), "previous")
        self.assertEqual([item.name for item in self.directory.iterdir()], ["result.txt"])
