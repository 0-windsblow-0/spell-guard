"""Claude Code and Cursor thin adapter tests (protocol mapping layer).

Both adapters reuse the codex_repair_window core; the Codex suite
(test_window_hook) already covers the shared state machine end-to-end. These
tests verify per-host event normalization and output vocabulary only.
The adapters are code-verified; live host replay is a separate acceptance
step and is claimed nowhere here.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# D01: implementations moved into the package; the legacy thin scripts import
# from it. Tests import package modules directly.
import spellguard.claude_hook as CLAUDE_MODULE  # noqa: E402
import spellguard.cursor_hook as CURSOR_MODULE  # noqa: E402

FIXTURE = {
    "schema_version": 1,
    "rules": [{
        "id": "TEMP-001",
        "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": "Synthetic fixture: compatibility helper kept during migration.",
        "desired_state": "Synthetic fixture: remove helper after migration.",
        "protected_symbol": {"path": "src/demo/workaround.py",
                             "symbol": "fallback", "source_root": "src"},
        "window": "no_external_callers",
        "resolution_reason": None,
    }],
}

DIGEST = None
DEFINITION = "def fallback():\n    return 0\n"
CALLER = ("from demo.workaround import fallback as f\n"
          "def run():\n    return f()\n")


def calc_digest():
    import hashlib
    return hashlib.sha256(json.dumps(
        FIXTURE, ensure_ascii=True, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def set_globals():
    global DIGEST
    DIGEST = calc_digest()


set_globals()


def _spec_and_load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdapterContractTest(unittest.TestCase):
    """In-process contract tests for the thin layers."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name) / "repo"
        (self.repo / "src" / "demo").mkdir(parents=True)
        (self.repo / ".spellguard").mkdir()
        (self.repo / ".spellguard" / "rules.json").write_text(json.dumps(FIXTURE))
        (self.repo / "src" / "demo" / "__init__.py").write_text("")
        (self.repo / "src" / "demo" / "workaround.py").write_text(DEFINITION)
        self.state_dir = Path(self.tempdir.name) / "state"
        for args in (["init", "-q"], ["config", "user.email", "t@e"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", os.fspath(self.repo)] + args,
                           check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        self._commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def _commit(self):
        subprocess.run(["git", "-C", os.fspath(self.repo), "add", "--all"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "-C", os.fspath(self.repo), "commit", "-qm", "f"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _claude(self):
        return CLAUDE_MODULE

    def _cursor(self):
        return CURSOR_MODULE

    def test_claude_normalizes_events_and_keeps_naming(self):
        adapter = self._claude()
        self.assertEqual(
            adapter.normalize_event({"hook_event_name": "Stop"}), "Stop")
        self.assertEqual(
            adapter.normalize_event({"hook_event_name": "UserPromptSubmit"}),
            "UserPromptSubmit")
        with self.assertRaises(adapter.HookFailure):
            adapter.normalize_event({"hook_event_name": "SessionEnd"})

    def test_cursor_uses_cascade_names_and_rejects_agent_names(self):
        adapter = self._cursor()
        self.assertEqual(
            adapter.normalize_event({"hook_event_name": "stop"}), "Stop")
        self.assertEqual(
            adapter.normalize_event({"hook_event_name": "beforeSubmitPrompt"}),
            "UserPromptSubmit")
        with self.assertRaises(adapter.HookFailure):
            # Cursor also has postToolUse and SessionStart, unsupported here
            adapter.normalize_event({"hook_event_name": "postToolUse"})
        with self.assertRaises(adapter.HookFailure):
            # camelCase 'Stop' is NOT accepted: Cursor uses lowercase stop
            adapter.normalize_event({"hook_event_name": "Stop"})

    def test_cursor_output_uses_snake_case_vocabulary(self):
        adapter = self._cursor()
        context_payload = adapter.ceil_output(
            "beforeSubmitPrompt",
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                    "additionalContext": "hello"}})
        self.assertEqual(context_payload,
                         {"continue": True, "user_message": "hello"})
        stop_payload = adapter.ceil_output(
            "stop", {"systemMessage": "TEMP-001 alert"})
        self.assertEqual(stop_payload, {"followup_message": "TEMP-001 alert"})
        quiet = adapter.ceil_output("Stop", {})
        self.assertEqual(quiet, {})

    def test_claude_output_patterns_match_codex_contract(self):
        adapter = self._claude()
        prompt_output = adapter.run_adapter(
            os.fspath(self.repo), DIGEST, os.fspath(self.state_dir),
            json.dumps({"hook_event_name": "UserPromptSubmit",
                        "cwd": os.fspath(self.repo), "session_id": "s",
                        "turn_id": ""}))
        self.assertIn("hookSpecificOutput", prompt_output)
        self.assertEqual(
            prompt_output["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit")
        # Claude passes transcript_path/permission_mode; ignore those extras.
        unknown_extra = adapter.run_adapter(
            os.fspath(self.repo), DIGEST, os.fspath(self.state_dir),
            json.dumps({"hook_event_name": "Stop",
                        "cwd": os.fspath(self.repo),
                        "session_id": "s", "turn_id": "t",
                        "transcript_path": "/x/y.jsonl",
                        "permission_mode": "default"}))
        # OPEN + quiet => {} is fine
        self.assertEqual(unknown_extra, {})

    def test_cursor_violation_message_through_followup_message(self):
        (self.repo / "src" / "demo" / "use.py").write_text(CALLER)
        adapter = self._cursor()
        result = adapter.run_adapter(
            os.fspath(self.repo), DIGEST, os.fspath(self.state_dir),
            json.dumps({"hook_event_name": "stop",
                        "cwd": os.fspath(self.repo), "session_id": "s",
                        "turn_id": ""}))
        self.assertIn("followup_message", result)
        self.assertIn("TEMP-001", result["followup_message"])

    def test_cursor_repeated_violation_is_deduplicated(self):
        (self.repo / "src" / "demo" / "use.py").write_text(CALLER)
        adapter = self._cursor()
        payload = json.dumps({"hook_event_name": "stop",
                              "cwd": os.fspath(self.repo), "session_id": "s",
                              "turn_id": ""})
        first = adapter.run_adapter(os.fspath(self.repo), DIGEST,
                                    os.fspath(self.state_dir), payload)
        self.assertIn("followup_message", first)
        second = adapter.run_adapter(os.fspath(self.repo), DIGEST,
                                     os.fspath(self.state_dir), payload)
        self.assertEqual(second, {})

    def test_claude_violation_uses_systemmessage(self):
        (self.repo / "src" / "demo" / "use.py").write_text(CALLER)
        adapter = self._claude()
        result = adapter.run_adapter(
            os.fspath(self.repo), DIGEST, os.fspath(self.state_dir),
            json.dumps({"hook_event_name": "Stop",
                        "cwd": os.fspath(self.repo), "session_id": "s",
                        "turn_id": ""}))
        self.assertIn("systemMessage", result)
        self.assertIn("TEMP-001", result["systemMessage"])


def _spec_and_load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, path if isinstance(path, str) else str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdapterSubprocessContractTest(unittest.TestCase):
    """Real subprocess round-trips with the absolute interpreter monkey."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name) / "repo"
        (self.repo / "src" / "demo").mkdir(parents=True)
        (self.repo / ".spellguard").mkdir()
        (self.repo / ".spellguard" / "rules.json").write_text(json.dumps(FIXTURE))
        (self.repo / "src" / "demo" / "__init__.py").write_text("")
        (self.repo / "src" / "demo" / "workaround.py").write_text(DEFINITION)
        self.state_dir = Path(self.tempdir.name) / "state"
        for args in (["init", "-q"], ["config", "user.email", "t@e"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", os.fspath(self.repo)] + args,
                           check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        self._commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def _commit(self):
        subprocess.run(["git", "-C", os.fspath(self.repo), "add", "--all"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "-C", os.fspath(self.repo), "commit", "-qm", "f"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _invoke(self, script_name, payload):
        command = [sys.executable, "-m", "spellguard", "hook",
                   "--host", {"claude_repair_window.py": "claude",
                              "cursor_repair_window.py": "cursor"}[script_name],
                   "--repo", os.fspath(self.repo),
                   "--registry-sha256", DIGEST,
                   "--state-dir", os.fspath(self.state_dir)]
        return subprocess.run(command, input=json.dumps(payload), cwd="/tmp",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=30,
                              env={**os.environ, "PYTHONPATH": str(
                                  Path(__file__).resolve().parents[1] / "src")})

    def test_claude_subprocess_open_is_quiet(self):
        result = self._invoke("claude_repair_window.py",
                              {"hook_event_name": "Stop",
                               "cwd": os.fspath(self.repo),
                               "session_id": "s", "turn_id": ""})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_cursor_subprocess_protocol_shapes_survive_real_cli(self):
        result = self._invoke("cursor_repair_window.py",
                              {"hook_event_name": "beforeSubmitPrompt",
                               "cwd": os.fspath(self.repo),
                               "session_id": "s", "turn_id": ""})
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("user_message", payload)


if __name__ == "__main__":
    unittest.main()
