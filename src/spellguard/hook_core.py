"""Host-agnostic Repair Window hook core: prompt context, turn-end check.

Shared by the three single-host thin adapters. Contract source:
docs/ARCHITECTURE.md §6 and the D01 distribution plan. stdin JSON only;
no prompt/transcript reading; refuse links and traversal; exit 0."""


import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CHECK_TIMEOUT_SECONDS = 10
LOCK_WAIT_SECONDS = 1.0
STATE_SCHEMA = 2
LEGACY_STATE_SCHEMA = 1
STATUSES = {"OPEN", "VIOLATED", "UNVERIFIED", "RESOLVED", "ERROR"}
NOTIFIABLE_STATUSES = {"VIOLATED", "UNVERIFIED"}


class HookFailure(Exception):
    """A visible adapter failure, never an implicit successful check."""


def _diagnostic_keys(report):
    report = report or {}
    diagnostics = list(report.get("diagnostics", []))
    for result in report.get("results", []):
        diagnostics.extend(result.get("diagnostics", []))
    return sorted({(d["code"], d.get("path")) for d in diagnostics},
                  key=lambda d: (d[0], d[1] or ""))


def notification_key(report: Optional[Dict], failure: Optional[str] = None,
                     digest_value: Optional[str] = None) -> str:
    report = report or {}
    results = report.get("results", [])
    payload = {
        "digest": digest_value or report.get("registry_digest"),
        "statuses": sorted((r["rule_id"], r["status"]) for r in results),
        "consumers": sorted({(r["rule_id"], c["path"], c["symbol"])
                             for r in results for c in r.get("consumers", [])}),
        "diagnostics": _diagnostic_keys(report),
        "complete": report.get("complete", False),
        # Report errors use stable diagnostic evidence, never wording/line numbers.
        "failure": failure if not report else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _rule_identity(result: Dict) -> Dict:
    symbol = result.get("protected_symbol") or {}
    return {
        "rule_id": result.get("rule_id"),
        "protected_symbol": {
            "path": symbol.get("path"),
            "symbol": symbol.get("symbol"),
            "source_root": symbol.get("source_root"),
        },
        "reason": result.get("reason"),
        "desired_state": result.get("desired_state"),
    }


def _item_key(payload: Dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def notification_items(report: Optional[Dict], failure: Optional[str] = None,
                       digest_value: Optional[str] = None,
                       fault_message: Optional[str] = None) -> List[Dict]:
    """Per-rule notification identity, ignoring lines, time, HEAD and the
    whole-registry digest so unrelated rules never re-notify an unchanged one."""
    items: List[Dict] = []
    if fault_message is not None:
        # Installation/registry faults dedupe on their own stable message.
        items.append({
            "kind": "managed",
            "key": _item_key({"kind": "managed", "message": fault_message}),
            "message": fault_message,
        })
    elif failure is not None:
        items.append({
            "kind": "failure",
            "key": _item_key({"kind": "failure", "failure": failure}),
            "failure": failure,
        })
    for result in (report or {}).get("results", []):
        status = result.get("status")
        if status not in NOTIFIABLE_STATUSES:
            continue
        if status == "VIOLATED" and not result.get("consumers"):
            continue
        payload = {
            "kind": "rule",
            "status": status,
            "rule": _rule_identity(result),
            "consumers": sorted({(c.get("path"), c.get("symbol"))
                                 for c in result.get("consumers", [])}),
            "diagnostics": sorted({(d.get("code"), d.get("path"))
                                   for d in result.get("diagnostics", [])}),
            "complete": bool(result.get("complete")),
        }
        items.append({
            "kind": "violation" if status == "VIOLATED" else "unknown",
            "key": _item_key(payload),
            "status": status,
            "result": result,
        })
    return items


def _module_name(symbol):
    path = Path(symbol["path"])
    if symbol["source_root"] != ".":
        path = path.relative_to(symbol["source_root"])
    return path.with_suffix("").as_posix().replace("/", ".")


def _rule_context_line(rule: Dict) -> str:
    symbol = rule["protected_symbol"]
    try:
        module_path = _module_name(symbol)
    except ValueError:
        module_path = symbol["path"]
    return "{}.{}".format(module_path, symbol["symbol"])


def hook_output(event_name: str, report: Optional[Dict], notify: bool,
                last_status: Optional[str] = None,
                failure: Optional[str] = None,
                previous_notified: bool = False,
                items: Optional[List[Dict]] = None) -> Dict:
    if event_name == "UserPromptSubmit":
        if failure is not None:
            return {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext":
                    "Spellguard: last check did not finish ({}). "
                    "Spellguard is installed; treat results as unknown.".format(
                        failure)}}
        rules = (report or {}).get("rules", [])
        if not rules and last_status is None:
            return {}
        if not rules and last_status is not None:
            still_failing = last_status in ("VIOLATED", "UNVERIFIED", "ERROR")
            if not still_failing:
                return {}
            return {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext":
                    "Spellguard: no ACTIVE rule; last check ended in {} "
                    "(上次检查未收口，这是上次状态，不是新检查结果).".format(last_status)}}
        if len(rules) == 1:
            rule = rules[0]
            context = "Spellguard: {} protects {}; check runs after changes.".format(
                rule["id"], _rule_context_line(rule))
        else:
            listed = ", ".join(
                "{} ({})".format(rule["id"], _rule_context_line(rule))
                for rule in rules)
            context = ("Spellguard: {} temporary rules: {}; check runs after "
                       "changes.".format(len(rules), listed))
        if last_status == "VIOLATED":
            context += " 上次检查有未解决违规（上次状态，尚未重新检查）。"
        elif last_status == "ERROR":
            context += " 上次检查未完成（上次状态，尚未重新检查）。"
        elif last_status == "UNVERIFIED":
            context += " 上次检查有未知边界未收口（上次状态，尚未重新检查）。"
        return {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context}}
    if event_name == "Stop":
        if not notify:
            return {}
        if items is not None:
            message = _aggregate_message(items)
            return {"systemMessage": message} if message else {}
        # Legacy callers pass no item list: keep the previous wording.
        if failure is not None:
            violation = _violation_message(report) if report else None
            if violation is not None:
                reason = failure if isinstance(failure, str) else "check incomplete"
                return {"systemMessage": "{} 检查未完成原因：{}；这不是 OPEN。"
                        .format(violation, reason)}
            return {"systemMessage":
                    "Spellguard: 检查未完成（{}）；这不是 OPEN，也不是无违规。"
                    .format(failure)}
        message = _violation_message(report)
        if message is not None:
            return {"systemMessage": message}
        return {}
    return {"systemMessage":
            "Spellguard: received an unsupported host event ({}).".format(
                event_name)}


