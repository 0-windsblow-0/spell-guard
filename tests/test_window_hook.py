"""Hook adapter contract tests (R05).

Pure functions are loaded from spellguard.hook_core via importlib (the
legacy scripts entry is now a thin wrapper; D01);
state-machine cases run against the real functions with temp state dirs.
Notification key must exclude line numbers / snapshot hash / base_commit /
added_consumers; identical evidence never re-notifies, new consumers do.
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = None  # legacy path no longer owns the implementation (D01)

hook = __import__("spellguard.hook_core", fromlist=["hook_core"])

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


def digest() -> str:
    import hashlib
    return hashlib.sha256(json.dumps(
        FIXTURE, ensure_ascii=True, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def check_report(status="OPEN", consumers=(), diagnostics=(),
                 base_commit="abc", identity="snap-1", digest_value="D"):
    return {
        "schema_version": 1,
        "command": "check",
        "registry_digest": digest_value,
        "base_commit": base_commit,
        "snapshot_identity": identity,
        "analysis_scope": "python-direct-calls-v1",
        "complete": status != "UNVERIFIED" and not diagnostics,
        "results": [{
            "rule_id": "TEMP-001",
            "status": status,
            "complete": status != "UNVERIFIED" and not diagnostics,
            "protected_symbol": {"path": "src/demo/workaround.py",
                                 "symbol": "fallback", "source_root": "src"},
            "reason": "r", "desired_state": "d",
            "consumers": list(consumers),
            "added_consumers": None,
            "diagnostics": list(diagnostics),
        }],
        "diagnostics": list(diagnostics),
    }


class NotificationKeyTest(unittest.TestCase):
    def test_open_key_is_stable_across_line_moves(self):
        base = check_report("OPEN")
        moved = check_report("OPEN")
        moved["snapshot_identity"] = "different-snapshot"
        self.assertEqual(hook.notification_key(base), hook.notification_key(moved))

    def test_consumer_added_changes_key(self):
        one = check_report(
            "VIOLATED", [{"path": "src/demo/use.py", "symbol": "run",
                          "lines": [3]}])
        two = check_report(
            "VIOLATED", [
                {"path": "src/demo/use.py", "symbol": "run", "lines": [3]},
                {"path": "src/other.py", "symbol": "helper", "lines": [9]}])
        self.assertNotEqual(hook.notification_key(one), hook.notification_key(two))

    def test_same_consumer_new_line_does_not_change_key(self):
        one = check_report(
            "VIOLATED", [{"path": "src/demo/use.py", "symbol": "run",
                          "lines": [3]}])
        moved = check_report(
            "VIOLATED", [{"path": "src/demo/use.py", "symbol": "run",
                          "lines": [7]}])
        self.assertEqual(hook.notification_key(one), hook.notification_key(moved))

    def test_consumer_rename_changes_key(self):
        one = check_report("VIOLATED", [{"path": "p", "symbol": "one",
                                         "lines": [1]}])
        two = check_report("VIOLATED", [{"path": "p", "symbol": "two",
                                         "lines": [1]}])
        self.assertNotEqual(hook.notification_key(one),
                            hook.notification_key(two))

    def test_status_change_changes_key_and_diagnostics_count(self):
        first = hook.notification_key(check_report("VIOLATED", [], []))
        second = hook.notification_key(check_report(
            "UNVERIFIED", [], [{"code": "X", "message": "m", "path": "p"}]))
        self.assertNotEqual(first, second)

    def test_base_commit_missing_from_key(self):
        self.assertEqual(
            hook.notification_key(check_report("OPEN", base_commit="a")),
            hook.notification_key(check_report("OPEN", base_commit="b")))

    def test_key_is_deterministic_across_consumer_order(self):
        left = hook.notification_key(check_report("VIOLATED", [
            {"path": "a", "symbol": "s", "lines": [1]},
            {"path": "b", "symbol": "t", "lines": [1]}]))
        right = hook.notification_key(check_report("VIOLATED", [
            {"path": "b", "symbol": "t", "lines": [1]},
            {"path": "a", "symbol": "s", "lines": [1]}]))
        self.assertEqual(left, right)

    def test_diagnosis_path_counts_not_message(self):
        left = hook.notification_key(check_report(
            "UNVERIFIED", [], [{"code": "X", "message": "one", "path": "p"}]))
        right = hook.notification_key(check_report(
            "UNVERIFIED", [], [{"code": "X", "message": "two", "path": "p"}]))
        self.assertEqual(left, right)


class HookOutputTest(unittest.TestCase):
    def test_quiet_when_nothing_to_report(self):
        report = check_report("OPEN")
        self.assertEqual(hook.hook_output("Stop", report, notify=False), {})

    def test_first_violation_notifies_system_message(self):
        report = check_report("VIOLATED", [
            {"path": "src/demo/use.py", "symbol": "run", "lines": [3]}])
        output = hook.hook_output("Stop", report, notify=True)
        self.assertIn("systemMessage", output)
        self.assertNotIn("decision", output)
        self.assertNotIn("hookSpecificOutput", output)

    def test_user_prompt_submit_emits_context_line(self):
        report = {
            "schema_version": 1, "command": "context",
            "registry_digest": "D", "complete": True,
            "rules": [{"id": "TEMP-001", "reason": "r", "desired_state": "d",
                       "protected_symbol": {"path": "src/demo/workaround.py",
                                            "symbol": "fallback",
                                            "source_root": "src"},
                       "window": "no_external_callers"}],
            "diagnostics": []}
        output = hook.hook_output("UserPromptSubmit", report, notify=False,
                                  last_status="VIOLATED")
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TEMP-001", context)
        self.assertIn("上次", context)

    def test_user_prompt_submit_quiet_when_no_rules_and_no_history(self):
        report = {"schema_version": 1, "command": "context",
                  "registry_digest": "D", "complete": True, "rules": [],
                  "diagnostics": []}
        output = hook.hook_output("UserPromptSubmit", report, notify=False,
                                  last_status=None)
        self.assertEqual(output, {})

    def test_check_failure_message_is_visible_and_not_open(self):
        output = hook.hook_output(
            "Stop", None, notify=True,
            failure="check subprocess timed out; results are not available")
        self.assertIn("systemMessage", output)
        self.assertIn("not", output["systemMessage"].lower())


class StateMachineTest(unittest.TestCase):
    """OPEN → VIOLATED → repeat → line move → new consumer → OPEN → VIOLATED."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name).resolve() / "state"
        self.repo = Path(self.tempdir.name).resolve() / "repo"
        self.repo.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _record(self, report, failure=None):
        return hook.record_outcome(
            event="Stop", report=report, failure=failure,
            state_dir=self.state_dir, repo_root=self.repo,
            session_id="s", turn_id="t")

    def test_expected_notification_sequence(self):
        seq = [
            (check_report("OPEN"), 0, None),
            (check_report("VIOLATED", [
                {"path": "src/demo/use.py", "symbol": "run", "lines": [3]}]), 1, None),
            (check_report("VIOLATED", [
                {"path": "src/demo/use.py", "symbol": "run", "lines": [3]}]), 0, None),
            (check_report("VIOLATED", [
                {"path": "src/demo/use.py", "symbol": "run", "lines": [9]}]), 0, None),
            (check_report("VIOLATED", [
                {"path": "src/demo/use.py", "symbol": "run", "lines": [3]},
                {"path": "src/demo/other.py", "symbol": "call", "lines": [2]}]), 1, None),
            (check_report("OPEN"), 0, None),
            (check_report("VIOLATED", [
                {"path": "src/demo/use.py", "symbol": "run", "lines": [3]}]), 1, None),
        ]
        events = []
        for report, expected, failure in seq:
            outcome = self._record(report, failure)
            self.assertEqual(outcome["notify"], bool(expected),
                             "at step {}".format(events))
            self.assertEqual(outcome["notify"], bool(expected))
            events.append(outcome["key"])
        self.assertEqual(len(events), 7)
        state = hook.load_state(self.state_dir)
        self.assertEqual(state["last_status"], "VIOLATED")
        self.assertNotEqual(state["last_key"], "")

    def test_repeated_failure_is_deduplicated_then_renotified_after_recovery(self):
        first = self._record(None, failure="subprocess timed out after 10s")
        self.assertTrue(first["notify"])
        second = self._record(None, failure="subprocess timed out after 10s")
        self.assertFalse(second["notify"])
        ok = self._record(check_report("OPEN"))
        self.assertFalse(ok["notify"])
        third = self._record(None, failure="subprocess timed out after 10s")
        self.assertTrue(third["notify"])
        state = hook.load_state(self.state_dir)
        self.assertEqual(state["last_status"], "ERROR")

    def test_corrupt_state_file_is_not_silent_install(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "state.json").write_text("{ broken")
        with self.assertRaises(hook.HookFailure):
            self._record(check_report("OPEN"))

    def test_unwritable_state_dir_surfaces_failure(self):
        readonly = Path(self.tempdir.name).resolve() / "readonly"
        readonly.mkdir()
        os.chmod(readonly, 0o500)
        try:
            with self.assertRaises(hook.HookFailure):
                hook.record_outcome(
                    event="Stop", report=check_report("OPEN"), failure=None,
                    state_dir=readonly / "nested", repo_root=self.repo,
                    session_id="s", turn_id="t")
        finally:
            os.chmod(readonly, 0o700)

    def test_subdirectory_of_installed_repo_accepts_deeper_cwd(self):
        self.assertTrue(hook.same_repository(self.repo, self.repo / "src"))

    def test_nested_other_repository_rejected(self):
        nested = self.repo / "nested"
        nested.mkdir()
        subprocess.run(["git", "-C", os.fspath(nested), "init", "-q"], check=True)
        self.assertFalse(hook.same_repository(self.repo, nested))

    def test_lock_serializes_state_updates(self):
        import threading
        results = []
        errors = []
        def worker():
            try:
                results.append(self._record(check_report("VIOLATED", [
                    {"path": "p", "symbol": "run", "lines": [1]}])))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        notified = sum(1 for r in results if r["notify"])
        self.assertEqual(notified, 1)


