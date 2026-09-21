"""AI-assisted TEMP proposal and confirmation (M01-M02).

propose_temporary: build a single-rule proposal with the existing registry
schema, validate it through parse_registry (so digests and rules stay
identical to the installed product), and store it atomically ONLY inside Git
metadata (spellguard/proposal.json under the .git directory). It never
touches .spellguard/rules.json and never reports a confirmed state.

confirm_proposal: promoted a previously shown proposal to the real registry
only when the caller supplies the exact digest the maintainer has seen. Any
mismatch, corrupted draft, already-existing registry, unsafe path or failed
atomic write aborts with zero side effects.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Dict

from .registry import REGISTRY_MAX_BYTES, RegistryError, parse_registry
from .repository import git_dir, repository_root

PROPOSAL_REL = "spellguard/proposal.json"


def _rule_dict(path: str, symbol: str, source_root: str,
               reason: str, desired_state: str) -> Dict:
    return {
        "schema_version": 1,
        "rules": [{
            "id": "TEMP-001",
            "classification": "temporary",
            "lifecycle": "ACTIVE",
            "reason": reason,
            "desired_state": desired_state,
            "protected_symbol": {"path": path, "symbol": symbol,
                                 "source_root": source_root},
            "window": "no_external_callers",
            "resolution_reason": None,
        }],
    }


@contextlib.contextmanager
def _storage_directory(base: Path, name: str, *, create: bool):
    """Open one fixed child directory without following a replaced link."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    base_fd = child_fd = None
    try:
        base_fd = os.open(os.fspath(base), flags)
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=base_fd)
            except FileExistsError:
                pass
        child_fd = os.open(name, flags, dir_fd=base_fd)
        yield child_fd
    except OSError as error:
        raise RegistryError(
            "REGISTRY_READ_FAILED",
            "storage path is unavailable: {}".format(error)) from error
    finally:
        if child_fd is not None:
            os.close(child_fd)
        if base_fd is not None:
            os.close(base_fd)


def _temporary_file(directory: int, prefix: str, payload: bytes) -> str:
    name = ".{}-{}.tmp".format(prefix, secrets.token_hex(8))
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RegistryError("REGISTRY_READ_FAILED",
                            "temporary path is not a regular file")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return name


