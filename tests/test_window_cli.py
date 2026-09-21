"""CLI integration tests for check/context (R04).

Real temporary Git repositories, real CLI main entry, no report mocks.
Covers the task-card three-step Git sequence plus B07/B09/B13 registry and
read-failure boundaries.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from spellguard.cli import main

FIXTURE = {
    "schema_version": 1,
    "rules": [{
        "id": "TEMP-001",
        "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": "Synthetic fixture: compatibility helper retained during migration.",
        "desired_state": "Synthetic fixture: remove helper after migration.",
        "protected_symbol": {"path": "src/demo/workaround.py",
                             "symbol": "fallback", "source_root": "src"},
        "window": "no_external_callers",
        "resolution_reason": None,
    }],
}


def fixture_digest() -> str:
    return hashlib.sha256(json.dumps(
        FIXTURE, ensure_ascii=True, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


DEFINITION = "def fallback():\n    return 0\n"
CALLER = ("from demo.workaround import fallback as f\n"
          "def run():\n    return f()\n")


class WindowCliGoTest(unittest.TestCase):
    """G03: Go rule through the real CLI with temporary Git repo; the frozen
    sequence isolated OPEN -> caller VIOLATED + added -> committed VIOLATED +
    no added -> removal OPEN -> syntax-error exit 2."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "spellguard-test@example.invalid")
        self._git("config", "user.name", "Spellguard Test")
        self.previous = os.getcwd()
        os.chdir(self.root)
        import hashlib
        self.go_fixture = {
            "schema_version": 1,
            "rules": [{"id": "TEMP-001", "classification": "temporary",
                       "lifecycle": "ACTIVE", "reason": "Synthetic migration",
                       "desired_state": "Remove adapter",
                       "window": "no_external_callers",
                       "resolution_reason": None,
                       "protected_symbol": {"path": "backend/legacy/adapt.go",
                                            "symbol": "Adapt",
                                            "source_root": "backend"}}]}
        self.digest = hashlib.sha256(json.dumps(
            self.go_fixture, ensure_ascii=True, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        self._write("backend/go.mod", "module example.test/demo\n")
        self._write("backend/legacy/adapt.go",
                    "package legacy\n\nfunc Adapt() int { return 0 }\n")
        self._write(".spellguard/rules.json", json.dumps(self.go_fixture))
        self._commit_all()

    def tearDown(self):
        os.chdir(self.previous)
        self.tempdir.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.root)] + list(arguments),
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _write(self, relative_path: str, content: str):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _commit_all(self):
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def _run(self, argv):
        import contextlib, io
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_go_frozen_sequence(self):
        # 1. isolated
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(report["results"][0]["status"], "OPEN")
        # 2. new untracked caller
        self._write("backend/cmd/run.go",
                    'package main\n\nimport "example.test/demo/legacy"\n\n'
                    "func main() { legacy.Adapt() }\n")
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["results"][0]["status"], "VIOLATED")
        self.assertEqual(len(report["results"][0]["added_consumers"]), 1)
        # 3. commit — remains violated, no added
        self._commit_all()
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["results"][0]["added_consumers"], [])
        # 4. remove caller — back to open
        (self.root / "backend/cmd/run.go").unlink()
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(report["results"][0]["status"], "OPEN")

    def test_go_registry_change_requires_new_digest(self):
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        self.assertEqual(code, 0)
        # change rule but keep digest — must refuse
        changed = json.loads(json.dumps(self.go_fixture))
        changed["rules"][0]["reason"] = "Changed without reinstall"
        (self.root / ".spellguard" / "rules.json").write_text(json.dumps(changed))
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertIn("REGISTRY_CHANGED",
                      [d["code"] for d in report["diagnostics"]])


class WindowCliTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "spellguard-test@example.invalid")
        self._git("config", "user.name", "Spellguard Test")
        self.previous = os.getcwd()
        os.chdir(self.root)
        self.digest = fixture_digest()

    def tearDown(self):
        os.chdir(self.previous)
        self.tempdir.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.root)] + list(arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _write(self, relative_path: str, content: str):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _install_registry(self):
        registry = self.root / ".spellguard" / "rules.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(FIXTURE), encoding="utf-8")

    def _commit_all(self):
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def _run(self, argv):
        import contextlib
        import io

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    # ---- task-card three-step sequence -------------------------------------

    def test_head_without_caller_new_working_tree_caller_is_violated(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/use.py", CALLER)

        code, output, error = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertTrue(report["complete"])
        self.assertEqual(report["results"][0]["status"], "VIOLATED")
        self.assertEqual(len(report["results"][0]["added_consumers"]), 1)

    def test_committed_caller_keeps_violated_with_no_added_consumers(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/use.py", CALLER)
        self._commit_all()

        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["results"][0]["status"], "VIOLATED")
        self.assertEqual(report["results"][0]["added_consumers"], [])

    def test_removing_caller_recovers_open(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/use.py", CALLER)
        self._commit_all()
        (self.root / "src/demo/use.py").unlink()

        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(report["results"][0]["status"], "OPEN")
        self.assertTrue(report["complete"])

    def test_text_check_is_quiet_when_open(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()

        code, output, error = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "text"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(error, "")

    def test_json_is_deterministic_across_runs(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/use.py", CALLER)

        first = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        second = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])

    # ---- registry boundaries ----------------------------------------------

    def test_missing_registry_is_not_empty_success(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._commit_all()
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["rules"], [])
        self.assertIsNone(report["registry_digest"])
        self.assertIn("REGISTRY_READ_FAILED",
                      [d["code"] for d in report["diagnostics"]])

    def test_corrupt_registry_is_registry_invalid(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", "{ broken")
        self._commit_all()
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertIn("REGISTRY_INVALID",
                      [d["code"] for d in report["diagnostics"]])

    def test_wrong_digest_is_registry_changed_not_pass(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        wrong = hashlib.sha256(b"old-agreement").hexdigest()
        code, output, _ = self._run(
            ["check", "--registry-sha256", wrong, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertIn("REGISTRY_CHANGED",
                      [d["code"] for d in report["diagnostics"]])
        self.assertEqual(report["results"], [])

    def test_malformed_digest_argument_is_cli_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(
                ["check", "--registry-sha256", "nothex", "--format", "json"])
        self.assertEqual(ctx.exception.code, 2)

    def test_modifier_untracked_caller_counts(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("untracked_use.py", CALLER)
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["results"][0]["status"], "VIOLATED")

    def test_context_reports_active_rule_without_analysis(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        code, output, _ = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(report["complete"])
        self.assertEqual(report["command"], "context")
        self.assertEqual(len(report["rules"]), 1)
        self.assertEqual(report["rules"][0]["id"], "TEMP-001")

    def test_context_text_with_one_rule(self):
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        code, output, _ = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "text"])
        self.assertEqual(code, 0)
        self.assertIn("TEMP-001", output)
        self.assertIn("demo.workaround.fallback", output)

    def test_staged_then_edited_again_still_counts_working_tree(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/use.py", CALLER)
        self._git("add", "--all")
        # second edit keeps the call but changes lines: still VIOLATED
        self._write("src/demo/use.py",
                    "# commentary\n\n" + CALLER)
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(report["results"][0]["status"], "VIOLATED")

    def test_subdirectory_cwd_resolves_repository_root(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        os.chdir(self.root / "src")
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertTrue(report["complete"])

    def test_repository_without_head_reports_incomplete_not_crash(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        # no commit at all: HEAD missing
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["status"], "OPEN")
        self.assertIsNone(report["results"][0]["added_consumers"])
        self.assertIsNone(report["base_commit"])

    def test_deleted_protected_function_is_unverified_exit_two(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", "def helper():\n    return 0\n")
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["status"], "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED",
                      [d["code"] for d in report["diagnostics"]])

    def test_syntax_error_elsewhere_keeps_violation_but_incomplete(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/use.py", CALLER)
        self._write("src/demo/broken.py", "def broken(:\n")
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["status"], "VIOLATED")
        self.assertIn("PYTHON_SYNTAX_ERROR",
                      [d["code"] for d in report["diagnostics"]])

    def test_f03_head_missing_symbol_keeps_current_open_but_incomplete(self):
        """F03: HEAD has only other(); working tree has correct fallback and no
        caller. Current OPEN is kept, but the baseline symbol is unknown, so
        global complete=false, exit 2, added_consumers=null, baseline diag kept."""
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", "def other():\n    return 1\n")
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/workaround.py",
                    "def fallback():\n    return 0\n")
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["status"], "OPEN")
        self.assertIsNone(report["results"][0]["added_consumers"])
        baseline_diags = [d for d in report["diagnostics"]
                          if d["code"] == "SYMBOL_UNVERIFIED"]
        self.assertTrue(baseline_diags)

    def test_f03_head_protected_file_syntax_error_is_incomplete(self):
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", "def fallback(:\n")
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()
        self._write("src/demo/workaround.py",
                    "def fallback():\n    return 0\n")
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["results"][0]["status"], "OPEN")
        self.assertIsNone(report["results"][0]["added_consumers"])

    def test_f14_baseline_syntax_error_keeps_real_reason_and_path(self):
        """F14: HEAD's baseline_bad.py has the syntax error; the working tree
        version is valid; the protected file is valid on both sides. The real
        PYTHON_SYNTAX_ERROR with the offending path must be kept, not replaced
        by a generic protected-file SYMBOL_UNVERIFIED."""
        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._write("baseline_bad.py", "def broken(:\n")
        self._commit_all()
        self._write("baseline_bad.py", "def fixed():\n    return 0\n")
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        syntax_diags = [d for d in report["diagnostics"]
                        if d["code"] == "PYTHON_SYNTAX_ERROR"]
        for diagnostic in report["diagnostics"]:
            self.assertEqual(set(diagnostic), {"code", "message", "path"})
        self.assertTrue(syntax_diags)
        self.assertTrue(any(d.get("path") == "baseline_bad.py"
                            for d in syntax_diags))

    def test_f04_context_missing_registry_json_shape_and_exit(self):
        code, output, _ = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertIsInstance(report, dict)
        self.assertFalse(report["complete"])
        self.assertEqual(report["rules"], [])
        self.assertIsNone(report["registry_digest"])
        self.assertTrue(report["diagnostics"])

    def test_f04_context_missing_registry_text_no_traceback(self):
        code, output, error = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "text"])
        self.assertEqual(code, 2)
        self.assertIn("REGISTRY", output + error)
        self.assertNotIn("Traceback", output + error)

    def test_f04_context_corrupt_registry_text_no_traceback(self):
        self._install_registry()
        (self.root / ".spellguard" / "rules.json").write_text("{ broken")
        code, output, error = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "text"])
        self.assertEqual(code, 2)
        self.assertIn("REGISTRY_INVALID", output + error)
        self.assertNotIn("Traceback", output + error)

    def test_f04_context_wrong_digest_text_no_traceback(self):
        self._install_registry()
        code, output, error = self._run(
            ["context", "--registry-sha256",
             hashlib.sha256(b"other-digest").hexdigest(), "--format", "text"])
        self.assertEqual(code, 2)
        self.assertIn("REGISTRY_CHANGED", output + error)
        self.assertNotIn("Traceback", output + error)

    def test_registry_changed_during_collection_is_detected(self):
        # registry content is modified between two internal reads in
        # review_windows; simulate by pipelining a mutation through the public
        # API instead of racing threads.
        from spellguard import window_review

        self._write("src/demo/__init__.py", "")
        self._write("src/demo/workaround.py", DEFINITION)
        self._write(".spellguard/rules.json", json.dumps(FIXTURE))
        self._commit_all()

        original = window_review.load_registry_twice

        def mutated(root, digest):
            registry = root / ".spellguard" / "rules.json"
            registry.write_text(
                json.dumps({"schema_version": 1, "rules": []}),
                encoding="utf-8")
            return original(root, digest)

        window_review.load_registry_twice = mutated
        try:
            code, output, _ = self._run(
                ["check", "--registry-sha256", self.digest, "--format", "json"])
        finally:
            window_review.load_registry_twice = original
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete"])
        self.assertIn("REGISTRY_CHANGED",
                      [d["code"] for d in report["diagnostics"]])


class MultiRuleCliTest(unittest.TestCase):
    """S-A03/S-A04 CLI: one snapshot evaluates every rule; an unknown rule does
    not hide another rule's confirmed violation."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "src" / "demo").mkdir(parents=True)
        self.previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)
        self._git("init", "-q")
        self._git("config", "user.email", "s@example.invalid")
        self._git("config", "user.name", "S")
        (self.root / "src" / "demo" / "__init__.py").write_text("")
        (self.root / "src" / "demo" / "workaround.py").write_text(
            "def fallback():\n    return 0\n")
        (self.root / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\n"
            "def run():\n    return f()\n")
        self.fixture = {"schema_version": 1, "rules": [
            {"id": "TEMP-001", "classification": "temporary",
             "lifecycle": "ACTIVE", "reason": "migration helper",
             "desired_state": "remove later", "window": "no_external_callers",
             "resolution_reason": None,
             "protected_symbol": {"path": "src/demo/workaround.py",
                                  "symbol": "fallback", "source_root": "src"}},
            {"id": "TEMP-002", "classification": "temporary",
             "lifecycle": "ACTIVE", "reason": "second helper",
             "desired_state": "remove later", "window": "no_external_callers",
             "resolution_reason": None,
             "protected_symbol": {"path": "src/demo/missing.py",
                                  "symbol": "gone", "source_root": "src"}},
        ]}
        payload = json.dumps(self.fixture, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")).encode()
        self.digest = hashlib.sha256(payload).hexdigest()
        (self.root / ".spellguard").mkdir()
        (self.root / ".spellguard" / "rules.json").write_bytes(payload)
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def _git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.root)] + list(arguments),
                              check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

    def _run(self, argv):
        import contextlib
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_check_reports_every_rule_independently(self):
        code, output, _ = self._run(
            ["check", "--registry-sha256", self.digest, "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertEqual([r["rule_id"] for r in report["results"]],
                         ["TEMP-001", "TEMP-002"])
        by_id = {r["rule_id"]: r for r in report["results"]}
        self.assertEqual(by_id["TEMP-001"]["status"], "VIOLATED")
        self.assertTrue(by_id["TEMP-001"]["consumers"])
        self.assertEqual(by_id["TEMP-002"]["status"], "UNVERIFIED")

    def test_context_lists_all_active_rules(self):
        code, output, _ = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "json"])
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual([r["id"] for r in report["rules"]],
                         ["TEMP-001", "TEMP-002"])
        text_code, text_output, _ = self._run(
            ["context", "--registry-sha256", self.digest, "--format", "text"])
        self.assertEqual(text_code, 0)
        self.assertIn("2 temporary rules", text_output)
        self.assertIn("TEMP-001", text_output)
        self.assertIn("TEMP-002", text_output)


if __name__ == "__main__":
    unittest.main()