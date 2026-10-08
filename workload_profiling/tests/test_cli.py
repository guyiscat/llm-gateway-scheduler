"""Command forwarding, help, and imports after directory consolidation."""
from contextlib import redirect_stderr
from io import StringIO
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .. import __main__ as project_cli
from ..common.paths import ROOT


class CommandEntryTests(unittest.TestCase):
    def test_project_commands_forward_arguments_and_restore_argv(self):
        for command, (module_name, _) in project_cli.COMMANDS.items():
            with self.subTest(command=command):
                observed = []
                argv = ["workload_profiling", command, "--help"]
                module = SimpleNamespace(main=lambda: observed.append(sys.argv[:]))
                with patch.object(sys, "argv", argv), patch.object(
                        project_cli.importlib, "import_module", return_value=module) as imported:
                    project_cli.main()
                    self.assertIs(sys.argv, argv)
                imported.assert_called_once_with(module_name)
                self.assertEqual(observed[0][1:], ["--help"])
                self.assertTrue(observed[0][0].endswith(command))


    def test_subcommand_failure_restores_argv(self):
        argv = ["workload_profiling", "replay", "--invalid"]
        def fail():
            raise SystemExit(2)
        with patch.object(sys, "argv", argv), patch.object(project_cli.importlib, "import_module",
                                                          return_value=SimpleNamespace(main=fail)):
            with self.assertRaises(SystemExit) as error:
                project_cli.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIs(sys.argv, argv)

    def test_unknown_command_does_not_import_modules(self):
        with patch.object(sys, "argv", ["workload_profiling", "unknown"]), patch.object(
                project_cli.importlib, "import_module") as imported, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                project_cli.main()
            self.assertEqual(error.exception.code, 2)
            imported.assert_not_called()

    def test_real_help_for_all_entrypoints_without_data_loading(self):
        commands = [
            ["-m", "workload_profiling"],
            *[["-m", "workload_profiling", name] for name in project_cli.COMMANDS],
        ]
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, *command, "--help"], cwd=ROOT,
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertIn(b"usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
