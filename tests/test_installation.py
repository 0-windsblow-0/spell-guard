"""S01 installation control plane (S-A01).

preview (plan_setup) is side-effect free. apply_setup executes exactly the plan
whose digest the caller echoes back, is idempotent, preserves foreign hook
entries and top-level fields, and resumes an interrupted setup/removal through
the pending record. Every conflict (corrupt JSON, TOML hooks, legacy manual
entry, concurrent edit, link, traversal, unsupported worktree, oversize or
unsafe permissions) is rejected without touching unrelated content.
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spellguard import installation
from spellguard.cli import main as cli_main
from spellguard.installation import InstallationError


def _registry_payload(reason="migration helper"):
    return json.dumps(
        {
            "schema_version": 1,
            "rules": [{
                "id": "TEMP-001",
                "classification": "temporary",
                "lifecycle": "ACTIVE",
                "reason": reason,
                "desired_state": "remove after migration",
                "protected_symbol": {
                    "path": "src/demo/adapt.py",
                    "symbol": "adapt",
                    "source_root": "src",
                },
                "window": "no_external_callers",
                "resolution_reason": None,
            }],
        },
        ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _InstallationBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        (self.repo / "src" / "demo").mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "s@example.invalid")
        self._git("config", "user.name", "S")
        (self.repo / "src" / "demo" / "__init__.py").write_text(
            "", encoding="utf-8")
        (self.repo / "src" / "demo" / "adapt.py").write_text(
            "def adapt():\n    return 0\n", encoding="utf-8")
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")
        self.entry = self.base / "bin" / "spellguard"
        self.entry.parent.mkdir()
        self.entry.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SPELLGUARD_TEST_OUT\"\n",
            encoding="utf-8")
        self.entry.chmod(0o755)
        environment = mock.patch.dict(os.environ, {
            "SPELLGUARD_HOME": str(self.home),
            "SPELLGUARD_ENTRY": str(self.entry),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.previous_cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.previous_cwd)

    def _git(self, *arguments):
        subprocess.run(["git", "-C", str(self.repo)] + list(arguments),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def _run(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _preview(self, remove=False):
        argv = ["setup", "--host", "codex", "--format", "json"]
        if remove:
            argv.insert(1, "--remove")
        return self._run(argv)

    def _apply(self, digest, remove=False):
        argv = ["setup", "--apply", digest, "--format", "json"]
        if remove:
            argv.insert(1, "--remove")
        return self._run(argv)

    def _registry_path(self):
        return self.repo / ".spellguard" / "rules.json"

    def _write_registry(self):
        self._registry_path().parent.mkdir(exist_ok=True)
        payload = _registry_payload()
        self._registry_path().write_bytes(payload)
        from spellguard.registry import parse_registry
        return parse_registry(payload).digest

    def _hooks_path(self):
        return self.repo / ".codex" / "hooks.json"

    def _write_hooks(self, obj):
        self._hooks_path().parent.mkdir(exist_ok=True)
        self._hooks_path().write_text(
            json.dumps(obj, indent=2) + "\n", encoding="utf-8")

    def _external(self, installation_id):
        return self.home / "installations" / installation_id

    def _plan(self, remove=False):
        code, output, error = self._preview(remove=remove)
        self.assertEqual(code, 0, error or output)
        return json.loads(output)


class PreviewTest(_InstallationBase):
    def test_preview_is_side_effect_free_and_shows_adopted_intents(self):
        digest = self._write_registry()
        original = self._registry_path().read_bytes()
        plan = self._plan()
        self.assertEqual(plan["command"], "setup")
        self.assertEqual(plan["operation"], "install")
        self.assertEqual(plan["host"], "codex")
        self.assertEqual(plan["config_path"], ".codex/hooks.json")
        self.assertTrue(plan["requires_host_trust"])
        self.assertEqual(plan["registry_state"], "unbound")
        self.assertEqual(plan["registry_digest"], digest)
        self.assertEqual([rule["id"] for rule in plan["adopted_rules"]], ["TEMP-001"])
        actions = [change["action"] for change in plan["changes"]]
        self.assertEqual(actions, ["create_file", "add_hook", "add_hook",
                                   "write_manifest"])
        self.assertEqual(len(plan["plan_digest"]), 64)
        self.assertFalse(self._hooks_path().exists())
        self.assertFalse((self.home / "installations").exists())
        self.assertEqual(self._registry_path().read_bytes(), original)

    def test_apply_binds_digest_and_second_apply_is_idempotent(self):
        plan = self._plan()
        code, output, error = self._apply(plan["plan_digest"])
        self.assertEqual(code, 0, error or output)
        applied = json.loads(output)
        self.assertTrue(applied["applied"])
        self.assertFalse(applied["already_applied"])
        self.assertEqual(applied["lifecycle"], "active")
        self.assertFalse(applied["host_verified"])
        first = self._hooks_path().read_bytes()
        config = json.loads(first)
        for event in ("UserPromptSubmit", "Stop"):
            self.assertEqual(len(config["hooks"][event]), 1)
        code, output, error = self._apply(plan["plan_digest"])
        self.assertEqual(code, 0, error or output)
        repeat = json.loads(output)
        self.assertTrue(repeat["already_applied"])
        self.assertEqual(self._hooks_path().read_bytes(), first)

    def test_apply_rejects_unknown_or_malformed_digest(self):
        self._plan()
        code, output, _ = self._apply("0" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "SETUP_PLAN_CHANGED")
        self.assertFalse(self._hooks_path().exists())
        code, output, _ = self._apply("not-a-digest")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "SETUP_PLAN_INVALID")


class PreserveTest(_InstallationBase):
    def test_uninstall_preserves_unrelated_empty_matcher_group(self):
        group = {"matcher": "user-placeholder", "hooks": [],
                 "description": "keep this user-owned group"}
        self._write_hooks({"hooks": {"Stop": [group]}})
        install = self._plan()
        self.assertEqual(self._apply(install["plan_digest"])[0], 0)
        removal = self._plan(remove=True)
        self.assertEqual(self._apply(removal["plan_digest"])[0], 0)
        self.assertEqual(json.loads(self._hooks_path().read_text())["hooks"]["Stop"],
                         [group])

    def test_uninstall_preserves_other_hooks_and_fields(self):
        other = {"type": "command", "command": "/usr/bin/echo hello",
                 "timeout": 5}
        self._write_hooks({
            "description": "user config",
            "custom": {"nested": [1, 2, 3]},
            "hooks": {
                "UserPromptSubmit": [{"matcher": "x", "hooks": [other]}],
                "Stop": [],
            },
        })
        install = self._plan()
        self.assertEqual(self._apply(install["plan_digest"])[0], 0)
        install_remove = self._plan(remove=True)
        code, output, error = self._apply(install_remove["plan_digest"], remove=True)
        self.assertEqual(code, 0, error or output)
        result = json.loads(output)
        self.assertEqual(result["lifecycle"], "disabled")
        config = json.loads(self._hooks_path().read_text())
        self.assertEqual(config["description"], "user config")
        self.assertEqual(config["custom"], {"nested": [1, 2, 3]})
        self.assertEqual(config["hooks"]["UserPromptSubmit"],
                         [{"matcher": "x", "hooks": [other]}])
        self.assertEqual(config["hooks"]["Stop"], [])

    def test_uninstall_deletes_only_a_file_it_created(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        self.assertTrue(self._hooks_path().exists())
        remove = self._plan(remove=True)
        self.assertIn("delete_file",
                      [change["action"] for change in remove["changes"]])
        self.assertEqual(self._apply(remove["plan_digest"], remove=True)[0], 0)
        self.assertFalse(self._hooks_path().exists())

    def test_remove_without_record_is_idempotent_noop(self):
        remove = self._plan(remove=True)
        self.assertEqual(remove["changes"], [])
        code, output, error = self._apply(remove["plan_digest"], remove=True)
        self.assertEqual(code, 0, error or output)
        self.assertFalse(self._hooks_path().exists())


class ConflictTest(_InstallationBase):
    def test_corrupt_hooks_json_rejected(self):
        self._hooks_path().parent.mkdir(parents=True)
        self._hooks_path().write_text("{not json", encoding="utf-8")
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "CONFIG_INVALID")

    def test_toml_hooks_conflict_rejected(self):
        toml = self.repo / ".codex" / "config.toml"
        toml.parent.mkdir(parents=True)
        toml.write_text("[hooks]\nenabled = true\n", encoding="utf-8")
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "CONFIG_TOML_CONFLICT")
        self.assertFalse(self._hooks_path().exists())

    def test_legacy_manual_spellguard_entry_rejected(self):
        self._write_hooks({"hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": "python /old/codex_repair_window.py --registry-sha256 abc",
            "timeout": 15,
        }]}]}})
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "LEGACY_HOOK_CONFLICT")

    def test_concurrent_config_edit_rejected_without_overwrite(self):
        plan = self._plan()
        self._write_hooks({"hooks": {"UserPromptSubmit": [], "Stop": []},
                           "extra": [1]})
        edited = self._hooks_path().read_bytes()
        code, output, _ = self._apply(plan["plan_digest"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "SETUP_PLAN_CHANGED")
        self.assertEqual(self._hooks_path().read_bytes(), edited)

    def test_symlinked_hooks_file_rejected(self):
        self._hooks_path().parent.mkdir(parents=True)
        target = self.base / "outside.json"
        target.write_text("{}", encoding="utf-8")
        self._hooks_path().symlink_to(target)
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertIn(json.loads(output)["diagnostics"][0]["code"],
                      ("CONFIG_UNSAFE", "CONFIG_INVALID"))

    def test_symlinked_config_directory_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "hooks.json").write_text("{}", encoding="utf-8")
        (self.repo / ".codex").symlink_to(outside, target_is_directory=True)
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertIn(json.loads(output)["diagnostics"][0]["code"],
                      ("CONFIG_UNSAFE", "CONFIG_INVALID"))

    def test_oversize_hooks_json_rejected(self):
        self._write_hooks({"hooks": {}, "pad": "x" * 70000})
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "CONFIG_TOO_LARGE")

    def test_unsafe_external_permissions_rejected(self):
        plan = self._plan()
        directory = self._external(plan["installation_id"])
        directory.mkdir(parents=True)
        os.chmod(directory, 0o755)
        code, output, _ = self._apply(plan["plan_digest"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "INSTALLATION_PERMISSIONS")

    def test_nested_repository_rejected(self):
        outer = self.base / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(outer)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-q", str(inner)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chdir(inner)
        try:
            code, output, _ = self._preview()
        finally:
            os.chdir(self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "NESTED_REPOSITORY")

    def test_linked_worktree_rejected(self):
        worktree = self.base / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add",
                        str(worktree)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chdir(worktree)
        try:
            code, output, _ = self._preview()
        finally:
            os.chdir(self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "WORKTREE_UNSUPPORTED")

    def test_cwd_outside_repository_rejected(self):
        outside = self.base / "nowhere"
        outside.mkdir()
        os.chdir(outside)
        try:
            code, output, _ = self._preview()
        finally:
            os.chdir(self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REPOSITORY_ERROR")


class EntryQuotingTest(_InstallationBase):
    def test_hostile_entry_path_runs_quoted_without_injection(self):
        weird = self.base / "odd $(touch CANARY) 'q' \"d\""
        weird.mkdir()
        script = weird / "spellguard"
        script.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SPELLGUARD_TEST_OUT\"\n",
            encoding="utf-8")
        script.chmod(0o755)
        with mock.patch.dict(os.environ, {"SPELLGUARD_ENTRY": str(script)}):
            plan = self._plan()
        output = self.base / "args.txt"
        canary = self.base / "CANARY"
        environment = dict(os.environ, SPELLGUARD_TEST_OUT=str(output))
        subprocess.run(["sh", "-c", plan["hook_command"]], cwd=str(self.base),
                       env=environment, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertFalse(canary.exists())
        arguments = output.read_text().splitlines()
        self.assertEqual(arguments[0], "hook")
        self.assertEqual(arguments[1:3], ["--host", "codex"])
        self.assertEqual(arguments[3], "--installation-id")
        self.assertEqual(arguments[4], plan["installation_id"])


class FaultInjectionTest(_InstallationBase):
    def _flaky_commit(self):
        real = installation._commit_record
        state = {"calls": 0}

        def commit(directory, plan, repo_root):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError(
                    "INSTALLATION_WRITE_FAILED", "synthetic commit failure")
            return real(directory, plan, repo_root)

        return commit, state

    def test_install_resumes_after_config_written_before_manifest(self):
        plan = self._plan()
        commit, _state = self._flaky_commit()
        with mock.patch("spellguard.installation._commit_record",
                        side_effect=commit):
            code, _output, _error = self._apply(plan["plan_digest"])
            self.assertEqual(code, 2)
        self.assertTrue(self._hooks_path().exists())
        self.assertFalse(
            (self._external(plan["installation_id"]) / "installation.json").exists())
        self.assertTrue(
            (self._external(plan["installation_id"]) / "setup-pending.json").exists())
        code, output, error = self._apply(plan["plan_digest"])
        self.assertEqual(code, 0, error or output)
        self.assertFalse(
            (self._external(plan["installation_id"]) / "setup-pending.json").exists())
        record = json.loads(
            (self._external(plan["installation_id"]) / "installation.json").read_text())
        self.assertEqual(record["lifecycle"], "active")

    def test_remove_resumes_after_entries_removed_before_manifest_disabled(self):
        install = self._plan()
        self.assertEqual(self._apply(install["plan_digest"])[0], 0)
        remove = self._plan(remove=True)
        commit, _state = self._flaky_commit()
        with mock.patch("spellguard.installation._commit_record",
                        side_effect=commit):
            code, _output, _error = self._apply(remove["plan_digest"], remove=True)
            self.assertEqual(code, 2)
        self.assertFalse(self._hooks_path().exists())
        code, output, error = self._apply(remove["plan_digest"], remove=True)
        self.assertEqual(code, 0, error or output)
        record = json.loads(
            (self._external(install["installation_id"]) / "installation.json").read_text())
        self.assertEqual(record["lifecycle"], "disabled")

    def test_unrecognized_third_content_is_never_overwritten(self):
        plan = self._plan()
        commit, _state = self._flaky_commit()
        with mock.patch("spellguard.installation._commit_record",
                        side_effect=commit):
            self.assertEqual(self._apply(plan["plan_digest"])[0], 2)
        self._write_hooks({"hooks": {"UserPromptSubmit": [{"matcher": "third",
                                                           "hooks": []}]}})
        third = self._hooks_path().read_bytes()
        code, output, _ = self._apply(plan["plan_digest"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "SETUP_CONFLICT")
        self.assertEqual(self._hooks_path().read_bytes(), third)


class AgentChoiceTest(_InstallationBase):
    def test_no_registry_install_is_unregistered_absent(self):
        plan = self._plan()
        self.assertEqual(plan["registry_state"], "absent")
        self.assertIsNone(plan["registry_digest"])
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        code, output, error = self._run(["status", "--format", "json"])
        self.assertEqual(code, 0, error or output)
        status = json.loads(output)
        self.assertTrue(status["configured"])
        self.assertEqual(status["registry_state"], "absent")
        self.assertIsNone(status["registry_digest"])
        self.assertFalse(status["host_verified"])

    def test_unreadable_registry_blocks_install_and_is_not_absent(self):
        self._registry_path().parent.mkdir(exist_ok=True)
        self._registry_path().write_text("{broken", encoding="utf-8")
        code, output, _ = self._preview()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_READ_FAILED")
        self.assertFalse(self._hooks_path().exists())
        # after a clean install, corruption surfaces as unreadable, not absent
        self._registry_path().unlink()
        install = self._plan()
        self.assertEqual(self._apply(install["plan_digest"])[0], 0)
        self._registry_path().write_text("{broken", encoding="utf-8")
        code, output, _ = self._run(["status", "--format", "json"])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["registry_state"], "unreadable")
        import shutil
        shutil.rmtree(self.repo / ".spellguard")
        (self.repo / ".spellguard").write_text("not a directory",
                                               encoding="utf-8")
        code, output, _ = self._run(["status", "--format", "json"])
        self.assertEqual(json.loads(output)["registry_state"], "unreadable")


class StatusTest(_InstallationBase):
    def test_status_before_install_is_not_configured(self):
        code, output, error = self._run(["status", "--format", "json"])
        self.assertEqual(code, 0, error or output)
        status = json.loads(output)
        self.assertFalse(status["configured"])
        self.assertIsNone(status["lifecycle"])
        self.assertEqual(status["registry_state"], "absent")

    def test_status_after_install_and_disable(self):
        self._write_registry()
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        code, output, _ = self._run(["status", "--format", "json"])
        status = json.loads(output)
        self.assertTrue(status["configured"])
        self.assertEqual(status["lifecycle"], "active")
        self.assertEqual(status["registry_state"], "bound")
        remove = self._plan(remove=True)
        self.assertEqual(self._apply(remove["plan_digest"], remove=True)[0], 0)
        code, output, _ = self._run(["status", "--format", "json"])
        status = json.loads(output)
        self.assertFalse(status["configured"])
        self.assertEqual(status["lifecycle"], "disabled")

    def test_rewritten_entry_is_reported_not_configured(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        config = json.loads(self._hooks_path().read_text())
        config["hooks"]["Stop"][0]["hooks"][0]["command"] += " --extra"
        self._write_hooks(config)
        code, output, _ = self._run(["status", "--format", "json"])
        status = json.loads(output)
        self.assertFalse(status["configured"])
        self.assertIn("LEGACY_HOOK_CONFLICT",
                      [item["code"] for item in status["diagnostics"]])


class ContractTest(_InstallationBase):
    def test_load_installation_rejects_foreign_id(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        with self.assertRaises(InstallationError) as raised:
            installation.load_installation(self.repo, "0" * 64)
        self.assertEqual(raised.exception.code, "INSTALLATION_UNKNOWN")

    def test_load_installation_missing_record(self):
        plan = self._plan()
        with self.assertRaises(InstallationError) as raised:
            installation.load_installation(self.repo, plan["installation_id"])
        self.assertEqual(raised.exception.code, "INSTALLATION_NOT_FOUND")

    def test_load_installation_returns_record(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        record = installation.load_installation(self.repo, plan["installation_id"])
        self.assertEqual(record["installation_id"], plan["installation_id"])
        self.assertEqual(record["lifecycle"], "active")
        self.assertEqual(record["hook_command"], plan["hook_command"])

    def test_config_file_untouched_by_preview_after_registry(self):
        self._write_registry()
        self._plan()
        self.assertFalse(self._hooks_path().exists())


class StatusExplanationTest(_InstallationBase):
    """S04: status explains not-configured / no-agreement / unverified
    separately instead of calling everything safe."""

    def _status_text(self):
        code, output, error = self._run(["status"])
        self.assertEqual(code, 0, error or output)
        return output

    def test_not_configured_is_explained(self):
        text = self._status_text()
        self.assertIn("未接入", text)
        self.assertIn("不代表安全", text)

    def test_configured_without_registry_is_not_safe(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        text = self._status_text()
        self.assertIn("已接入", text)
        self.assertIn("尚无已确认约定", text)
        self.assertIn("不代表安全", text)

    def test_bound_registry_is_explained(self):
        self._write_registry()
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        text = self._status_text()
        self.assertIn("已确认摘要一致", text)

    def test_unreadable_registry_is_not_absent(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        self._registry_path().parent.mkdir(parents=True, exist_ok=True)
        self._registry_path().write_text("{ broken", encoding="utf-8")
        text = self._status_text()
        self.assertIn("登记读取失败", text)
        self.assertNotIn("尚无已确认约定", text)

    def test_disabled_is_explained(self):
        plan = self._plan()
        self.assertEqual(self._apply(plan["plan_digest"])[0], 0)
        remove = self._plan(remove=True)
        self.assertEqual(self._apply(remove["plan_digest"], remove=True)[0], 0)
        text = self._status_text()
        self.assertIn("已停用", text)


if __name__ == "__main__":
    unittest.main()