def _violation_line(result: Dict) -> str:
    consumer = result["consumers"][0]
    symbol = result["protected_symbol"]
    try:
        module_path = _module_name(symbol)
    except ValueError:
        module_path = symbol["path"]
    location = "{}:{}".format(consumer["path"], consumer["lines"][0])
    return ("{}: {}.{} now has an external caller at {} ({}). "
            "Should this temporary implementation gain a cross-file "
            "dependency? Reason: {}").format(
        result["rule_id"], module_path, symbol["symbol"],
        location, consumer["symbol"], result["reason"])


def _unknown_line(result: Dict) -> str:
    symbol = result["protected_symbol"]
    try:
        module_path = _module_name(symbol)
    except ValueError:
        module_path = symbol["path"]
    evidence = "; ".join(
        "{} ({})".format(d.get("code"), d.get("path") or "repository")
        for d in result.get("diagnostics", [])) or "check incomplete"
    return ("{}: {}.{} could not be verified ({}); this is not OPEN."
            .format(result["rule_id"], module_path, symbol["symbol"], evidence))


def _aggregate_message(items: List[Dict]) -> str:
    lines: List[str] = []
    failures: List[str] = []
    for item in items:
        if item["kind"] == "managed":
            lines.append(str(item.get("message")))
        elif item["kind"] == "failure":
            failures.append(str(item.get("failure")))
        elif item["kind"] == "violation":
            lines.append(_violation_line(item["result"]))
        elif item["kind"] == "unknown":
            lines.append(_unknown_line(item["result"]))
    message = " ".join(lines)
    if lines:
        message = "{} (全部约定与位置：运行 spellguard check 查询)".format(message)
    if failures:
        reason = "; ".join(failures)
        if message:
            message = "{} 检查未完成原因：{}；这不是 OPEN。".format(message, reason)
        else:
            message = ("Spellguard: 检查未完成（{}）；这不是 OPEN，也不是无违规。"
                       .format(reason))
    return message


