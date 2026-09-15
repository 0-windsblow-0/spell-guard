"""Distribution contracts (D01/D02): in-package demo/hook entry points.

The legacy scripts/ entry points stay as thin wrappers importing the package
modules. These tests call the installed-equivalent package paths, not source
scripts, and never interact with a real agent host.
"""

import contextlib
import io
from importlib.resources import files
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from spellguard.cli import main as cli_main


class DistributionCliTest(unittest.TestCase):
    def _run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = cli_main(argv)
            except SystemExit as exit_error:
                code = exit_error.code if isinstance(exit_error.code, int) else 2
        return code, stdout.getvalue(), stderr.getvalue()

    def test_demo_subcommand_wiring_exists(self):
        # D01: `spellguard demo` forwards to spellguard.demo.main.
        self._write_demo()
        code, output, _ = self._run_cli(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("1. Isolated", output)
        self.assertIn("Demo passed", output)
        self._remove_demo()

    def _write_demo(self):
        # demo writes its own temp git dir; ensure git identity never missing
        os.environ.setdefault("GIT_AUTHOR_NAME", "t")

    def _remove_demo(self):
        pass

    def test_hook_subcommand_forwards_to_adapter(self):
        code, stdout, stderr = self._run_cli(["hook", "--help"])
        self.assertEqual(code, 0)
        combined = stdout + stderr
        self.assertIn("--host", combined)
        self.assertIn("codex", combined)

    def test_hook_requires_host_and_reports_usages(self):
        code, _, stderr = self._run_cli(
            ["hook", "--repo", "/tmp", "--state-dir", "/tmp/s",
             "--registry-sha256", "a" * 64])
        self.assertEqual(code, 2)

    def test_hook_rejects_unknown_host(self):
        code, _, _ = self._run_cli(
            ["hook", "--host", "vscode", "--repo", "/tmp",
             "--state-dir", "/tmp/s", "--registry-sha256", "a" * 64,
             "--help"] if False else
            ["hook", "--host", "vscode", "--repo", "/tmp",
             "--state-dir", "/tmp/s", "--registry-sha256", "a" * 64])
        self.assertEqual(code, 2)

    def test_hook_announces_default_codex_when_other_args_invalid(self):
        # The usage error for missing host names supported hosts.
        code, _, stderr = self._run_cli([] if False else
                                        ["hook", "--repo", "/tmp",
                                         "--state-dir", "/tmp/s",
                                         "--registry-sha256", "a" * 64])
        combined = stderr
        self.assertEqual(code, 2)
        self.assertIn("codex", combined)


class PackageEntryTest(unittest.TestCase):
    """python -m spellguard demo / hook work without scripts/ directory."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        # simulate no source checkout: cwd outside repo
        self.previous = os.getcwd()
        os.chdir(self.tempdir.name)

    def tearDown(self):
        os.chdir(self.previous)
        self.tempdir.cleanup()

    def test_python_dash_m_spellguard_hook_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "spellguard", "hook", "--help"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(result.returncode, 0)
        self.assertIn("--host", result.stdout + result.stderr)

    def test_python_dash_m_spellguard_demo_runs(self):
        result = subprocess.run(
            [sys.executable, "-m", "spellguard", "demo"],
            capture_output=True, text=True, timeout=90,
            env={**os.environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertIn("1. Isolated", result.stdout)
        self.assertIn("Demo passed", result.stdout)


class AssistDistributionTest(unittest.TestCase):
    """A-A03 partial: in-package assist prompt and entry point."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.tempdir.name)

    def tearDown(self):
        os.chdir(self.previous)
        self.tempdir.cleanup()

    def test_assist_prepare_and_validate_documented(self):
        result = subprocess.run(
            [sys.executable, "-m", "spellguard", "assist", "--help"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(result.returncode, 0)
        self.assertIn("prepare", result.stdout + result.stderr)
        self.assertIn("validate", result.stdout + result.stderr)

    def test_assist_prompt_is_packaged(self):
        prompt = files("spellguard").joinpath("assist_prompt.md").read_text()
        self.assertIn("Return at most three review questions", prompt)
        self.assertIn("Never change the code", prompt)


if __name__ == "__main__":
    unittest.main()
