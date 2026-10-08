"""The default dashboard and baseline export work without scientific imports."""
import csv
from dataclasses import replace
import io
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4

from ..baseline import BaselineRunner, WorkloadRequest, load_config
from ..baseline.reporting import requests_csv, write_outputs
from ..common.paths import CACHE


ROOT = Path(__file__).resolve().parents[2]
BLOCK_SCIENTIFIC_IMPORTS = '''
import importlib.abc
import sys
class BlockScientificImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pandas', 'numpy', 'scipy', 'pyarrow'}:
            raise ImportError('Scientific imports unavailable in this regression test: ' + fullname)
sys.meta_path.insert(0, BlockScientificImports())
'''


class BaselineDependencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = (CACHE / ("baseline_dependency_test_" + uuid4().hex)).resolve()
        self.directory.mkdir(parents=True)
        self.addCleanup(self.remove_directory)

    def remove_directory(self):
        if not self.directory.is_relative_to(CACHE.resolve()) or self.directory == CACHE.resolve():
            raise ValueError("Test directory escaped cache")
        shutil.rmtree(self.directory)

    def run_isolated(self, code):
        process = subprocess.run([sys.executable, "-X", "utf8", "-"], input=BLOCK_SCIENTIFIC_IMPORTS + code,
                                 text=True, encoding="utf-8", cwd=ROOT,
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return process.stdout

    def test_default_demo_open_dispatches_without_scientific_dependencies(self):
        output = self.run_isolated('''
from unittest.mock import patch
import demo
sys.argv = ['demo.py', '--open']
with patch('workload_profiling.baseline.dashboard.serve') as serve:
    demo.main()
    serve.assert_called_once_with(8765, open_browser=True)
assert not any(name in sys.modules for name in ['pandas', 'numpy', 'scipy', 'pyarrow'])
print('baseline startup passed')
''')
        self.assertIn("baseline startup passed", output)

    def test_length_cli_exports_all_files_when_scientific_imports_are_blocked(self):
        output = self.run_isolated(f'''
from workload_profiling.baseline.run import main
sys.argv = ['baseline.py', '--source', 'examples/length_requests.jsonl',
            '--source-format', 'lengths', '--batch-size', '2', '--batch-wait-ms', '3',
            '--batch-order', 'shortest_first', '--output-dir', {str(self.directory)!r}]
main()
assert not any(name in sys.modules for name in ['pandas', 'numpy', 'scipy', 'pyarrow'])
''')
        self.assertIn('"completed_requests": 9', output)
        self.assertIn('"rejected_requests": 0', output)
        self.assertEqual({p.name for p in self.directory.iterdir()},
                         {"config.json", "summary.json", "requests.csv", "events.jsonl",
                          "batches.json", "endpoints.json", "report.md"})
        self.assertTrue((self.directory / "requests.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_csv_preserves_missing_values_large_integers_and_quoted_ids(self):
        request_id = 'line,"quoted"\nnext'
        settings = replace(load_config(), batch_size=1)
        result = BaselineRunner(settings).run([
            WorkloadRequest(request_id, 100, 20),
            WorkloadRequest("large", 9007199254740993, 0),
        ])
        write_outputs(result, settings, self.directory)
        with (self.directory / "requests.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["request_id"], request_id)
        self.assertEqual(rows[0]["endpoint_id"], "")
        self.assertEqual(rows[0]["batch_position"], "")
        self.assertEqual(rows[0]["heavy"], "False")
        self.assertEqual(rows[1]["input_tokens"], "9007199254740993")
        self.assertEqual(rows[1]["batch_position"], "0")
        self.assertEqual(rows[1]["status"], "rejected")

    def test_empty_csv_retains_header(self):
        rows = csv.DictReader(io.StringIO(requests_csv([])))
        self.assertEqual(rows.fieldnames,
                         ["request_id", "source_line", "input_tokens", "output_tokens", "heavy",
                          "arrival_at_ms", "endpoint_id", "dispatch_at_ms", "finished_at_ms", "status"])
        self.assertEqual(list(rows), [])


if __name__ == "__main__":
    unittest.main()