def _atomic_replace_proposal(root: Path, payload: bytes) -> None:
    with _storage_directory(git_dir(root), "spellguard", create=True) as directory:
        try:
            status = os.stat("proposal.json", dir_fd=directory,
                             follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(status.st_mode):
                raise RegistryError(
                    "REGISTRY_READ_FAILED",
                    "proposal path must be a regular file")
        temporary = None
        try:
            temporary = _temporary_file(directory, "proposal", payload)
            os.replace(temporary, "proposal.json",
                       src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
        except OSError as error:
            raise RegistryError(
                "REGISTRY_READ_FAILED",
                "proposal write failed: {}".format(error)) from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass


def _read_proposal(root: Path) -> bytes:
    with _storage_directory(git_dir(root), "spellguard", create=False) as directory:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open("proposal.json", flags, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise RegistryError(
                    "REGISTRY_READ_FAILED",
                    "proposal path must be a regular file")
            with os.fdopen(descriptor, "rb") as stream:
                raw = stream.read(REGISTRY_MAX_BYTES + 1)
        except RegistryError:
            raise
        except OSError as error:
            raise RegistryError(
                "REGISTRY_READ_FAILED",
                "proposal could not be read: {}".format(error)) from error
        if len(raw) > REGISTRY_MAX_BYTES:
            raise RegistryError("REGISTRY_READ_FAILED",
                                "proposal exceeds the registry size limit")
        return raw


def _atomic_create_registry(root: Path, payload: bytes) -> bool:
    """Publish a complete registry without ever replacing an existing name."""
    with _storage_directory(repository_root(root), ".spellguard",
                            create=True) as directory:
        temporary = None
        try:
            temporary = _temporary_file(directory, "rules", payload)
            os.link(temporary, "rules.json",
                    src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
            temporary = None
            return True
        except FileExistsError as error:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                descriptor = os.open("rules.json", flags, dir_fd=directory)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise OSError("registry path is not a regular file")
                with os.fdopen(descriptor, "rb") as stream:
                    existing = stream.read(REGISTRY_MAX_BYTES + 1)
            except OSError:
                existing = None
            if existing == payload:
                return False
            raise RegistryError(
                "REGISTRY_INVALID",
                "a live registry already exists; refusing to overwrite it") from error
        except OSError as error:
            raise RegistryError(
                "REGISTRY_READ_FAILED",
                "registry write failed: {}".format(error)) from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass


def proposal_digest_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def propose_temporary(root: Path, *, path: str, symbol: str,
                      source_root: str, reason: str,
                      desired_state: str) -> Dict:
    """Build, validate and atomically store an unconfirmed TEMP proposal."""
    actual_root = repository_root(root)
    schema = _rule_dict(path, symbol, source_root, reason, desired_state)
    payload = json.dumps(schema, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode()
    registry = parse_registry(payload)  # strict validation, may raise
    _atomic_replace_proposal(actual_root, payload)
    digest = registry.digest
    rule = registry.rules[0]
    summary = ("Proposed TEMP-001: temporary={!r}; remove when: {!r}; "
               "protects {} in {} (source_root={})".format(
                   rule.reason, rule.desired_state,
                   rule.protected_symbol.symbol,
                   rule.protected_symbol.path,
                   rule.protected_symbol.source_root))
    return {
        "command": "propose",
        "proposal_digest": digest,
        "rule": {
            "id": rule.id,
            "protected_symbol": {
                "path": rule.protected_symbol.path,
                "symbol": rule.protected_symbol.symbol,
                "source_root": rule.protected_symbol.source_root,
            },
            "reason": rule.reason,
            "desired_state": rule.desired_state,
        },
        "confirmed": False,
        "proposal_path": PROPOSAL_REL,
        "summary": summary,
    }


def confirm_proposal(root: Path, expected_digest: str) -> Dict:
    """Promote the shown proposal to the real registry (M02, atomic)."""
    actual_root = repository_root(root)
    raw = _read_proposal(actual_root)
    registry = parse_registry(raw)  # strict validation of the draft
    if registry.digest != expected_digest:
        raise RegistryError(
            "REGISTRY_CHANGED",
            "proposal digest does not match the digest you confirmed")
    registry_created = _atomic_create_registry(actual_root, raw)
    # Registry is live and equivalent to the proposal: safe to clean the draft.
    proposal_cleaned = True
    try:
        with _storage_directory(git_dir(actual_root), "spellguard",
                                create=False) as directory:
            os.unlink("proposal.json", dir_fd=directory)
    except (OSError, RegistryError):
        proposal_cleaned = False
    return {
        "command": "confirm",
        "confirmed": True,
        "already_confirmed": not registry_created,
        "proposal_cleaned": proposal_cleaned,
        "registry_digest": registry.digest,
        "next": ["spellguard context --registry-sha256 {}".format(
            registry.digest),
            "spellguard check --registry-sha256 {}".format(registry.digest)],
    }


def managed_agent_guidance() -> str:
    """One short line injected through UserPromptSubmit additionalContext."""
    return (
        "Spellguard is installed for this repository. If you deliberately "
        "introduce a temporary compromise, run `spellguard propose` with "
        "--installation-id, show the maintainer the target, reason and end "
        "state, and wait. Only after the maintainer explicitly confirms, run "
        "confirm. Never confirm on your own; normally stay silent."
    )


def agent_instructions() -> str:
    return (
        "Spellguard keeps already-confirmed temporary compromises from "
        "silently spreading. These rules are for you, the coding agent; the "
        "maintainer should only have to read intent in ordinary conversation.\n"
        "\n"
        "Authorization and host trust are separate: the maintainer's approval "
        "of a specific intent is not the same as Codex's native project/hook "
        "trust review. Never claim the host trust step has been removed.\n"
        "\n"
        "When you, the coding agent, deliberately introduce a temporary "
        "compatibility compromise in this session:\n"
        "\n"
        "1. Read the installation id yourself with `spellguard status "
        "--format json` (never make the maintainer copy technical ids).\n"
        "2. Draft the change with `spellguard propose` and --installation-id; "
        "add the target path/symbol/source-root, the reason, and the end state "
        "(or `--resolve TEMP_ID --reason ...` to close one).\n"
        "3. Show the maintainer the object, the reason and the end state in "
        "plain language, and wait. Do not show digests or file paths as the "
        "thing they must judge.\n"
        "4. Only after the maintainer explicitly confirms that specific "
        "proposal, run `spellguard confirm --proposal-sha256 <digest> "
        "--installation-id <id>`. The digest stays with you.\n"
        "5. After confirmation, run the real check once and give a short "
        "result. If the check is incomplete, say the agreement was saved but "
        "the current comparison is unverified; do not commit, change the "
        "baseline, or rebuild trust records yourself.\n"
        "\n"
        "If the maintainer declines, is unsure, or does not answer, do not "
        "register anything and do not ask again in the same turn. Never run "
        "confirm without that explicit confirmation, never parse free text to "
        "auto-confirm, never call a model to obtain confirmation, and never "
        "invent host buttons. A proposal is not a CAND, a business claim, or a "
        "confirmed rule.\n"
        "\n"
        "Reminders are static observations: report that a temporary function "
        "gained a new external caller and ask whether to keep it isolated or "
        "explicitly close it; do not assert a business defect. Nothing to "
        "report means no user-visible output at all. Query with `spellguard "
        "status`; disable with `spellguard setup --remove`.\n"
    )


__all__ = ["propose_temporary", "confirm_proposal", "agent_instructions",
           "managed_agent_guidance", "proposal_digest_of"]
