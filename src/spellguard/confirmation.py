"""Managed TEMP confirmation transaction (S02, ARCHITECTURE §12.3).

The managed proposal is a versioned envelope stored ONLY in Git metadata
(``spellguard/managed-proposal.json``): it binds the installation id, the
operation, the exact registry preimage, the resulting registry and its digest.
The whole envelope is hashed with canonical JSON; replacing a proposal makes the
old digest unconfirmable.

confirm_change runs a bounded transaction inside the installation lock: write an
external pending record, re-verify the registry preimage, write the registry,
commit the external confirmation digest, then clear pending. recover_confirmation
only replays an existing pending record; with no pending it never creates trust.
After the lock the real check runs once and its actual exit status is reported,
even when registration succeeded and the check is incomplete.

External storage primitives are reused from installation.py; Git-metadata
proposal storage reuses marking.py's directory-FD helpers (original callers
``_atomic_replace_proposal``/``_read_proposal``).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import installation
from .installation import (
    CONTROL_MAX_BYTES,
    InstallationError,
    _canonical,
    _installation_lock,
    _now,
    _parse_record,
    _read_control_from_dir,
    _read_optional_file,
    _sha256_text,
    _unlink_control,
    _write_control,
    load_installation,
)
from .marking import _storage_directory, _temporary_file
from .registry import (
    MAX_RULES,
    REGISTRY_MAX_BYTES,
    RegistryError,
    parse_registry,
)
from .repository import RepositoryError, git_dir, repository_root

MANAGED_PROPOSAL_NAME = "managed-proposal.json"
LEGACY_PROPOSAL_NAME = "proposal.json"
CONFIRMATION_NAME = "confirmation.json"
PENDING_NAME = "confirmation-pending.json"
INSTALLATION_NAME = "installation.json"
CONFIRMATION_SCHEMA = 1
REGISTRY_REL = ".spellguard/rules.json"
ADD_FIELDS = frozenset({"path", "symbol", "source_root", "reason",
                        "desired_state"})
RESOLVE_FIELDS = frozenset({"rule_id", "reason"})
ENVELOPE_FIELDS = frozenset({
    "schema_version", "installation_id", "operation",
    "expected_registry_digest", "result_registry", "result_registry_digest",
})
CONFIRMATION_FIELDS = frozenset({
    "schema_version", "installation_id", "operation", "proposal_digest",
    "result_registry_digest", "confirmed_at",
})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TEMP_ID = re.compile(r"^TEMP-(\d{3,})$")


class _DuplicateKey(Exception):
    pass


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _load_json(raw: bytes, code: str, message: str) -> Dict:
    try:
        value = json.loads(raw.decode("utf-8"),
                           object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey):
        raise InstallationError(code, message)
    if not isinstance(value, dict):
        raise InstallationError(code, message)
    return value


# ---------------------------------------------------------------------------
# Git-metadata managed proposal
# ---------------------------------------------------------------------------

def _read_git_control(root: Path, name: str) -> Optional[bytes]:
    metadata = git_dir(root) / "spellguard"
    try:
        os.lstat(os.fspath(metadata))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallationError(
            "CONFIRMATION_UNSAFE",
            "Git metadata storage is unavailable") from error
    try:
        with _storage_directory(git_dir(root), "spellguard",
                                create=False) as directory:
            flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
            try:
                descriptor = os.open(name, flags, dir_fd=directory)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise InstallationError(
                    "CONFIRMATION_UNSAFE",
                    "proposal path is not a safe regular file") from error
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise InstallationError(
                        "CONFIRMATION_UNSAFE",
                        "proposal path is not a regular file")
                data = b""
                while len(data) <= CONTROL_MAX_BYTES:
                    chunk = os.read(descriptor,
                                    min(65536, CONTROL_MAX_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                if len(data) > CONTROL_MAX_BYTES:
                    raise InstallationError(
                        "CONFIRMATION_TOO_LARGE",
                        "proposal exceeds the control size limit")
                return data
            finally:
                os.close(descriptor)
    except RegistryError as error:
        raise InstallationError(
            "CONFIRMATION_UNSAFE",
            "Git metadata storage is unavailable") from error


def _write_git_control(root: Path, name: str, payload: bytes) -> None:
    try:
        with _storage_directory(git_dir(root), "spellguard",
                                create=True) as directory:
            try:
                status = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(status.st_mode):
                    raise InstallationError(
                        "CONFIRMATION_UNSAFE",
                        "proposal path must be a regular file")
            temporary = _temporary_file(
                directory, name.split(".")[0].replace("-", "_"), payload)
            try:
                os.replace(temporary, name, src_dir_fd=directory,
                           dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except RegistryError as error:
        raise InstallationError(
            "CONFIRMATION_WRITE_FAILED",
            "proposal could not be written") from error


def _envelope_digest(envelope: Dict) -> str:
    return _sha256_text(_canonical(envelope))


def _parse_envelope(raw: bytes, installation_id: str) -> Dict:
    envelope = _load_json(raw, "CONFIRMATION_INVALID",
                          "managed proposal is not valid JSON")
    unknown = set(envelope) - ENVELOPE_FIELDS
    missing = ENVELOPE_FIELDS - set(envelope)
    if unknown or missing:
        raise InstallationError(
            "CONFIRMATION_INVALID", "managed proposal has an unexpected shape")
    if envelope["schema_version"] != CONFIRMATION_SCHEMA:
        raise InstallationError(
            "CONFIRMATION_INVALID", "unsupported managed proposal version")
    if envelope["installation_id"] != installation_id:
        raise InstallationError(
            "CONFIRMATION_INVALID",
            "managed proposal belongs to another installation")
    if envelope["operation"] not in ("add", "resolve"):
        raise InstallationError(
            "CONFIRMATION_INVALID", "managed proposal operation is invalid")
    expected = envelope["expected_registry_digest"]
    if expected is not None and not _DIGEST.match(str(expected)):
        raise InstallationError(
            "CONFIRMATION_INVALID", "expected registry digest is malformed")
    result = envelope["result_registry"]
    if not isinstance(result, dict):
        raise InstallationError(
            "CONFIRMATION_INVALID", "result registry must be an object")
    try:
        digest = parse_registry(_canonical(result).encode("utf-8")).digest
    except RegistryError as error:
        raise InstallationError(
            "CONFIRMATION_INVALID",
            "result registry is not valid: {}".format(error.code))
    if envelope["result_registry_digest"] != digest:
        raise InstallationError(
            "CONFIRMATION_INVALID", "result registry digest does not match")
    return envelope


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _read_registry(root: Path) -> Tuple[bool, Optional[Dict], Optional[str]]:
    raw = _read_optional_file(root, REGISTRY_REL, REGISTRY_MAX_BYTES)
    if raw is None:
        return False, None, None
    try:
        registry = parse_registry(raw)
    except RegistryError as error:
        raise InstallationError(
            "REGISTRY_READ_FAILED",
            "registry could not be read: {}".format(error.code))
    schema = _load_json(raw, "REGISTRY_READ_FAILED",
                        "registry is not valid JSON")
    return True, schema, registry.digest


def _next_rule_id(rules) -> str:
    highest = 0
    for rule in rules:
        match = _TEMP_ID.match(str(rule.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return "TEMP-{:03d}".format(highest + 1)


def _build_add(schema: Optional[Dict], fields: Dict) -> Dict:
    rules = list(schema["rules"]) if schema else []
    if len(rules) >= MAX_RULES:
        raise InstallationError(
            "REGISTRY_LIMIT",
            "at most {} TEMP rules are supported".format(MAX_RULES))
    identity = (fields["path"], fields["symbol"], fields["source_root"])
    for existing in rules:
        if existing.get("lifecycle") != "ACTIVE":
            continue
        symbol = existing.get("protected_symbol", {})
        if (symbol.get("path"), symbol.get("symbol"),
                symbol.get("source_root")) == identity:
            raise InstallationError(
                "RULE_DUPLICATE",
                "an ACTIVE TEMP already protects this symbol")
    rule = {
        "id": _next_rule_id(rules),
        "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": fields["reason"],
        "desired_state": fields["desired_state"],
        "protected_symbol": {
            "path": fields["path"],
            "symbol": fields["symbol"],
            "source_root": fields["source_root"],
        },
        "window": "no_external_callers",
        "resolution_reason": None,
    }
    return {"schema_version": 1, "rules": rules + [rule]}


def _build_resolve(schema: Dict, fields: Dict) -> Dict:
    reason = fields["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise InstallationError(
            "CONFIRMATION_INVALID", "resolve requires a non-empty reason")
    target = fields["rule_id"]
    found = False
    rules = []
    for rule in schema["rules"]:
        if rule.get("id") == target:
            if rule.get("lifecycle") != "ACTIVE":
                raise InstallationError(
                    "RULE_NOT_ACTIVE", "only an ACTIVE rule can be resolved")
            updated = dict(rule)
            updated["lifecycle"] = "RESOLVED"
            updated["resolution_reason"] = reason
            rules.append(updated)
            found = True
        else:
            rules.append(rule)
    if not found:
        raise InstallationError("RULE_NOT_FOUND", "no such TEMP rule")
    return {"schema_version": 1, "rules": rules}


def _require_bound_absent(record: Dict) -> None:
    if record.get("adopted_registry_digest") is not None:
        raise InstallationError(
            "REGISTRY_MISSING",
            "a previously confirmed registry is missing; refusing to treat it "
            "as absent")


def _require_adopted(record: Dict, present: bool,
                     digest: Optional[str]) -> None:
    """Current content must equal the last externally confirmed digest.

    The proposal's own preimage is not enough: a manual edit made before the
    proposal would otherwise be silently adopted by the next confirmation.
    """
    if not present:
        _require_bound_absent(record)
        return
    adopted = record.get("adopted_registry_digest")
    if adopted is None or adopted != digest:
        raise InstallationError(
            "REGISTRY_DRIFT",
            "the registry does not match the last confirmed digest; refusing to "
            "build or apply a change on drifted content")


def _rule_summary(operation: str, schema: Dict,
                  target_rule_id: Optional[str] = None) -> Dict:
    if operation == "add":
        rule = schema["rules"][-1]
        return {
            "id": rule["id"],
            "reason": rule["reason"],
            "desired_state": rule["desired_state"],
            "protected_symbol": dict(rule["protected_symbol"]),
        }
    for rule in schema["rules"]:
        if rule.get("id") == target_rule_id:
            return {"id": rule["id"],
                    "resolution_reason": rule.get("resolution_reason")}
    return {}


# ---------------------------------------------------------------------------
# propose_change
# ---------------------------------------------------------------------------

def propose_change(root: Path, installation_id: str, *,
                   operation: str, rule_fields: Dict) -> Dict:
    if operation not in ("add", "resolve"):
        raise InstallationError(
            "CONFIRMATION_OPERATION", "operation must be add or resolve")
    fields = dict(rule_fields or {})
    record = load_installation(Path(root), installation_id)
    if record["lifecycle"] != "active":
        raise InstallationError(
            "INSTALLATION_DISABLED", "the installation is not active")
    repo_root = repository_root(Path(root))
    if _read_git_control(repo_root, LEGACY_PROPOSAL_NAME) is not None:
        raise InstallationError(
            "LEGACY_PROPOSAL_CONFLICT",
            "an unmanaged proposal exists; finish or remove it before using "
            "the managed flow")
    with _installation_lock(installation_id):
        return _propose_locked(repo_root, installation_id, record, operation,
                               fields)


def _propose_locked(repo_root: Path, installation_id: str, record: Dict,
                    operation: str, fields: Dict) -> Dict:
    present, schema, digest = _read_registry(repo_root)
    _require_adopted(record, present, digest)
    if operation == "add":
        unknown = set(fields) - ADD_FIELDS
        missing = ADD_FIELDS - set(fields)
        if unknown:
            raise InstallationError(
                "CONFIRMATION_FIELD",
                "unsupported rule field: {}".format(sorted(unknown)[0]))
        if missing:
            raise InstallationError(
                "CONFIRMATION_FIELD",
                "missing rule field: {}".format(sorted(missing)[0]))
        if not present:
            _require_bound_absent(record)
        expected = digest
        result = _build_add(schema, fields)
    else:
        unknown = set(fields) - RESOLVE_FIELDS
        missing = RESOLVE_FIELDS - set(fields)
        if unknown:
            raise InstallationError(
                "CONFIRMATION_FIELD",
                "unsupported resolve field: {}".format(sorted(unknown)[0]))
        if missing:
            raise InstallationError(
                "CONFIRMATION_FIELD",
                "missing resolve field: {}".format(sorted(missing)[0]))
        if not present:
            raise InstallationError(
                "REGISTRY_MISSING", "no registry is bound to this installation")
        expected = digest
        result = _build_resolve(schema, fields)

    result_digest = parse_registry(_canonical(result).encode("utf-8")).digest
    envelope = {
        "schema_version": CONFIRMATION_SCHEMA,
        "installation_id": installation_id,
        "operation": operation,
        "expected_registry_digest": expected,
        "result_registry": result,
        "result_registry_digest": result_digest,
    }
    proposal_digest = _envelope_digest(envelope)
    _write_git_control(repo_root, MANAGED_PROPOSAL_NAME,
                       _canonical(envelope).encode("utf-8"))
    return {
        "schema_version": CONFIRMATION_SCHEMA,
        "command": "propose",
        "operation": operation,
        "installation_id": installation_id,
        "proposal_digest": proposal_digest,
        "expected_registry_digest": expected,
        "result_registry_digest": result_digest,
        "rule": _rule_summary(operation, result, fields.get("rule_id")),
        "confirmed": False,
        "proposal_path": "spellguard/{}".format(MANAGED_PROPOSAL_NAME),
        "next": "spellguard confirm --proposal-sha256 {} --installation-id {}"
                .format(proposal_digest, installation_id),
        "diagnostics": [],
    }


# ---------------------------------------------------------------------------
# External confirmation records
# ---------------------------------------------------------------------------

def _parse_confirmation(raw: bytes, installation_id: str) -> Dict:
    record = _load_json(raw, "CONFIRMATION_INVALID",
                        "confirmation record is not valid JSON")
    if set(record) != CONFIRMATION_FIELDS:
        raise InstallationError(
            "CONFIRMATION_INVALID", "confirmation record has an unexpected shape")
    if record["installation_id"] != installation_id:
        raise InstallationError(
            "CONFIRMATION_INVALID", "confirmation record belongs elsewhere")
    if not _DIGEST.match(str(record["proposal_digest"])):
        raise InstallationError(
            "CONFIRMATION_INVALID", "confirmation record digest is malformed")
    return record


def _read_confirmation_from_dir(directory: int,
                                installation_id: str) -> Optional[Dict]:
    raw = _read_control_from_dir(directory, CONFIRMATION_NAME)
    if raw is None:
        return None
    return _parse_confirmation(raw, installation_id)


def _read_pending(directory: int) -> Optional[Dict]:
    raw = _read_control_from_dir(directory, PENDING_NAME)
    if raw is None:
        return None
    pending = _load_json(raw, "CONFIRMATION_INVALID",
                         "confirmation pending record is not valid JSON")
    required = {"schema_version", "installation_id", "operation",
                "proposal_digest", "preimage", "result_registry",
                "result_registry_digest"}
    if set(pending) != required or pending["schema_version"] != CONFIRMATION_SCHEMA:
        raise InstallationError(
            "CONFIRMATION_INVALID", "pending record has an unexpected shape")
    return pending


def _write_pending(directory: int, pending: Dict) -> None:
    _write_control(directory, PENDING_NAME,
                   _canonical(pending).encode("utf-8"), mode=0o600)


def _write_registry_file(root: Path, schema: Dict) -> None:
    payload = _canonical(schema).encode("utf-8")
    if len(payload) > REGISTRY_MAX_BYTES:
        raise InstallationError(
            "REGISTRY_LIMIT", "resulting registry exceeds 64 KiB")
    directory = installation._open_child_directory(root, ".spellguard",
                                                   create=True)
    assert directory is not None
    try:
        _write_control(directory, "rules.json", payload, mode=0o644)
    finally:
        os.close(directory)


def _commit_confirmation(directory: int, pending: Dict) -> None:
    raw = _read_control_from_dir(directory, INSTALLATION_NAME)
    if raw is None:
        raise InstallationError(
            "INSTALLATION_NOT_FOUND", "installation record is missing")
    record = _parse_record(raw)
    confirmation = {
        "schema_version": CONFIRMATION_SCHEMA,
        "installation_id": pending["installation_id"],
        "operation": pending["operation"],
        "proposal_digest": pending["proposal_digest"],
        "result_registry_digest": pending["result_registry_digest"],
        "confirmed_at": _now(),
    }
    _write_control(directory, CONFIRMATION_NAME,
                   _canonical(confirmation).encode("utf-8"), mode=0o600)
    record["adopted_registry_digest"] = pending["result_registry_digest"]
    record["updated_at"] = _now()
    _write_control(directory, INSTALLATION_NAME,
                   _canonical(record).encode("utf-8"), mode=0o600)


def _matches_preimage(preimage: Dict, present: bool,
                      digest: Optional[str]) -> bool:
    if bool(preimage.get("present")) != present:
        return False
    if present:
        return preimage.get("digest") == digest
    return True


def _execute_confirmation(directory: int, pending: Dict,
                          root: Path) -> bool:
    target = pending["result_registry_digest"]
    present, _schema, digest = _read_registry(root)
    if present and digest == target:
        pass
    elif _matches_preimage(pending["preimage"], present, digest):
        _write_registry_file(root, pending["result_registry"])
    else:
        raise InstallationError(
            "CONFIRMATION_CONFLICT",
            "registry changed to unrecognized content; refusing to overwrite it")
    _commit_confirmation(directory, pending)
    try:
        _unlink_control(directory, PENDING_NAME)
        return True
    except InstallationError:
        return False


def _report_diagnostics(report: Dict) -> list:
    collected = []
    seen = set()
    entries = list(report.get("diagnostics", []) or [])
    for result in report.get("results", []) or []:
        entries.extend(result.get("diagnostics", []) or [])
    for diagnostic in entries:
        item = {"code": diagnostic.get("code"),
                "message": diagnostic.get("message"),
                "path": diagnostic.get("path")}
        key = (item["code"], item["message"], item["path"])
        if key in seen:
            continue
        seen.add(key)
        collected.append(item)
    return collected


def _run_check(root: Path, digest: str):
    from .window_review import review_windows
    diagnostics = []
    try:
        report, code = review_windows(root, digest)
        return code, bool(report.get("complete")), _report_diagnostics(report)
    except (RegistryError, RepositoryError) as error:
        diagnostics.append({
            "code": getattr(error, "code", "CHECK_FAILED"),
            "message": getattr(error, "message", str(error)),
        })
        return 2, False, diagnostics
    except Exception as error:  # pragma: no cover - defensive
        diagnostics.append({"code": "CHECK_FAILED", "message": str(error)})
        return 2, False, diagnostics


def _confirmed_result(source: Dict, root: Path, *, already: bool,
                      recovery_required: bool = False,
                      command: str = "confirm") -> Dict:
    code, complete, diagnostics = _run_check(
        root, source["result_registry_digest"])
    return {
        "schema_version": CONFIRMATION_SCHEMA,
        "command": command,
        "confirmed": True,
        "already_confirmed": already,
        "installation_id": source["installation_id"],
        "operation": source["operation"],
        "proposal_digest": source["proposal_digest"],
        "registry_digest": source["result_registry_digest"],
        "recovery_required": recovery_required,
        "check_exit_code": code,
        "check_complete": complete,
        "host_verified": False,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# confirm_change / recover_confirmation
# ---------------------------------------------------------------------------

def _verify_preimage(envelope: Dict, record: Dict, root: Path) -> None:
    present, _schema, digest = _read_registry(root)
    expected = envelope["expected_registry_digest"]
    if expected is None:
        if present:
            raise InstallationError(
                "REGISTRY_CHANGED",
                "a registry appeared after the proposal; preview again")
    elif not present or digest != expected:
        raise InstallationError(
            "REGISTRY_CHANGED",
            "the registry changed after the proposal; preview again")
    # The proposal preimage alone is not trust: it must also equal the last
    # externally confirmed digest, or an earlier manual edit gets adopted.
    _require_adopted(record, present, digest)


def confirm_change(root: Path, installation_id: str,
                   expected_proposal_digest: str) -> Dict:
    if not _DIGEST.match(expected_proposal_digest or ""):
        raise InstallationError(
            "PROPOSAL_INVALID", "proposal digest must be 64 hex characters")
    repo_root = repository_root(Path(root))
    record = load_installation(repo_root, installation_id)
    if record["lifecycle"] != "active":
        raise InstallationError(
            "INSTALLATION_DISABLED", "the installation is not active")

    with _installation_lock(installation_id) as directory:
        # A pending record always wins: it may be a half-committed operation
        # (confirmation record written, installation digest not yet updated).
        # Returning "already confirmed" here would leave the Hook reporting an
        # unfinished confirmation forever.
        active = _read_pending(directory)
        if active is not None:
            if active["proposal_digest"] != expected_proposal_digest:
                raise InstallationError(
                    "CONFIRMATION_BUSY",
                    "a different confirmation is pending; recover it first")
            cleanup_ok = _execute_confirmation(directory, active, repo_root)
            return _confirmed_result(active, repo_root, already=False,
                                     recovery_required=not cleanup_ok)
        existing = _read_confirmation_from_dir(directory, installation_id)
        if existing is not None and (
                existing["proposal_digest"] == expected_proposal_digest):
            present, _schema, digest = _read_registry(repo_root)
            if (not present or digest != existing["result_registry_digest"]
                    or record.get("adopted_registry_digest")
                    != existing["result_registry_digest"]):
                raise InstallationError(
                    "CONFIRMATION_CONFLICT",
                    "the external confirmation does not match the current "
                    "registry or installation digest; refusing to report it "
                    "as confirmed")
            return _confirmed_result(existing, repo_root, already=True)
        # Re-read the envelope under the lock so a competing propose cannot be
        # confirmed under an old digest.
        raw = _read_git_control(repo_root, MANAGED_PROPOSAL_NAME)
        if raw is None:
            raise InstallationError(
                "PROPOSAL_NOT_FOUND", "no managed proposal is pending")
        envelope = _parse_envelope(raw, installation_id)
        if _envelope_digest(envelope) != expected_proposal_digest:
            raise InstallationError(
                "PROPOSAL_CHANGED",
                "proposal digest does not match the digest you confirmed")
        _verify_preimage(envelope, record, repo_root)
        pending = {
            "schema_version": CONFIRMATION_SCHEMA,
            "installation_id": installation_id,
            "operation": envelope["operation"],
            "proposal_digest": expected_proposal_digest,
            "preimage": {
                "present": envelope["expected_registry_digest"] is not None,
                "digest": envelope["expected_registry_digest"],
            },
            "result_registry": envelope["result_registry"],
            "result_registry_digest": envelope["result_registry_digest"],
        }
        _write_pending(directory, pending)
        cleanup_ok = _execute_confirmation(directory, pending, repo_root)
        return _confirmed_result(pending, repo_root, already=False,
                                 recovery_required=not cleanup_ok)


def recover_confirmation(root: Path, installation_id: str) -> Dict:
    repo_root = repository_root(Path(root))
    record = load_installation(repo_root, installation_id)
    if record["lifecycle"] != "active":
        raise InstallationError(
            "INSTALLATION_DISABLED", "the installation is not active")
    with _installation_lock(installation_id) as directory:
        pending = _read_pending(directory)
        if pending is None:
            return {
                "schema_version": CONFIRMATION_SCHEMA,
                "command": "recover",
                "recovered": False,
                "recovery_required": False,
                "installation_id": installation_id,
                "confirmed": False,
                "host_verified": False,
                "diagnostics": [],
            }
        if pending["installation_id"] != installation_id:
            raise InstallationError(
                "CONFIRMATION_INVALID",
                "pending record belongs to another installation")
        cleanup_ok = _execute_confirmation(directory, pending, repo_root)
        result = _confirmed_result(pending, repo_root, already=False,
                                   recovery_required=not cleanup_ok,
                                   command="recover")
        result["recovered"] = True
        return result


__all__ = [
    "propose_change",
    "confirm_change",
    "recover_confirmation",
]
