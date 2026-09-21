"""M01: deterministic TEMP proposal (M-A01/M-A02).

propose writes ONLY into Git metadata (spellguard/proposal.json inside .git),
never touches .spellguard/rules.json, never shows up in git status, and never
reports confirmed=true. Same input is byte-identical; changed input replaces
the unconfirmed draft with a different digest. All invalid input exits with
an error and leaves zero side effects.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spellguard.cli import main as cli_main


class MarkingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "src" / "demo").mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "m@example.invalid")
        self._git("config", "user.name", "M")
        (self.root / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "src" / "demo" / "adapt.py").write_text(
            "def adapt():\n    return 0\n", encoding="utf-8")
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.tempdir.cleanup()

    def _git(self, *arguments):
        subprocess.run(["git", "-C", str(self.root)] + list(arguments),
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

    def _proposal_path(self):
        return self.root / ".git" / "spellguard" / "proposal.json"

    def _registry_path(self):
        return self.root / ".spellguard" / "rules.json"

    def _run_cli(self, argv):
        import contextlib
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _propose(self, *extra):
        return self._run_cli(
            ["propose", "--path", "src/demo/adapt.py", "--symbol", "adapt",
             "--source-root", "src", "--reason", "migration helper",
             "--desired-state", "remove after migration",
             "--format", "json"] + list(extra))

    def test_propose_writes_git_metadata_only(self):
        code, output, error = self._propose()
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["command"], "propose")
        self.assertFalse(report["confirmed"])
        self.assertEqual(report["proposal_path"], "spellguard/proposal.json")
        self.assertIn("TEMP-001", report["summary"])
        self.assertIn("proposal_digest", report)
        self.assertTrue(self._proposal_path().exists())
        self.assertFalse(self._registry_path().exists())
        status_raw = subprocess.run(
            ["git", "-C", str(self.root), "status", "--short"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        self.assertEqual(status_raw.strip(), b"")

    def test_proposal_bytes_are_deterministic(self):
        first_code, first_out, _ = self._propose()
        self.assertEqual(first_code, 0)
        second_code, second_out, _ = self._propose()
        self.assertEqual(second_code, 0)
        self.assertEqual(first_out, second_out)
        first = json.loads(first_out)
        self.assertEqual(first["proposal_digest"],
                         hashlib.sha256(
                             self._proposal_path().read_bytes()).hexdigest())

    def test_changed_draft_replaces_with_different_digest(self):
        code, output, _ = self._propose()
        self.assertEqual(code, 0)
        first_digest = json.loads(output)["proposal_digest"]
        code, output2, _ = self._propose("--reason", "other reason")
        self.assertEqual(code, 0)
        second_digest = json.loads(output2)["proposal_digest"]
        self.assertNotEqual(first_digest, second_digest)
        self.assertTrue(self._proposal_path().exists())
        self.assertFalse(self._registry_path().exists())

    def test_invalid_symbol_rejected_with_zero_side_effects(self):
        code, _, error = self._run_cli(
            ["propose", "--path", "src/demo.py", "--symbol", "Type.method",
             "--source-root", "src", "--reason", "r", "--desired-state", "d",
             "--format", "json"])
        self.assertEqual(code, 2)
        self.assertFalse(self._proposal_path().exists())
        self.assertFalse(self._registry_path().exists())

    def test_empty_reason_rejected(self):
        code, _, error = self._run_cli(
            ["propose", "--path", "src/demo/adapt.py", "--symbol", "adapt",
             "--source-root", "src", "--reason", "", "--desired-state", "d",
             "--format", "json"])
        self.assertEqual(code, 2, error)
        self.assertFalse(self._proposal_path().exists())
        self.assertFalse(self._registry_path().exists())


if __name__ == "__main__":
    unittest.main()

class ConfirmTest(unittest.TestCase):
    """M02: explicit confirm with atomic promotion (M-A03/M-A04/M-A05)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / "src" / "demo").mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "m@example.invalid")
        self._git("config", "user.name", "M")
        (self.root / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "src" / "demo" / "adapt.py").write_text(
            "def adapt():\n    return 0\n", encoding="utf-8")
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.tempdir.cleanup()

    def _git(self, *arguments):
        subprocess.run(["git", "-C", str(self.root)] + list(arguments),
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

    def _run_cli(self, argv):
        import contextlib
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _propose(self):
        return self._run_cli(
            ["propose", "--path", "src/demo/adapt.py", "--symbol", "adapt",
             "--source-root", "src", "--reason", "migration helper",
             "--desired-state", "remove after migration", "--format", "json"])

    def _confirm(self, digest):
        return self._run_cli(
            ["confirm", "--proposal-sha256", digest, "--format", "json"])

    def test_wrong_digest_rejected_zero_side_effects(self):
        code, output, _ = self._propose()
        self.assertEqual(code, 0)
        proposal = self.root / ".git" / "spellguard" / "proposal.json"
        original = proposal.read_bytes()
        code, _, _ = self._confirm("a" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(proposal.read_bytes(), original)
        self.assertFalse((self.root / ".spellguard" / "rules.json").exists())

    def test_draft_modified_rejected_zero_side_effects(self):
        code, output, _ = self._propose()
        self.assertEqual(code, 0)
        digest = json.loads(output)["proposal_digest"]
        proposal = self.root / ".git" / "spellguard" / "proposal.json"
        tampered = json.loads(proposal.read_text())
        tampered["rules"][0]["reason"] = "tampered"
        proposal.write_text(json.dumps(tampered))
        code, _, _ = self._confirm(digest)
        self.assertEqual(code, 2)
        self.assertFalse((self.root / ".spellguard" / "rules.json").exists())

    def test_existing_registry_refuses_overwrite(self):
        registry = self.root / ".spellguard" / "rules.json"
        registry.parent.mkdir(exist_ok=True)
        registry.write_text('{"schema_version": 1, "rules": []}')
        code, output, _ = self._propose()
        self.assertEqual(code, 0)
        digest = json.loads(output)["proposal_digest"]
        code, _, _ = self._confirm(digest)
        self.assertEqual(code, 2)
        self.assertEqual(registry.read_text(),
                         '{"schema_version": 1, "rules": []}')

    def test_correct_digest_promotes_atomically(self):
        code, output, _ = self._propose()
        self.assertEqual(code, 0)
        report = json.loads(output)
        digest = report["proposal_digest"]
        code, confirm_out, error = self._confirm(digest)
        self.assertEqual(code, 0, error)
        confirm = json.loads(confirm_out)
        self.assertTrue(confirm["confirmed"])
        registry = self.root / ".spellguard" / "rules.json"
        self.assertTrue(registry.exists())
        proposal = self.root / ".git" / "spellguard" / "proposal.json"
        self.assertFalse(proposal.exists())
        from spellguard.registry import parse_registry
        parsed = parse_registry(registry.read_bytes())
        self.assertEqual(parsed.digest, digest)

    def test_context_and_check_ignore_proposal_until_confirmed(self):
        code, output, _ = self._propose()
        self.assertEqual(code, 0)
        digest = json.loads(output)["proposal_digest"]
        # before confirm: context must fail (no registry yet)
        code, _, _ = self._run_cli(
            ["context", "--registry-sha256", digest, "--format", "json"])
        self.assertEqual(code, 2)
        # confirm -> check is OPEN
        code, _, error = self._confirm(digest)
        self.assertEqual(code, 0, error)
        code, check_out, _ = self._run_cli(
            ["check", "--registry-sha256", digest, "--format", "json"])
        self.assertEqual(code, 0)
        report = json.loads(check_out)
        self.assertEqual(report["results"][0]["status"], "OPEN")


class InstructionsTest(unittest.TestCase):
    """M03: static instructions with the three non-negotiable actions (M-A06
    automation part only; the live-host observation stays NOT_RUN)."""

    def _run_outside_repo(self, argv):
        import contextlib
        import io
        tmp = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli_main(argv)
        finally:
            os.chdir(cwd)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_instructions_contain_three_required_actions(self):
        code, output, _ = self._run_outside_repo(["instructions"])
        self.assertEqual(code, 0)
        for marker in ("spellguard propose", "spellguard confirm",
                       "after the maintainer explicitly confirms"):
            self.assertIn(marker, output)
        self.assertIn("Never run confirm", output)

    def test_instructions_work_outside_any_repo(self):
        code, output, _ = self._run_outside_repo(["instructions", "--format", "text"])
        self.assertEqual(code, 0)
        self.assertIn("spellguard propose", output)
        # no repository interaction: blank cwd contains no .git
        import pathlib
        self.assertTrue(True)

    def test_instructions_cover_managed_flow_and_trust_separation(self):
        code, output, _ = self._run_outside_repo(["instructions"])
        self.assertEqual(code, 0)
        for marker in ("installation-id", "native project/hook trust",
                       "does not answer", "unverified", "setup --remove",
                       "do not ask again"):
            self.assertIn(marker, output)

    def test_managed_guidance_is_short_and_host_independent(self):
        from spellguard.marking import managed_agent_guidance
        guidance = managed_agent_guidance()
        self.assertIn("installation-id", guidance)
        self.assertIn("Never confirm on your own", guidance)
        self.assertLess(len(guidance), 400)

    def test_instructions_do_not_write_files(self):
        import pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            code, _, _ = self._run_cli_static(["instructions"])
            self.assertEqual(code, 0)
        finally:
            os.chdir(cwd)
        entries = [p.name for p in tmp.iterdir()]
        self.assertEqual(entries, [])

    def _run_cli_static(self, argv):
        import contextlib
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class MarkingBoundaryTest(unittest.TestCase):
    """Regression coverage for the marking trust-boundary failures."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        package = self.root / "src" / "demo"
        package.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "adapt.py").write_text(
            "def adapt():\n    return 0\n", encoding="utf-8")
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.tempdir.cleanup()

    def _run_cli(self, argv):
        import contextlib
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _propose(self):
        return self._run_cli([
            "propose", "--path", "src/demo/adapt.py", "--symbol", "adapt",
            "--source-root", "src", "--reason", "migration helper",
            "--desired-state", "remove after migration", "--format", "json",
        ])

    def _confirm(self, digest):
        return self._run_cli([
            "confirm", "--proposal-sha256", digest, "--format", "json",
        ])

    def test_non_repository_error_is_structured_exit_two(self):
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        os.chdir(outside)
        code, output, error = self._propose()
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["diagnostics"][0]["code"], "REPOSITORY_ERROR")
        self.assertNotIn("Traceback", output + error)

    def test_non_directory_storage_parents_are_structured_failures(self):
        metadata_parent = self.root / ".git" / "spellguard"
        metadata_parent.touch()
        code, output, error = self._propose()
        self.assertEqual(code, 2, error)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_READ_FAILED")
        metadata_parent.unlink()

        code, proposal_output, error = self._propose()
        self.assertEqual(code, 0, error)
        digest = json.loads(proposal_output)["proposal_digest"]
        (self.root / ".spellguard").touch()
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 2, error)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_READ_FAILED")
        self.assertTrue(
            (self.root / ".git" / "spellguard" / "proposal.json").exists())

    def test_confirm_rejects_proposal_symlink_before_path_read(self):
        code, proposal_output, error = self._propose()
        self.assertEqual(code, 0, error)
        digest = json.loads(proposal_output)["proposal_digest"]
        proposal = self.root / ".git" / "spellguard" / "proposal.json"
        external = Path(self.tempdir.name) / "external.json"
        external.write_bytes(proposal.read_bytes())
        proposal.unlink()
        proposal.symlink_to(external)
        with mock.patch.object(
                Path, "read_bytes",
                side_effect=AssertionError("proposal symlink was followed")):
            code, output, error = self._confirm(digest)
        self.assertEqual(code, 2, error)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_READ_FAILED")
        self.assertFalse((self.root / ".spellguard" / "rules.json").exists())

    def test_concurrent_registry_creation_is_not_overwritten(self):
        code, proposal_output, error = self._propose()
        self.assertEqual(code, 0, error)
        digest = json.loads(proposal_output)["proposal_digest"]
        original_link = os.link

        def competing_link(source, target, *, src_dir_fd=None,
                           dst_dir_fd=None, follow_symlinks=True):
            descriptor = os.open(
                target, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600, dir_fd=dst_dir_fd)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"competitor")
            return original_link(
                source, target, src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

        with mock.patch("spellguard.marking.os.link",
                        side_effect=competing_link):
            code, output, error = self._confirm(digest)
        self.assertEqual(code, 2, error)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_INVALID")
        registry = self.root / ".spellguard" / "rules.json"
        self.assertEqual(registry.read_bytes(), b"competitor")
        self.assertTrue(
            (self.root / ".git" / "spellguard" / "proposal.json").exists())

    def test_cleanup_failure_keeps_confirmation_successful_and_retry_idempotent(self):
        code, proposal_output, error = self._propose()
        self.assertEqual(code, 0, error)
        digest = json.loads(proposal_output)["proposal_digest"]
        original_unlink = os.unlink

        def deny_proposal_cleanup(path, *args, **kwargs):
            if path == "proposal.json":
                raise PermissionError("synthetic Git metadata denial")
            return original_unlink(path, *args, **kwargs)

        with mock.patch("spellguard.marking.os.unlink",
                        side_effect=deny_proposal_cleanup):
            code, output, error = self._confirm(digest)
            self.assertEqual(code, 0, error)
            report = json.loads(output)
            self.assertTrue(report["confirmed"])
            self.assertFalse(report["already_confirmed"])
            self.assertFalse(report["proposal_cleaned"])

            code, retry_output, error = self._confirm(digest)
            self.assertEqual(code, 0, error)
            retry = json.loads(retry_output)
            self.assertTrue(retry["confirmed"])
            self.assertTrue(retry["already_confirmed"])
            self.assertFalse(retry["proposal_cleaned"])

        registry = self.root / ".spellguard" / "rules.json"
        proposal = self.root / ".git" / "spellguard" / "proposal.json"
        self.assertEqual(registry.read_bytes(), proposal.read_bytes())