def _violation_message(report: Optional[Dict]) -> Optional[str]:
    if report is None:
        return None
    for result in report.get("results", []):
        if result.get("status") != "VIOLATED" or not result.get("consumers"):
            continue
        return _violation_line(result)
    return None


def _top_diagnostic_summary(report: Dict) -> str:
    evidence = _diagnostic_keys(report)
    return "; ".join("{} ({})".format(code, path or "repository")
                     for code, path in evidence) or "check incomplete"


_STATE_FIELDS = ("last_key", "last_status", "last_checked_at")


def _empty_state():
    return {"schema_version": STATE_SCHEMA, "last_key": None,
            "last_status": None, "last_checked_at": None, "notified": []}


def _common_state_fields_valid(state: Dict) -> bool:
    if state.get("last_status") not in STATUSES | {None}:
        return False
    return all(state.get(field) is None or isinstance(state[field], str)
               for field in _STATE_FIELDS)


@contextlib.contextmanager
def _state_directory(path: Path, repo_root: Optional[Path] = None):
    """Pin each directory fd; no path component or subsequent file follows links.

    The path is resolved first on POSIX systems whose well-known directories
    (/var, /tmp) are symlinks; after resolve() the path contains no link
    components, so the O_NOFOLLOW walk never trips on them (state resolution
    on macOS, Linux keeps /tmp itself direct)."""
    if path.is_symlink():
        raise HookFailure("state directory is a symbolic link; refusing")
    path = path.absolute().resolve()
    if ".." in path.parts:
        raise HookFailure("state directory cannot contain parent traversal")
    if repo_root is not None:
        root = repo_root.resolve()
        if path == root or root in path.parents:
            raise HookFailure("state directory must be outside the repository")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except (OSError, ValueError) as error:
        raise HookFailure("state storage unavailable: {}".format(error)) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _regular_file(directory, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HookFailure("{} is not a regular file".format(name))
    return fd


@contextlib.contextmanager
def _locked_state(path, repo_root=None):
    with _state_directory(path, repo_root) as directory:
        try:
            lock = _regular_file(
                directory, "lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            lock = _regular_file(directory, "lock", os.O_WRONLY)
        try:
            started = time.monotonic()
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - started >= LOCK_WAIT_SECONDS:
                        raise HookFailure("state lock timed out after 1s")
                    time.sleep(0.05)
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)


def _load_state(directory):
    try:
        fd = _regular_file(directory, "state.json", os.O_RDONLY)
    except FileNotFoundError:
        return _empty_state()
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        state = json.loads(stream.read(65537))
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int:
        raise HookFailure("state file has an unexpected shape")
    if state["schema_version"] == LEGACY_STATE_SCHEMA:
        if set(state) != {"schema_version", *_STATE_FIELDS}:
            raise HookFailure("state file has an unexpected shape")
        if not _common_state_fields_valid(state):
            raise HookFailure("state file has an unexpected shape")
        return {"schema_version": STATE_SCHEMA, "last_key": state["last_key"],
                "last_status": state["last_status"],
                "last_checked_at": state["last_checked_at"], "notified": []}
    if state["schema_version"] != STATE_SCHEMA:
        raise HookFailure("state file has an unsupported schema version")
    if set(state) != {"schema_version", *_STATE_FIELDS, "notified"}:
        raise HookFailure("state file has an unexpected shape")
    if not _common_state_fields_valid(state):
        raise HookFailure("state file has an unexpected shape")
    notified = state["notified"]
    if (not isinstance(notified, list)
            or any(not isinstance(item, str) for item in notified)):
        raise HookFailure("state file has an unexpected shape")
    return state


def load_state(state_dir: Path) -> Dict:
    with _locked_state(state_dir) as directory:
        return _load_state(directory)


def _write_state(directory, state):
    # Refuse a replaced destination link, too; os.replace itself never follows it.
    try:
        fd = _regular_file(directory, "state.json", os.O_RDONLY)
    except FileNotFoundError:
        pass
    else:
        os.close(fd)
    temporary = ".state-" + secrets.token_hex(8) + ".tmp"
    fd = _regular_file(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, sort_keys=True) + "\n")
        os.replace(temporary, "state.json", src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _append_event(directory, event):
    fd = _regular_file(directory, "events.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def _event(event, session_id, turn_id, key, output, report, exit_code, elapsed_ms):
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event,
            "session_id": session_id, "turn_id": turn_id, "key": key,
            "emitted": bool(output), "exit_code": exit_code,
            "elapsed_ms": elapsed_ms,
            "diagnostic_codes": sorted({code for code, _ in _diagnostic_keys(report)})}


def record_outcome(event: str, report: Optional[Dict], failure: Optional[str],
                   state_dir: Path, repo_root: Path, session_id: str, turn_id: str,
                   digest_value=None, exit_code=None, elapsed_ms=0,
                   fault_message: Optional[str] = None) -> Dict:
    key = notification_key(report, failure or fault_message, digest_value)
    statuses = {r["status"] for r in (report or {}).get("results", [])}
    status = ("VIOLATED" if "VIOLATED" in statuses
              else "ERROR" if (failure is not None or fault_message is not None)
              else "UNVERIFIED" if "UNVERIFIED" in statuses
              else "RESOLVED" if "RESOLVED" in statuses else "OPEN")
    with _locked_state(state_dir, repo_root) as directory:
        state = _load_state(directory)
        items = notification_items(report, failure, digest_value, fault_message)
        already = set(state["notified"])
        new_items = [item for item in items if item["key"] not in already]
        notify = bool(new_items)
        output = hook_output(event, report, notify, failure=failure,
                             items=items)
        # Log first: a failed log must never consume an unshown notification key.
        _append_event(directory, _event(event, session_id, turn_id, key, output,
                                       report, exit_code, elapsed_ms))
        _write_state(directory, {
            "schema_version": STATE_SCHEMA, "last_key": key,
            "last_status": status,
            "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "notified": sorted(item["key"] for item in items)})
    return {"notify": notify, "key": key, "status": status,
            "previous_status": state["last_status"], "output": output,
            "new_items": new_items}


def same_repository(repo_root: Path, working_directory: Path) -> bool:
    """True when cwd is repo_root or inside it, matching Git repo identity."""
    import subprocess

    def real(p: Path) -> Path:
        try:
            return p.resolve()
        except OSError:
            return p

    root = real(repo_root)
    candidate = real(working_directory)
    if candidate == root or root in candidate.parents:
        # A nested Git repository is a different installation target even
        # though it sits under the installed root.
        probe = candidate
        while probe != root and probe != probe.parent:
            if (probe / ".git").exists():
                return False  # nested repository boundary
            probe = probe.parent
        return True
    return False


def _validate_report(report, command, code):
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1 or report.get("command") != command
            or type(report.get("complete")) is not bool):
        raise HookFailure("unexpected CLI report shape")
    entries = report.get("results" if command == "check" else "rules")
    if not isinstance(entries, list) or not all(isinstance(r, dict) for r in entries):
        raise HookFailure("unexpected CLI entries")
    for diagnostics in [report.get("diagnostics")] + [r.get("diagnostics", []) for r in entries]:
        if not isinstance(diagnostics, list) or any(
                not isinstance(d, dict) or not isinstance(d.get("code"), str)
                or not isinstance(d.get("message"), str)
                or (d.get("path") is not None and not isinstance(d["path"], str))
                for d in diagnostics):
            raise HookFailure("unexpected CLI diagnostics")
    for entry in entries:
        identifier = "rule_id" if command == "check" else "id"
        symbol = entry.get("protected_symbol")
        if (not all(isinstance(entry.get(k), str) for k in
                    (identifier, "reason", "desired_state"))
                or not isinstance(symbol, dict)
                or not all(isinstance(symbol.get(k), str) for k in
                           ("path", "symbol", "source_root"))
                or not isinstance(symbol.get("source_root"), str)
                or not symbol["source_root"]
                or symbol["source_root"].startswith((".", "/"))
                or ".." in symbol["source_root"]):
            raise HookFailure("unexpected protected rule")
        try:
            _module_name(symbol)
        except ValueError as error:
            raise HookFailure("invalid protected symbol path") from error
    if command == "check":
        for result in entries:
            if (result.get("status") not in STATUSES - {"ERROR"}
                    or type(result.get("complete")) is not bool
                    or not isinstance(result.get("consumers"), list)):
                raise HookFailure("unexpected check result")
            for consumer in result["consumers"]:
                if (not isinstance(consumer, dict)
                        or not all(isinstance(consumer.get(k), str) for k in ("path", "symbol"))
                        or not isinstance(consumer.get("lines"), list)
                        or not consumer["lines"]
                        or any(type(n) is not int or n < 1 for n in consumer["lines"])):
                    raise HookFailure("unexpected consumer evidence")
        if report["complete"] and (any(not r["complete"] for r in entries)
                                   or _diagnostic_keys(report)):
            raise HookFailure("complete report contradicts diagnostics")
    expected = (2 if not report["complete"] else
                1 if command == "check" and any(r["status"] == "VIOLATED" for r in entries)
                else 0)
    if code != expected:
        raise HookFailure("CLI exit code contradicts report")


def _run_cli(repo_root, digest_value, command):
    started = time.monotonic()
    code = None
    report = None
    failure = None
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "from spellguard.cli import main; raise SystemExit(main())",
             command, "--registry-sha256", digest_value, "--format", "json"],
            cwd=repo_root, input="", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=CHECK_TIMEOUT_SECONDS)
        code = completed.returncode
        report = json.loads(completed.stdout)
        _validate_report(report, command, code)
        if not report["complete"]:
            failure = _top_diagnostic_summary(report)
    except (OSError, ValueError, HookFailure, subprocess.TimeoutExpired) as error:
        report = None
        failure = "{} could not finish: {}".format(command, error)
    return report, failure, code, round((time.monotonic() - started) * 1000, 3)


