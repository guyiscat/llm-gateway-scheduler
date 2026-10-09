"""实际文件验证原子写入失败后旧产物仍可用。"""
from pathlib import Path
import unittest
from uuid import uuid4

from ..common.io import write_parquet
from ..common.paths import CACHE


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = CACHE / ("io_test_" + uuid4().hex)
        self.directory.mkdir(parents=True)

    def tearDown(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

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
