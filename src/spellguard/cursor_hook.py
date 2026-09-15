#!/usr/bin/env python3
"""Cursor adapter for the Spellguard Repair Window.

Cursor hook events differ in names and casing: the prompt hook is
`beforeSubmitPrompt` and the turn end hook is lowercase `stop`; hook
configuration lives in `.cursor/hooks.json`. Cursor supports
`user_message`/`agent_message`/`followup_message` outputs (snake_case in
v2.0+). The shared Spellguard core is reused; only the normalization and the
output vocabulary differ.

Known limitation (verified against Cursor's own docs/bug history): several
Cursor releases have broken agent_message/user_message delivery; on this
adapter the response of `beforeSubmitPrompt` is `user_message` only, and the
`stop` handler prefers `followup_message` so the violation questions reach
the user even when message fields have known regressions in specific
Cursor versions. Hook installation is manual (.cursor/hooks.json).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

from .hook_core import (  # noqa: F401  (re-exported for legacy scripts)
    HookFailure,
    _failure_output,
    _load_state,
    _locked_state,
    _run_cli,
    _append_event,
    _event,
    hook_output,
    record_outcome,
    same_repository,
)

REQUIRED_EVENTS = {"Stop", "UserPromptSubmit"}
CURSOR_TO_NORMALIZED = {
    "beforeSubmitPrompt": "UserPromptSubmit",
    "stop": "Stop",
}


def normalize_event(payload: Dict) -> str:
    event = CURSOR_TO_NORMALIZED.get(payload.get("hook_event_name"))
    if event not in REQUIRED_EVENTS:
        raise HookFailure(
            "unsupported host event: {} (expected beforeSubmitPrompt or stop)".format(
                payload.get("hook_event_name")))
    return event


def ceil_output(cursor_event: str, output: Dict) -> Dict:
    """Adapt the shared output to Cursor's snake_case vocabulary."""
    if cursor_event == "beforeSubmitPrompt" and output.get("hookSpecificOutput"):
        return {"continue": True,
                "user_message": output["hookSpecificOutput"]["additionalContext"]}
    if cursor_event == "stop":
        message = output.get("systemMessage")
        if message:
            return {"followup_message": message}
        return {}
    return output


def run_adapter(repo_argument: str, digest_value: str, state_argument: str,
                payload_text: str) -> Dict:
    carry = {"event": None}
    report = None
    try:
        repo = Path(repo_argument)
        state_dir = Path(state_argument)
        if not repo.is_absolute() or not state_dir.is_absolute():
            raise HookFailure("repo and state-dir must be absolute paths")
        payload = json.loads(payload_text) if isinstance(
            payload_text, str) else payload_text
        if not isinstance(payload, dict):
            raise HookFailure("host event payload must be a JSON object")
        for field in ("hook_event_name", "cwd"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise HookFailure("{} must be a non-empty string".format(field))
        for field in ("session_id", "turn_id"):
            if not isinstance(payload.get(field, ""), str):
                raise HookFailure("{} must be a string".format(field))
        event = normalize_event(payload)
        cursor_event = payload["hook_event_name"]
        carry["event"] = cursor_event
        if not same_repository(repo, Path(payload["cwd"])):
            raise HookFailure("event cwd is outside the installed repository")
        with _locked_state(state_dir, repo) as directory:
            _load_state(directory)
        report, failure, code, elapsed = _run_cli(
            repo, digest_value, "check" if event == "Stop" else "context")
        details = dict(session_id=payload.get("session_id", ""),
                       turn_id=payload.get("turn_id", ""))
        if event == "Stop":
            outcome = record_outcome(event, report, failure, state_dir, repo,
                                     **details, digest_value=digest_value,
                                     exit_code=code, elapsed_ms=elapsed)
            normalized = outcome["output"]
        else:
            with _locked_state(state_dir, repo) as directory:
                state = _load_state(directory)
                normalized = hook_output(event, report, False,
                                         last_status=state["last_status"], failure=failure)
                _append_event(directory, _event(event, **details, key=None,
                              output=normalized, report=report, exit_code=code, elapsed_ms=elapsed))
        return ceil_output(cursor_event, normalized)
    except (HookFailure, OSError, ValueError, TypeError, KeyError) as error:
        carry_fail = _failure_output(report, error)
        message = carry_fail["systemMessage"]
        if carry.get("event") == "beforeSubmitPrompt":
            return {"continue": False, "user_message": message}
        return {"followup_message": message}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--registry-sha256", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    output = run_adapter(args.repo, args.registry_sha256, args.state_dir,
                         sys.stdin.read())
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