def _failure_output(report, error):
    return hook_output("Stop", report, True,
                       failure="{}; notification persistence is unavailable".format(error))


def run_managed_adapter(installation_id: str, payload_source,
                        protocol: str = "codex") -> Dict:
    """Consume only the committed confirmation digest of an installation.

    The managed Hook never confirms or recovers anything. While a confirmation
    is pending it reports the incompleteness and does not adopt the current
    registry. A registry that is still absent stays quiet.
    """
    from .installation import (
        InstallationError, _installation_path, installation_status,
        load_installation)
    text = payload_source if isinstance(payload_source, str) else payload_source.read()
    report = None
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise HookFailure("host event payload must be a JSON object")
        for field in ("hook_event_name", "cwd"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise HookFailure("{} must be a non-empty string".format(field))
        event = normalize_event(payload, protocol)
        record = load_installation(Path(payload["cwd"]), installation_id)
        installed_root = Path(record["repository_root"])
        if not same_repository(installed_root, Path(payload["cwd"])):
            raise HookFailure("event cwd is outside the installed repository")
        guidance = None
        if event == "UserPromptSubmit":
            from .marking import managed_agent_guidance
            guidance = managed_agent_guidance()
        state_dir = _installation_path(installation_id) / "hook"
        notice_context = {
            "event": event,
            "state_dir": state_dir,
            "repo_root": installed_root,
            "session_id": payload.get("session_id", "")
            if isinstance(payload.get("session_id"), str) else "",
            "turn_id": payload.get("turn_id", "")
            if isinstance(payload.get("turn_id"), str) else "",
            "guidance": guidance,
        }
        status = installation_status(installed_root)
        if status.get("confirmation_pending"):
            return _managed_notice(
                "Spellguard: a confirmation is not finished; treat results as "
                "unknown.", **notice_context)
        notices = {
            "unreadable":
                "the registry could not be read; treat results as unknown.",
            "drifted":
                "the registry no longer matches the confirmed digest; treat "
                "results as unknown.",
            "unbound":
                "a registry exists but is not bound to this installation; "
                "treat results as unknown.",
        }
        state = status.get("registry_state")
        if state in notices:
            return _managed_notice(
                "Spellguard: {}".format(notices[state]), **notice_context)
        digest = record.get("adopted_registry_digest")
        if digest is None:
            # Bound to "no registry yet": quiet, but still teach the workflow.
            if event == "UserPromptSubmit":
                return {"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": guidance}}
            # Record the clean turn so a cleared fault stops being "already
            # notified" and can alert again if it recurs.
            return record_outcome(
                event="Stop", report=None, failure=None, state_dir=state_dir,
                repo_root=installed_root,
                session_id=notice_context["session_id"],
                turn_id=notice_context["turn_id"],
                exit_code=0, elapsed_ms=0)["output"]
        output = run_adapter(str(installed_root), digest, str(state_dir),
                             payload_source=text, protocol=protocol)
        if event == "UserPromptSubmit":
            # Inject the short operating rule; the maintainer sees nothing.
            specific = output.get("hookSpecificOutput", {}).get(
                "additionalContext")
            context = (specific + " " + guidance if specific else guidance)
            return {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context}}
        return output
    except InstallationError as error:
        return _failure_output(report, error)
    except (HookFailure, OSError, ValueError, TypeError, KeyError) as error:
        return _failure_output(report, error)