class MainContractTest(unittest.TestCase):
    """main() via real subprocess using installed package interpreter."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name).resolve() / "repo"
        (self.repo / "src" / "demo").mkdir(parents=True)
        (self.repo / ".spellguard").mkdir()
        (self.repo / ".spellguard" / "rules.json").write_text(json.dumps(FIXTURE))
        (self.repo / "src" / "demo" / "__init__.py").write_text("")
        (self.repo / "src" / "demo" / "workaround.py").write_text(
            "def fallback():\n    return 0\n")
        for arguments in (["init", "-q"],
                          ["config", "user.email", "t@e.invalid"],
                          ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", os.fspath(self.repo)] + arguments,
                           check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        self.state_dir = Path(self.tempdir.name).resolve() / "state"
        self.digest_value = digest()

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        subprocess.run(["git", "-C", os.fspath(self.repo)] + list(arguments),
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _commit(self):
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def _payload(self, event):
        return json.dumps({"hook_event_name": event, "cwd": os.fspath(self.repo),
                           "session_id": "s", "turn_id": "t"})

    def _invoke(self, event, timeout=30):
        command = [sys.executable, "-m", "spellguard", "hook", "--host", "codex",
                   "--repo", os.fspath(self.repo),
                   "--registry-sha256", self.digest_value,
                   "--state-dir", os.fspath(self.state_dir)]
        result = subprocess.run(
            command, input=self._payload(event), cwd="/tmp",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout, env={**os.environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[1] / "src")})
        return result

    def test_open_turn_is_quiet_and_exit_zero(self):
        self._commit()
        result = self._invoke("Stop")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_violation_notifies_once_then_stays_quiet(self):
        self._commit()
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\ndef run():\n    return f()\n")
        first = self._invoke("Stop")
        self.assertEqual(first.returncode, 0)
        message = json.loads(first.stdout)["systemMessage"]
        self.assertIn("TEMP-001", message)
        second = self._invoke("Stop")
        self.assertEqual(json.loads(second.stdout), {})

    def test_f06_syntax_unknown_failure_is_visible_on_first_stop(self):
        """F06: a use.py syntax error makes check exit 2/complete=false; the
        first Stop must show a visible systemMessage, not silent {}."""
        self._commit()
        (self.repo / "src" / "demo" / "use.py").write_text("def broken(:\n")
        result = self._invoke("Stop")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertIn("systemMessage", output)
        self.assertNotEqual(output["systemMessage"], "{}")

    def test_f06_missing_registry_failure_is_visible_on_first_stop(self):
        (self.repo / ".spellguard" / "rules.json").unlink()
        self._commit()
        result = self._invoke("Stop")
        self.assertEqual(result.returncode, 0)
        self.assertIn("systemMessage", json.loads(result.stdout))

    def test_f06_repeated_failure_is_deduplicated_via_real_cli(self):
        (self.repo / ".spellguard" / "rules.json").unlink()
        self._commit()
        first = json.loads(self._invoke("Stop").stdout)
        second = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", first)
        self.assertEqual(second, {})

    def test_f07_same_violation_survives_user_prompt_submit(self):
        """F07: Stop(VIOLATED) → UserPromptSubmit → Stop must NOT re-notify the
        same unchanged violation; the prompt event must not erase last state."""
        self._commit()
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\ndef run():\n    return f()\n")
        first = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", first)
        prompt = json.loads(self._invoke("UserPromptSubmit").stdout)
        # prompt may carry context but must not disturb the check state
        prompt2 = json.loads(self._invoke("UserPromptSubmit").stdout)
        second = json.loads(self._invoke("Stop").stdout)
        self.assertEqual(second, {}, "same violation must stay deduplicated")

    def test_f07_recovered_open_then_new_violation_notifies_again(self):
        self._commit()
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\ndef run():\n    return f()\n")
        self.assertIn("systemMessage", json.loads(self._invoke("Stop").stdout))
        (self.repo / "src" / "demo" / "use.py").write_text(
            "value = 1\n")
        result = json.loads(self._invoke("Stop").stdout)
        self.assertEqual(result, {})
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\ndef run():\n    return f()\n")
        again = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", again)

    def test_f10_state_dir_inside_repo_is_rejected(self):
        """F10: a state-dir inside the installed repository must be refused,
        not silently created."""
        inside = self.repo / ".spellguard-state"
        with self.assertRaises(hook.HookFailure):
            hook.record_outcome(
                event="Stop", report=check_report("OPEN"), failure=None,
                state_dir=inside, repo_root=self.repo,
                session_id="s", turn_id="t")
        self.assertFalse(inside.exists())

    def test_f10_symlinked_events_log_is_rejected(self):
        """F10: events.jsonl must not follow a symlink to another file."""
        from spellguard.cli import main as _main
        self._commit()
        sentinel = Path(self.tempdir.name).resolve() / "sentinel.log"
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\n"
            "def run():\n    return f()\n")
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "events.jsonl").symlink_to(sentinel)
        result = self._invoke("Stop")
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertIn("systemMessage", output)
        self.assertFalse(sentinel.exists())

    def test_f11_violation_with_failure_shows_concrete_evidence(self):
        """F11: a definite violation plus a syntax error elsewhere must show
        rule id and call location, not a generic 'kept' note."""
        self._commit()
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\n"
            "def run():\n    return f()\n")
        (self.repo / "src" / "demo" / "bad.py").write_text("def broken(:\n")
        result = self._invoke("Stop")
        message = json.loads(result.stdout).get("systemMessage", "")
        self.assertIn("TEMP-001", message)
        self.assertIn("use.py", message)
        self.assertNotEqual(message, "")

    def test_f12_new_failure_path_is_renotified(self):
        """F12: diagnostics (code/path) belong to the notification identity —
        a syntax error moving from a.py to b.py is a NEW failure."""
        self._commit()
        (self.repo / "a.py").write_text("def broken(:\n")
        first = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", first)
        (self.repo / "a.py").unlink()
        (self.repo / "b.py").write_text("def broken(:\n")
        second = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", second)
        # unchanged evidence stays deduplicated
        third = json.loads(self._invoke("Stop").stdout)
        self.assertEqual(third, {})

    def test_f13_array_input_is_visible_protocol_failure(self):
        """F13: stdin as a valid JSON array must produce a visible protocol
        failure, host exit 0, JSON output — no traceback/crash."""
        command = [sys.executable, "-m", "spellguard", "hook", "--host", "codex",
                   "--repo", os.fspath(self.repo),
                   "--registry-sha256", self.digest_value,
                   "--state-dir", os.fspath(self.state_dir)]
        result = subprocess.run(command, input='[]', cwd="/tmp",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=20,
                                env={**os.environ, "PYTHONPATH": str(
                                    Path(__file__).resolve().parents[1] / "src")})
        self.assertEqual(result.returncode, 0)
        self.assertIn("systemMessage", json.loads(result.stdout))
        self.assertNotIn("Traceback", result.stderr)

    def test_committing_violation_still_notifies_quiet_after_key_match_real(self):
        """Real regression for committed violation persistence (replaces the
        old empty pass placeholder): commit the caller, violation stays
        notified only when key changes."""
        self._commit()
        (self.repo / "src" / "demo" / "use.py").write_text(
            "from demo.workaround import fallback as f\n"
            "def run():\n    return f()\n")
        self._commit()
        first = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", first)
        second = json.loads(self._invoke("Stop").stdout)
        self.assertEqual(second, {})

    def test_frozen_storage_links_rejected_for_both_events(self):
        self._commit()
        original = self.state_dir
        for event in ("Stop", "UserPromptSubmit"):
            for name in ("directory", "state.json", "lock", "events.jsonl"):
                with self.subTest(event=event, name=name):
                    case = original.parent / (event + name.replace(".", "-"))
                    case.mkdir()
                    target = case / "target"
                    if name == "directory":
                        target.mkdir()
                        state = case / "state"
                        state.symlink_to(target, target_is_directory=True)
                    else:
                        target.write_text("sentinel")
                        state = case / "state"
                        state.mkdir()
                        (state / name).symlink_to(target)
                    self.state_dir = state
                    output = self._invoke(event)
                    self.assertEqual(output.returncode, 0)
                    self.assertIn("systemMessage", json.loads(output.stdout))
                    if name == "directory":
                        self.assertEqual(list(target.iterdir()), [])
                    else:
                        self.assertEqual(target.read_text(), "sentinel")
        self.state_dir = original

    def test_frozen_log_failure_recovers_without_losing_violation(self):
        self._commit()
        (self.repo / "src/demo/use.py").write_text(
            "from demo.workaround import fallback\ndef run():\n    return fallback()\n")
        self.state_dir.mkdir()
        log = self.state_dir / "events.jsonl"
        log.mkdir()
        failed = self._invoke("Stop")
        self.assertEqual(failed.returncode, 0)
        self.assertIn("systemMessage", json.loads(failed.stdout))
        self.assertFalse((self.state_dir / "state.json").exists())
        log.rmdir()
        recovered = json.loads(self._invoke("Stop").stdout)
        self.assertIn("TEMP-001", recovered["systemMessage"])
        self.assertEqual(json.loads(self._invoke("Stop").stdout), {})

    def test_frozen_mixed_failure_keeps_reason_and_changes_key(self):
        self._commit()
        (self.repo / "src/demo/use.py").write_text(
            "from demo.workaround import fallback\ndef run():\n    return fallback()\n")
        for name in ("a.py", "b.py"):
            path = self.repo / name
            path.write_bytes(b"\xff")
            message = json.loads(self._invoke("Stop").stdout)["systemMessage"]
            self.assertIn("TEMP-001", message)
            self.assertIn("SOURCE_READ_FAILED", message)
            self.assertIn(name, message)
            self.assertEqual(json.loads(self._invoke("Stop").stdout), {})
            path.unlink()

    def test_frozen_invalid_fields_and_lock_are_visible_failures(self):
        from unittest.mock import patch
        self._commit()
        for field, value in (("cwd", 42), ("session_id", {}), ("turn_id", [])):
            with self.subTest(field=field):
                payload = json.loads(self._payload("Stop"))
                payload[field] = value
                with patch.object(self, "_payload", return_value=json.dumps(payload)):
                    output = self._invoke("Stop")
                self.assertEqual(output.returncode, 0)
                self.assertIn("systemMessage", json.loads(output.stdout))
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "lock").mkdir()
        output = self._invoke("Stop")
        self.assertEqual(output.returncode, 0)
        self.assertIn("systemMessage", json.loads(output.stdout))

    def test_frozen_event_log_matches_output_and_preserves_check_state(self):
        self._commit()
        for event in ("Stop", "UserPromptSubmit", "Stop"):
            before = ((self.state_dir / "state.json").read_bytes()
                      if (self.state_dir / "state.json").exists() else None)
            result = self._invoke(event)
            output = json.loads(result.stdout)
            logged = json.loads((self.state_dir / "events.jsonl").read_text().splitlines()[-1])
            self.assertEqual(logged["emitted"], bool(output))
            self.assertEqual(logged["exit_code"], 0)
            self.assertGreaterEqual(logged["elapsed_ms"], 0)
            self.assertEqual(logged["diagnostic_codes"], [])
            if event == "UserPromptSubmit":
                self.assertEqual((self.state_dir / "state.json").read_bytes(), before)

    def test_unsupported_event_is_reported_not_crash(self):
        self._commit()
        result = self._invoke("SessionEnd")
        self.assertEqual(result.returncode, 0)
        self.assertIn("systemMessage", json.loads(result.stdout))


class MainContractGoReplayTest(unittest.TestCase):
    """G04: Go rule replayed through the real CLI + existing adapter
    subprocess; protocol/engine integration only, no live host claim."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name) / "repo"
        (self.repo / "backend" / "legacy").mkdir(parents=True)
        (self.repo / ".spellguard").mkdir()
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
        self.digest_value = hashlib.sha256(json.dumps(
            self.go_fixture, ensure_ascii=True, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        self._write("backend/go.mod", "module example.test/demo\n")
        self._write("backend/legacy/adapt.go",
                    "package legacy\n\nfunc Adapt() int { return 0 }\n")
        self._write(".spellguard/rules.json",
                    json.dumps(self.go_fixture))
        for arguments in (["init", "-q"],
                          ["config", "user.email", "t@e"],
                          ["config", "user.name", "t"]):
            self._git(*arguments)
        self.state_dir = Path(self.tempdir.name) / "state"

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        subprocess.run(["git", "-C", os.fspath(self.repo)] + list(arguments),
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

    def _commit(self):
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def _payload(self, event):
        return json.dumps({"hook_event_name": event,
                           "cwd": os.fspath(self.repo),
                           "session_id": "s", "turn_id": "t"})

    def _invoke(self, event, timeout=30):
        command = [sys.executable, "-m", "spellguard", "hook",
                   "--host", "codex",
                   "--repo", os.fspath(self.repo),
                   "--registry-sha256", self.digest_value,
                   "--state-dir", os.fspath(self.state_dir)]
        return subprocess.run(
            command, input=self._payload(event), cwd="/tmp",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONPATH": str(
                Path(__file__).resolve().parents[1] / "src")})

    def _write(self, relative_path: str, content: str):
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_go_rule_replays_without_host_branch(self):
        # isolated → caller → dedup → removal
        self._commit()
        result = self._invoke("Stop")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

        self._write("backend/cmd/run.go",
                    'package main\n\nimport "example.test/demo/legacy"\n\n'
                    "func main() { legacy.Adapt() }\n")
        first = json.loads(self._invoke("Stop").stdout)
        self.assertIn("systemMessage", first)
        self.assertIn("TEMP-001", first["systemMessage"])

        second = json.loads(self._invoke("Stop").stdout)
        self.assertEqual(second, {})

        (self.repo / "backend" / "cmd" / "run.go").unlink()
        third = json.loads(self._invoke("Stop").stdout)
        self.assertEqual(third, {})


if __name__ == "__main__":
    unittest.main()
