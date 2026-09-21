"""S02 managed confirmation transaction (S-A02/S-A03).

A managed proposal is an envelope in Git metadata and does not touch the
registry until the exact digest is confirmed. confirm_change binds the registry
preimage, writes an external pending record, writes the registry, commits the
external confirmation digest and clears pending; recover_confirmation only
replays an existing pending record. Interruptions at every persistence boundary
resume with the same authorization, unrecognized third content is never
overwritten, and a missing pending never creates trust.
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

from spellguard import confirmation
from spellguard.cli import main as cli_main
from spellguard.installation import InstallationError
from spellguard.registry import parse_registry


class _ManagedBase(unittest.TestCase):
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
        self.entry.write_text("#!/bin/sh\necho entry\n", encoding="utf-8")
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

    def _install(self):
        code, output, error = self._run(["setup", "--format", "json"])
        self.assertEqual(code, 0, error or output)
        plan = json.loads(output)
        code, output, error = self._run(
            ["setup", "--apply", plan["plan_digest"], "--format", "json"])
        self.assertEqual(code, 0, error or output)
        self.installation_id = plan["installation_id"]
        self.hooks_after_install = self._hooks_path().read_bytes()
        return plan

    def _hooks_path(self):
        return self.repo / ".codex" / "hooks.json"

    def _registry_path(self):
        return self.repo / ".spellguard" / "rules.json"

    def _external(self, name):
        return self.home / "installations" / self.installation_id / name

    def _propose_add(self, reason="migration helper", symbol="adapt"):
        return self._run([
            "propose", "--path", "src/demo/adapt.py", "--symbol", symbol,
            "--source-root", "src", "--reason", reason,
            "--desired-state", "remove after migration",
            "--installation-id", self.installation_id, "--format", "json"])

    def _propose_resolve(self, rule_id="TEMP-001", reason="migration done"):
        return self._run([
            "propose", "--resolve", rule_id, "--reason", reason,
            "--installation-id", self.installation_id, "--format", "json"])

    def _confirm(self, digest):
        return self._run([
            "confirm", "--proposal-sha256", digest,
            "--installation-id", self.installation_id, "--format", "json"])

    def _recover(self):
        return self._run(["recover", "--format", "json"])

    def _registry_digest(self):
        return parse_registry(self._registry_path().read_bytes()).digest

    def _write_external(self, name, obj):
        path = self._external(name)
        path.write_text(json.dumps(obj), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def _hook(self, payload):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            return self._run(["hook", "--host", "codex",
                              "--installation-id", self.installation_id])


class ProposalTest(_ManagedBase):
    def test_managed_proposal_does_not_take_effect(self):
        self._install()
        code, output, error = self._propose_add()
        self.assertEqual(code, 0, error or output)
        report = json.loads(output)
        self.assertFalse(report["confirmed"])
        self.assertEqual(report["operation"], "add")
        self.assertIsNone(report["expected_registry_digest"])
        self.assertFalse(self._registry_path().exists())
        code, check_out, _ = self._run([
            "check", "--registry-sha256", report["result_registry_digest"],
            "--format", "json"])
        self.assertEqual(code, 2)
        self.assertFalse(self._registry_path().exists())

    def test_confirm_links_registry_and_external_digest(self):
        self._install()
        code, output, _ = self._propose_add()
        proposal = json.loads(output)
        digest = proposal["proposal_digest"]
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 0, error or output)
        result = json.loads(output)
        self.assertTrue(result["confirmed"])
        self.assertFalse(result["already_confirmed"])
        self.assertFalse(result["recovery_required"])
        self.assertFalse(result["host_verified"])
        self.assertEqual(result["registry_digest"], proposal["result_registry_digest"])
        self.assertEqual(self._registry_digest(), result["registry_digest"])
        confirmation_record = json.loads(
            self._external("confirmation.json").read_text())
        self.assertEqual(confirmation_record["proposal_digest"], digest)
        self.assertEqual(confirmation_record["result_registry_digest"],
                         result["registry_digest"])
        installation_record = json.loads(
            self._external("installation.json").read_text())
        self.assertEqual(installation_record["adopted_registry_digest"],
                         result["registry_digest"])
        self.assertEqual(self._hooks_path().read_bytes(), self.hooks_after_install)
        self.assertFalse(self._external("confirmation-pending.json").exists())

    def test_wrong_digest_and_stale_preimage_rejected(self):
        self._install()
        code, output, _ = self._propose_add()
        digest = json.loads(output)["proposal_digest"]
        code, output, _ = self._confirm("0" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "PROPOSAL_CHANGED")
        self.assertFalse(self._registry_path().exists())
        self._registry_path().parent.mkdir(parents=True)
        self._registry_path().write_text(
            '{"schema_version":1,"rules":[]}', encoding="utf-8")
        original = self._registry_path().read_bytes()
        code, output, _ = self._confirm(digest)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_CHANGED")
        self.assertEqual(self._registry_path().read_bytes(), original)

    def test_competing_proposal_makes_old_digest_unconfirmable(self):
        self._install()
        code, first, _ = self._propose_add(reason="first")
        first_digest = json.loads(first)["proposal_digest"]
        code, second, _ = self._propose_add(reason="second")
        second_digest = json.loads(second)["proposal_digest"]
        self.assertNotEqual(first_digest, second_digest)
        code, output, _ = self._confirm(first_digest)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "PROPOSAL_CHANGED")
        self.assertFalse(self._registry_path().exists())
        code, output, error = self._confirm(second_digest)
        self.assertEqual(code, 0, error or output)

    def test_duplicate_and_unknown_fields(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, _ = self._propose_add(reason="second")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "RULE_DUPLICATE")
        with self.assertRaises(InstallationError) as raised:
            confirmation.propose_change(
                self.repo, self.installation_id, operation="resolve",
                rule_fields={"rule_id": "TEMP-001", "reason": "ok",
                             "extra": 1})
        self.assertEqual(raised.exception.code, "CONFIRMATION_FIELD")

    def test_legacy_unmanaged_proposal_is_a_conflict(self):
        self._install()
        metadata = self.repo / ".git" / "spellguard"
        metadata.mkdir(parents=True, exist_ok=True)
        (metadata / "proposal.json").write_text("{}", encoding="utf-8")
        code, output, _ = self._propose_add()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "LEGACY_PROPOSAL_CONFLICT")
        self.assertTrue((metadata / "proposal.json").exists())


class ResolveTest(_ManagedBase):
    def test_resolve_closes_only_the_named_rule(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, error = self._propose_resolve(reason="migration finished")
        self.assertEqual(code, 0, error or output)
        proposal = json.loads(output)
        self.assertEqual(proposal["operation"], "resolve")
        self.assertEqual(proposal["rule"]["id"], "TEMP-001")
        code, output, error = self._confirm(proposal["proposal_digest"])
        self.assertEqual(code, 0, error or output)
        registry = parse_registry(self._registry_path().read_bytes())
        self.assertEqual(registry.rules[0].lifecycle, "RESOLVED")
        self.assertEqual(registry.rules[0].resolution_reason,
                         "migration finished")

    def test_resolve_requires_a_registry_and_a_reason(self):
        self._install()
        code, output, _ = self._propose_resolve()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_MISSING")
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, _ = self._run([
            "propose", "--resolve", "TEMP-001", "--reason", "   ",
            "--installation-id", self.installation_id, "--format", "json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "CONFIRMATION_INVALID")


class FaultInjectionTest(_ManagedBase):
    def _propose(self):
        code, output, error = self._propose_add()
        self.assertEqual(code, 0, error or output)
        return json.loads(output)["proposal_digest"]

    def test_resume_at_pending_write_boundary(self):
        self._install()
        digest = self._propose()
        real = confirmation._write_pending
        state = {"calls": 0}

        def flaky(directory, pending):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(directory, pending)

        with mock.patch("spellguard.confirmation._write_pending",
                        side_effect=flaky):
            code, _output, _error = self._confirm(digest)
            self.assertEqual(code, 2)
        self.assertFalse(self._registry_path().exists())
        self.assertFalse(self._external("confirmation-pending.json").exists())
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 0, error or output)
        self.assertTrue(json.loads(output)["confirmed"])

    def test_resume_at_registry_write_boundary(self):
        self._install()
        digest = self._propose()
        real = confirmation._write_registry_file
        state = {"calls": 0}

        def flaky(root, schema):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(root, schema)

        with mock.patch("spellguard.confirmation._write_registry_file",
                        side_effect=flaky):
            code, _output, _error = self._confirm(digest)
            self.assertEqual(code, 2)
        self.assertFalse(self._registry_path().exists())
        self.assertTrue(self._external("confirmation-pending.json").exists())
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 0, error or output)
        self.assertTrue(json.loads(output)["confirmed"])

    def test_resume_at_confirmation_commit_boundary(self):
        self._install()
        digest = self._propose()
        real = confirmation._commit_confirmation
        state = {"calls": 0}

        def flaky(directory, pending):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(directory, pending)

        with mock.patch("spellguard.confirmation._commit_confirmation",
                        side_effect=flaky):
            code, _output, _error = self._confirm(digest)
            self.assertEqual(code, 2)
        self.assertTrue(self._registry_path().exists())
        self.assertTrue(self._external("confirmation-pending.json").exists())
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 0, error or output)
        self.assertTrue(json.loads(output)["confirmed"])
        self.assertFalse(self._external("confirmation-pending.json").exists())

    def test_cleanup_failure_reports_recovery_required_then_recovers(self):
        self._install()
        digest = self._propose()
        real = confirmation._unlink_control

        def deny(directory, name):
            if name == confirmation.PENDING_NAME:
                raise InstallationError("INSTALLATION_WRITE_FAILED", "synthetic")
            return real(directory, name)

        with mock.patch("spellguard.confirmation._unlink_control",
                        side_effect=deny):
            code, output, error = self._confirm(digest)
            self.assertEqual(code, 0, error or output)
            result = json.loads(output)
            self.assertTrue(result["confirmed"])
            self.assertTrue(result["recovery_required"])
        self.assertTrue(self._external("confirmation-pending.json").exists())
        code, output, error = self._recover()
        self.assertEqual(code, 0, error or output)
        self.assertTrue(json.loads(output)["recovered"])
        self.assertFalse(self._external("confirmation-pending.json").exists())

    def test_third_registry_content_is_not_overwritten(self):
        self._install()
        digest = self._propose()
        real = confirmation._write_registry_file
        state = {"calls": 0}

        def flaky(root, schema):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(root, schema)

        with mock.patch("spellguard.confirmation._write_registry_file",
                        side_effect=flaky):
            self.assertEqual(self._confirm(digest)[0], 2)
        third = {"schema_version": 1, "rules": []}
        self._registry_path().parent.mkdir(parents=True, exist_ok=True)
        self._registry_path().write_text(json.dumps(third), encoding="utf-8")
        original = self._registry_path().read_bytes()
        code, output, _ = self._confirm(digest)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "CONFIRMATION_CONFLICT")
        self.assertEqual(self._registry_path().read_bytes(), original)

    def test_recover_without_pending_creates_no_trust(self):
        self._install()
        code, output, error = self._recover()
        self.assertEqual(code, 0, error or output)
        report = json.loads(output)
        self.assertFalse(report["recovered"])
        self.assertFalse(report["recovery_required"])
        self.assertFalse(self._registry_path().exists())
        self.assertFalse(self._external("confirmation.json").exists())

    def test_handwritten_confirmation_cannot_reset_trust(self):
        self._install()
        self._write_external("confirmation.json", {
            "schema_version": 1,
            "installation_id": self.installation_id,
            "operation": "add",
            "proposal_digest": "a" * 64,
            "result_registry_digest": "b" * 64,
            "confirmed_at": "2026-01-01T00:00:00Z",
        })
        code, output, _ = self._confirm("a" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "CONFIRMATION_CONFLICT")
        self.assertFalse(self._registry_path().exists())

    def test_retry_completes_partial_commit_and_clears_pending(self):
        self._install()
        digest = self._propose()
        real = confirmation._write_control
        state = {"failed": False}

        def flaky(directory, name, payload, mode=0o600):
            if name == confirmation.INSTALLATION_NAME and not state["failed"]:
                state["failed"] = True
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(directory, name, payload, mode=mode)

        with mock.patch("spellguard.confirmation._write_control",
                        side_effect=flaky):
            code, _output, _error = self._confirm(digest)
            self.assertEqual(code, 2)
        self.assertTrue(self._external("confirmation-pending.json").exists())
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 0, error or output)
        result = json.loads(output)
        self.assertTrue(result["confirmed"])
        self.assertFalse(result["already_confirmed"])
        self.assertFalse(result["recovery_required"])
        self.assertFalse(self._external("confirmation-pending.json").exists())

    def test_status_reports_pending_confirmation(self):
        self._install()
        digest = self._propose()
        real = confirmation._write_registry_file
        state = {"calls": 0}

        def flaky(root, schema):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(root, schema)

        with mock.patch("spellguard.confirmation._write_registry_file",
                        side_effect=flaky):
            self.assertEqual(self._confirm(digest)[0], 2)
        code, output, _ = self._run(["status", "--format", "json"])
        self.assertTrue(json.loads(output)["confirmation_pending"])
        self.assertEqual(self._recover()[0], 0)
        code, output, _ = self._run(["status", "--format", "json"])
        self.assertFalse(json.loads(output)["confirmation_pending"])


class CheckHandlingTest(_ManagedBase):
    def test_check_failure_does_not_fail_confirmation(self):
        self._install()
        (self.repo / "src" / "demo" / "adapt.py").write_text(
            "def adapt():\n    return 0\n\n\ndef fresh():\n    return 1\n",
            encoding="utf-8")
        code, output, error = self._propose_add(symbol="fresh")
        self.assertEqual(code, 0, error or output)
        digest = json.loads(output)["proposal_digest"]
        code, output, error = self._confirm(digest)
        self.assertEqual(code, 0, error or output)
        result = json.loads(output)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["check_exit_code"], 2)
        self.assertFalse(result["check_complete"])
        self.assertTrue(result["diagnostics"])
        self.assertIn("SYMBOL_UNVERIFIED",
                      [d["code"] for d in result["diagnostics"]])

    def test_check_pass_is_reported(self):
        self._install()
        code, output, _ = self._propose_add()
        code, output, error = self._confirm(
            json.loads(output)["proposal_digest"])
        self.assertEqual(code, 0, error or output)
        result = json.loads(output)
        self.assertEqual(result["check_exit_code"], 0)
        self.assertTrue(result["check_complete"])


class ManagedHookTest(_ManagedBase):
    def _payload(self, event="UserPromptSubmit"):
        return {"hook_event_name": event, "cwd": str(self.repo),
                "session_id": "s", "turn_id": "t"}

    def test_absent_registry_stop_is_quiet(self):
        self._install()
        code, output, error = self._hook(self._payload("Stop"))
        self.assertEqual(code, 0, error or output)
        self.assertEqual(json.loads(output), {})

    def test_pending_confirmation_is_visible_and_not_adopted(self):
        self._install()
        code, output, _ = self._propose_add()
        digest = json.loads(output)["proposal_digest"]
        real = confirmation._write_registry_file
        state = {"calls": 0}

        def flaky(root, schema):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(root, schema)

        with mock.patch("spellguard.confirmation._write_registry_file",
                        side_effect=flaky):
            self.assertEqual(self._confirm(digest)[0], 2)
        code, output, error = self._hook(self._payload())
        self.assertEqual(code, 0, error or output)
        rendered = json.loads(output)
        self.assertIn("confirmation is not finished",
                      rendered["hookSpecificOutput"]["additionalContext"])
        # the pending registry was never adopted
        self.assertFalse(self._registry_path().exists())

    def test_committed_digest_injects_short_guidance(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(
            json.loads(output)["proposal_digest"])[0], 0)
        code, output, error = self._hook(self._payload())
        self.assertEqual(code, 0, error or output)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("installation-id", context)
        self.assertIn("Never confirm on your own", context)

    def test_guidance_injected_even_without_registry(self):
        self._install()
        code, output, error = self._hook(self._payload())
        self.assertEqual(code, 0, error or output)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("installation-id", context)

    def test_unreadable_registry_is_a_visible_failure(self):
        self._install()
        self._registry_path().parent.mkdir(exist_ok=True)
        self._registry_path().write_text("{ broken", encoding="utf-8")
        code, output, error = self._hook(self._payload("Stop"))
        self.assertEqual(code, 0, error or output)
        rendered = json.loads(output)
        self.assertIn("systemMessage", rendered)
        self.assertIn("unknown", rendered["systemMessage"].lower())
        code, output, error = self._hook(self._payload())
        self.assertEqual(code, 0, error or output)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("unknown", context.lower())

    def test_drifted_registry_is_a_visible_failure(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(
            json.loads(output)["proposal_digest"])[0], 0)
        # manual edit after confirmation is drift from the adopted digest
        self._registry_path().write_text(
            '{"schema_version":1,"rules":[]}', encoding="utf-8")
        code, output, error = self._hook(self._payload("Stop"))
        self.assertEqual(code, 0, error or output)
        self.assertIn("systemMessage", json.loads(output))

    def test_repeated_fault_stop_is_deduped_and_queryable(self):
        self._install()
        self._registry_path().parent.mkdir(parents=True, exist_ok=True)
        self._registry_path().write_text("{ broken", encoding="utf-8")
        first = json.loads(self._hook(self._payload("Stop"))[1])
        self.assertIn("systemMessage", first)
        second = json.loads(self._hook(self._payload("Stop"))[1])
        self.assertEqual(second, {})
        code, output, _ = self._run(["status", "--format", "json"])
        self.assertEqual(json.loads(output)["registry_state"], "unreadable")
        # fault clears, then recurs: both transitions are visible again
        self._registry_path().unlink()
        self.assertEqual(json.loads(self._hook(self._payload("Stop"))[1]), {})
        self._registry_path().write_text("{ broken", encoding="utf-8")
        again = json.loads(self._hook(self._payload("Stop"))[1])
        self.assertIn("systemMessage", again)

    def test_repeated_pending_stop_is_deduped(self):
        self._install()
        code, output, _ = self._propose_add()
        digest = json.loads(output)["proposal_digest"]
        real = confirmation._write_registry_file
        state = {"calls": 0}

        def flaky(root, schema):
            state["calls"] += 1
            if state["calls"] == 1:
                raise InstallationError("CONFIRMATION_WRITE_FAILED", "synthetic")
            return real(root, schema)

        with mock.patch("spellguard.confirmation._write_registry_file",
                        side_effect=flaky):
            self.assertEqual(self._confirm(digest)[0], 2)
        first = json.loads(self._hook(self._payload("Stop"))[1])
        self.assertIn("systemMessage", first)
        self.assertIn("confirmation is not finished", first["systemMessage"])
        second = json.loads(self._hook(self._payload("Stop"))[1])
        self.assertEqual(second, {})

    def test_committed_digest_is_consumed(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(
            json.loads(output)["proposal_digest"])[0], 0)
        code, output, error = self._hook(self._payload("Stop"))
        self.assertEqual(code, 0, error or output)
        json.loads(output)  # valid JSON either quiet or a notification
        state_dir = self._external("hook")
        self.assertTrue((state_dir / "state.json").exists())


class MultiRuleTest(_ManagedBase):
    """S-A04: a second TEMP registers without overwriting the first; resolve
    only changes the named lifecycle and closed rules still occupy a slot."""

    def _add_file(self, name, symbol):
        path = self.repo / "src" / "demo" / name
        path.write_text("def {}():\n    return 0\n".format(symbol),
                        encoding="utf-8")
        return "src/demo/{}".format(name)

    def _propose_named(self, path, symbol):
        return self._run([
            "propose", "--path", path, "--symbol", symbol,
            "--source-root", "src", "--reason", "second helper",
            "--desired-state", "remove later",
            "--installation-id", self.installation_id, "--format", "json"])

    def _rules(self):
        return json.loads(self._registry_path().read_text())["rules"]

    def _write_rules(self, rules):
        payload = json.dumps({"schema_version": 1, "rules": rules},
                             ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")).encode()
        self._registry_path().parent.mkdir(parents=True, exist_ok=True)
        self._registry_path().write_bytes(payload)
        return parse_registry(payload).digest

    def test_second_rule_registers_without_overwriting_first(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        first_rule = self._rules()[0]
        path = self._add_file("other.py", "helper")
        code, output, error = self._propose_named(path, "helper")
        self.assertEqual(code, 0, error or output)
        proposal = json.loads(output)
        self.assertEqual(proposal["rule"]["id"], "TEMP-002")
        code, output, error = self._confirm(proposal["proposal_digest"])
        self.assertEqual(code, 0, error or output)
        rules = self._rules()
        self.assertEqual([r["id"] for r in rules], ["TEMP-001", "TEMP-002"])
        self.assertEqual(rules[0], first_rule)
        self.assertEqual(rules[1]["lifecycle"], "ACTIVE")

    def test_duplicate_active_symbol_rejected(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, _ = self._propose_add(reason="same symbol again")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "RULE_DUPLICATE")

    def test_ninth_rule_rejected_and_closed_rule_counts(self):
        labels = ["a", "b", "c", "d", "e", "f", "g", "h"]
        rules = [{
            "id": "TEMP-{:03d}".format(i + 1),
            "classification": "temporary", "lifecycle": "ACTIVE",
            "reason": "x", "desired_state": "y",
            "protected_symbol": {"path": "src/demo/{}.py".format(labels[i]),
                                 "symbol": "sym_{}".format(i + 1),
                                 "source_root": "src"},
            "window": "no_external_callers", "resolution_reason": None,
        } for i in range(8)]
        self._write_rules(rules)
        self._install()
        code, output, _ = self._propose_named("src/demo/i.py", "sym_9")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_LIMIT")

    def test_id_follows_max_and_closed_rule_is_not_reused(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, _ = self._propose_resolve()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        self.assertEqual(self._rules()[0]["lifecycle"], "RESOLVED")
        path = self._add_file("third.py", "helper")
        code, output, error = self._propose_named(path, "helper")
        self.assertEqual(code, 0, error or output)
        self.assertEqual(json.loads(output)["rule"]["id"], "TEMP-002")
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        self.assertEqual([r["id"] for r in self._rules()],
                         ["TEMP-001", "TEMP-002"])
        self.assertEqual(self._rules()[0]["lifecycle"], "RESOLVED")

    def test_resolve_leaves_other_registration_bytes_untouched(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        path = self._add_file("other.py", "helper")
        code, output, _ = self._propose_named(path, "helper")
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        second = self._rules()[1]
        code, output, _ = self._propose_resolve(reason="migration finished")
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        rules = self._rules()
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["lifecycle"], "RESOLVED")
        self.assertEqual(rules[0]["resolution_reason"], "migration finished")
        self.assertEqual(rules[1], second)

    def test_new_proposal_rejects_drifted_registry(self):
        # install binds the trusted digest; a manual close afterwards is drift
        self._write_rules([{
            "id": "TEMP-001", "classification": "temporary",
            "lifecycle": "ACTIVE", "reason": "migration helper",
            "desired_state": "remove later",
            "protected_symbol": {"path": "src/demo/adapt.py",
                                 "symbol": "adapt", "source_root": "src"},
            "window": "no_external_callers", "resolution_reason": None}])
        code, output, error = self._run(["setup", "--format", "json"])
        self.assertEqual(code, 0, error or output)
        plan = json.loads(output)
        code, output, error = self._run(
            ["setup", "--apply", plan["plan_digest"], "--format", "json"])
        self.assertEqual(code, 0, error or output)
        self.installation_id = plan["installation_id"]
        rules = self._rules()
        rules[0]["lifecycle"] = "RESOLVED"
        rules[0]["resolution_reason"] = "closed outside the workflow"
        self._write_rules(rules)
        path = self._add_file("other.py", "helper")
        code, output, _ = self._propose_named(path, "helper")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["diagnostics"][0]["code"],
                         "REGISTRY_DRIFT")

    def test_resolve_summary_names_the_requested_rule(self):
        self._install()
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        path = self._add_file("other.py", "helper")
        code, output, _ = self._propose_named(path, "helper")
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, _ = self._propose_resolve("TEMP-001", reason="first done")
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        code, output, error = self._propose_resolve("TEMP-002", reason="second done")
        self.assertEqual(code, 0, error or output)
        proposal = json.loads(output)
        self.assertEqual(proposal["rule"]["id"], "TEMP-002")
        self.assertEqual(proposal["rule"]["resolution_reason"], "second done")

    def test_only_add_and_resolve_are_valid_operations(self):
        self._install()
        with self.assertRaises(InstallationError) as raised:
            confirmation.propose_change(
                self.repo, self.installation_id, operation="keep",
                rule_fields={"rule_id": "TEMP-001", "reason": "keep it"})
        self.assertEqual(raised.exception.code, "CONFIRMATION_OPERATION")
        code, output, _ = self._propose_add()
        self.assertEqual(self._confirm(json.loads(output)["proposal_digest"])[0], 0)
        # "not now" is not an operation and cannot close the rule
        self.assertEqual(self._rules()[0]["lifecycle"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