def _managed_notice(message: str, *, event: str, state_dir: Path,
                    repo_root: Path, session_id: str, turn_id: str,
                    guidance: Optional[str] = None) -> Dict:
    """A managed fault notice. Stop notices go through the same dedup state as
    check notifications so an unchanged fault does not repeat."""
    if event == "UserPromptSubmit":
        context = message
        if guidance:
            context = "{} {}".format(context, guidance)
        return {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context}}
    outcome = record_outcome(
        event="Stop", report=None, failure=None, state_dir=state_dir,
        repo_root=repo_root, session_id=session_id, turn_id=turn_id,
        fault_message=message, exit_code=2, elapsed_ms=0)
    return outcome["output"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo")
    parser.add_argument("--registry-sha256")
    parser.add_argument("--state-dir")
    parser.add_argument("--installation-id")
    args = parser.parse_args(argv)
    if args.installation_id:
        if args.repo or args.state_dir or args.registry_sha256:
            parser.error("--installation-id is mutually exclusive with the "
                         "manual --repo/--state-dir/--registry-sha256 mode")
        output = run_managed_adapter(args.installation_id, sys.stdin.read())
    else:
        if not (args.repo and args.registry_sha256 and args.state_dir):
            parser.error("--repo, --state-dir and --registry-sha256 are "
                         "required without --installation-id")
        output = run_adapter(args.repo, args.registry_sha256, args.state_dir,
                             sys.stdin.read())
    sys.stdout.write(json.dumps(output))
    return 0


# Shared, host-agnostic runner for the three single-host adapters.
NORMALIZED_EVENTS = {"Stop": "Stop", "UserPromptSubmit": "UserPromptSubmit"}
REQUIRED_EVENTS = {"Stop", "UserPromptSubmit"}


def normalize_event(payload: Dict, protocol: str) -> str:
    event = payload.get("hook_event_name")
    if protocol == "cursor":
        event = {"beforeSubmitPrompt": "UserPromptSubmit",
                 "stop": "Stop"}.get(event, event)
    if event not in REQUIRED_EVENTS:
        raise HookFailure("unsupported host event: {}".format(event))
    return event


def run_adapter(repo_argument: str, digest_value: str, state_argument: str,
                payload_source=sys.stdin, protocol: str = "codex",
                ) -> Dict:
    """Protocol-agnostic flow shared by the Codex/Claude/Cursor adapters.
    Protocol differences live in the payload shape and output normalization
    of each thin script."""
    report = None
    try:
        repo = Path(repo_argument)
        state_dir = Path(state_argument)
        if not repo.is_absolute() or not state_dir.is_absolute():
            raise HookFailure("repo and state-dir must be absolute paths")
        payload = json.loads(payload_source) if isinstance(
            payload_source, str) else json.load(payload_source)
        if not isinstance(payload, dict):
            raise HookFailure("host event payload must be a JSON object")
        for field in ("hook_event_name", "cwd"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise HookFailure("{} must be a non-empty string".format(field))
        for field in ("session_id", "turn_id"):
            if not isinstance(payload.get(field, ""), str):
                raise HookFailure("{} must be a string".format(field))
        event = normalize_event(payload, protocol)
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
