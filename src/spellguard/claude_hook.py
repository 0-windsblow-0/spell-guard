#!/usr/bin/env python3
"""Claude Code adapter for the Spellguard Repair Window.

Claude Code hook events are homologous to the Codex ones the R05 adapter
verifies: `UserPromptSubmit` and `Stop` use the same names, and the shared
output vocabulary (systemMessage, hookSpecificOutput.additionalContext) is
accepted. Input differences from Codex v1: Claude Code adds transcript_path /
permission_mode and has no turn_id; the adapter treats turn_id as an optional
empty string and never reads prompt or transcript contents.
"""

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


def normalize_event(payload: Dict) -> str:
    event = payload.get("hook_event_name")
    if event not in REQUIRED_EVENTS:
        raise HookFailure("unsupported host event: {}".format(event))
    return event


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--registry-sha256", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    output = run_adapter(args.repo, args.registry_sha256, args.state_dir,
                         sys.stdin.read())
    sys.stdout.write(json.dumps(output))
    return 0


def run_adapter(repo_argument: str, digest_value: str, state_argument: str,
                payload_text: str) -> Dict:
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
            output = outcome["output"]
        else:
            with _locked_state(state_dir, repo) as directory:
                state = _load_state(directory)
                output = hook_output(event, report, False,
                                     last_status=state["last_status"], failure=failure)
                _append_event(directory, _event(event, **details, key=None,
                              output=output, report=report, exit_code=code, elapsed_ms=elapsed))
    except (HookFailure, OSError, ValueError, TypeError, KeyError) as error:
        output = _failure_output(report, error)
    return output


if __name__ == "__main__":
    raise SystemExit(main())
