"""Codex-first installation control plane (S01).

The tool owns exactly the two command entries it adds to
``<repo>/.codex/hooks.json`` (``UserPromptSubmit`` and ``Stop``). Everything else
in that file keeps its value and order. Durable state that must survive a
reinstall lives outside the repository in
``~/.spellguard/installations/<installation-id>/`` (overridable with
``SPELLGUARD_HOME`` for tests and isolated installs).

``plan_setup`` is side-effect free: it renders a plan and never writes host
configuration, manifests or confirmation records. ``apply_setup`` only executes
a plan whose canonical digest the caller echoes back; it re-derives the plan from
the current file and refuses when the host configuration preimage changed. A
pending record is written before the host configuration so an interrupted setup
or removal can be resumed by repeating the same authorized digest.

Safe storage primitives mirror marking.py (same directory-FD / O_NOFOLLOW /
fsync / same-directory replace discipline; original callers there are
``_atomic_replace_proposal``, ``_read_proposal`` and ``_atomic_create_registry``).
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from .registry import REGISTRY_MAX_BYTES, RegistryError, parse_registry
from .repository import git_common_dir, git_dir, repository_root

CONFIG_REL = ".codex/hooks.json"
TOML_REL = ".codex/config.toml"
REGISTRY_REL = ".spellguard/rules.json"

CONTROL_MAX_BYTES = 64 * 1024
HOOK_TIMEOUT = 15
EVENTS = ("UserPromptSubmit", "Stop")
INSTALLATION_SCHEMA = 1
PENDING_NAME = "setup-pending.json"
RECORD_NAME = "installation.json"
CONFIRMATION_PENDING_NAME = "confirmation-pending.json"
_TEMP_ID = re.compile(r"^TEMP-\d{3,}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

RECORD_FIELDS = frozenset({
    "schema_version", "installation_id", "repository_root", "git_common_dir",
    "host", "config_rel", "entry", "hook_command", "timeout",
    "created_config", "lifecycle", "adopted_registry_digest",
    "registry_state_at_install", "applied_plan_digest", "applied_operation",
    "created_at", "updated_at",
})


class InstallationError(Exception):
    """A visible installation failure; never silently treated as success."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


class _DuplicateKey(Exception):
    pass


def _reject_duplicates(pairs: Any) -> Dict:
    result: Dict = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Identity and entry point
# ---------------------------------------------------------------------------

def _reject_nested(repo_root: Path) -> None:
    parent = repo_root.parent
    while parent != parent.parent:
        if os.path.lexists(os.fspath(parent / ".git")):
            raise InstallationError(
                "NESTED_REPOSITORY",
                "this checkout is nested inside another Git repository")
        parent = parent.parent


def _identity(root: Path) -> Tuple[str, Path, Path]:
    repo_root = repository_root(Path(root))
    common = git_common_dir(repo_root)
    local = git_dir(repo_root)
    if os.path.realpath(os.fspath(local)) != os.path.realpath(os.fspath(common)):
        raise InstallationError(
            "WORKTREE_UNSUPPORTED",
            "linked worktrees are not supported; run setup from the main "
            "checkout")
    _reject_nested(repo_root)
    identifier = _sha256_text(
        "{}\0{}".format(repo_root, common))
    return identifier, repo_root, common


def installation_identity(root: Path) -> Tuple[str, Path, Path]:
    """Public identity helper: (installation_id, canonical root, common dir)."""
    return _identity(Path(root))


@dataclass(frozen=True)
class _Entry:
    path: Path
    argv: Tuple[str, ...]


def resolve_entry() -> _Entry:
    """Return the absolute package entry point used by the managed Hook."""
    override = os.environ.get("SPELLGUARD_ENTRY")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file() or not os.access(os.fspath(candidate), os.X_OK):
            raise InstallationError(
                "ENTRY_NOT_FOUND",
                "the configured spellguard entry point is not an executable file")
        resolved = candidate.resolve()
        return _Entry(resolved, (str(resolved),))
    script = Path(sys.executable).parent / "spellguard"
    if script.is_file() and os.access(os.fspath(script), os.X_OK):
        resolved = script.resolve()
        return _Entry(resolved, (str(resolved),))
    found = shutil.which("spellguard")
    if found and os.access(found, os.X_OK):
        resolved = Path(found).resolve()
        return _Entry(resolved, (str(resolved),))
    python = Path(sys.executable).resolve()
    return _Entry(python, (str(python), "-m", "spellguard"))


def _hook_command(entry: _Entry, installation_id: str) -> str:
    argv = list(entry.argv) + ["hook", "--host", "codex",
                               "--installation-id", installation_id]
    return " ".join(shlex.quote(part) for part in argv)


# ---------------------------------------------------------------------------
# External control storage: ~/.spellguard/installations/<id>/
# ---------------------------------------------------------------------------

def _base_path() -> Path:
    override = os.environ.get("SPELLGUARD_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".spellguard"
    return base


def _installations_root() -> Path:
    return _base_path() / "installations"


def _installation_path(installation_id: str) -> Path:
    return _installations_root() / installation_id


def _ensure_install_root() -> None:
    base = _base_path()
    root = base / "installations"
    for directory in (base, root):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            try:
                status = os.lstat(os.fspath(directory))
            except OSError as error:
                raise InstallationError(
                    "INSTALLATION_UNSAFE",
                    "installation root is unavailable") from error
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise InstallationError(
                    "INSTALLATION_UNSAFE",
                    "installation root is not a safe directory")


def _open_installation_dir(installation_id: str, *, create: bool):
    root = _installations_root()
    target = root / installation_id
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    if create:
        _ensure_install_root()
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(os.fspath(target), flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallationError(
            "INSTALLATION_UNSAFE",
            "installation directory is a link or not a directory") from error
    try:
        status = os.fstat(descriptor)
        if status.st_uid != os.getuid():
            raise InstallationError(
                "INSTALLATION_PERMISSIONS",
                "installation directory is owned by another user")
        if status.st_mode & 0o077:
            raise InstallationError(
                "INSTALLATION_PERMISSIONS",
                "installation directory must not be group/other accessible")
    except InstallationError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise InstallationError(
            "INSTALLATION_UNSAFE",
            "installation directory could not be verified") from error
    return descriptor


def _read_control(directory: int, name: str, max_bytes: int) -> Optional[bytes]:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallationError(
            "INSTALLATION_UNSAFE",
            "control file is not a safe regular file") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise InstallationError(
                "INSTALLATION_UNSAFE", "control file is not a regular file")
        if status.st_uid != os.getuid():
            raise InstallationError(
                "INSTALLATION_PERMISSIONS",
                "control file is owned by another user")
        if status.st_mode & 0o077:
            raise InstallationError(
                "INSTALLATION_PERMISSIONS",
                "control file must not be group/other accessible")
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > max_bytes:
            raise InstallationError(
                "CONTROL_TOO_LARGE", "control file exceeds 64 KiB")
        return data
    finally:
        os.close(descriptor)


def _read_external(path: Path, max_bytes: int) -> Optional[bytes]:
    path = Path(path)
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        directory = os.open(os.fspath(path.parent), flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallationError(
            "INSTALLATION_UNSAFE",
            "installation directory is not a safe directory") from error
    try:
        status = os.fstat(directory)
        if status.st_uid != os.getuid() or status.st_mode & 0o077:
            raise InstallationError(
                "INSTALLATION_PERMISSIONS",
                "installation directory permissions are unsafe")
        return _read_control(directory, path.name, max_bytes)
    finally:
        os.close(directory)


def _write_control(directory: int, name: str, payload: bytes,
                   mode: int = 0o600) -> None:
    try:
        status = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise InstallationError(
            "INSTALLATION_UNSAFE",
            "control path could not be verified") from error
    else:
        if not stat.S_ISREG(status.st_mode):
            raise InstallationError(
                "INSTALLATION_UNSAFE",
                "control path must be a regular file")
    temporary = ".{}-{}.tmp".format(name, secrets.token_hex(8))
    flags = (os.O_CREAT | os.O_EXCL | os.O_WRONLY
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(temporary, flags, mode, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InstallationError(
                "INSTALLATION_UNSAFE", "temporary path is not a regular file")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except InstallationError:
        raise
    except OSError as error:
        raise InstallationError(
            "INSTALLATION_WRITE_FAILED",
            "control file write failed") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _unlink_control(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise InstallationError(
            "INSTALLATION_WRITE_FAILED",
            "control file cleanup failed") from error


@contextlib.contextmanager
def _installation_lock(installation_id: str):
    directory = _open_installation_dir(installation_id, create=True)
    assert directory is not None
    lock = None
    try:
        lock_flags = (os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
                      | getattr(os, "O_NOFOLLOW", 0))
        try:
            lock = os.open("lock", lock_flags | os.O_CREAT | os.O_EXCL,
                           0o600, dir_fd=directory)
        except FileExistsError:
            lock = os.open("lock", lock_flags, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise InstallationError(
                "INSTALLATION_UNSAFE", "installation lock is not a regular file")
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= 5.0:
                    raise InstallationError(
                        "INSTALLATION_BUSY", "installation lock timed out")
                time.sleep(0.05)
        yield directory
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock)
        os.close(directory)


# ---------------------------------------------------------------------------
# Repository configuration: <repo>/.codex/hooks.json
# ---------------------------------------------------------------------------

def _read_optional_file(root: Path, relative: str,
                        max_bytes: int) -> Optional[bytes]:
    parts = PurePosixPath(relative).parts
    if (not parts or PurePosixPath(relative).is_absolute()
            or ".." in parts):
        raise InstallationError(
            "INSTALLATION_UNSAFE_PATH", "path must stay inside the repository")
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(os.fspath(root), directory_flags)
    except OSError as error:
        raise InstallationError(
            "REPOSITORY_ERROR", "repository root is unavailable") from error
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise InstallationError(
                    "CONFIG_UNSAFE",
                    "configuration directory is a link or not a directory") from error
            os.close(descriptor)
            descriptor = child
        try:
            file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise InstallationError(
                "CONFIG_UNSAFE",
                "configuration path is a link or not a regular file") from error
        try:
            status = os.fstat(file_descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise InstallationError(
                    "CONFIG_UNSAFE", "configuration path is not a regular file")
            data = b""
            while len(data) <= max_bytes:
                chunk = os.read(file_descriptor,
                                min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > max_bytes:
                raise InstallationError(
                    "CONFIG_TOO_LARGE", "configuration file exceeds 64 KiB")
            return data
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _open_repo_directory(repo_root: Path) -> int:
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        return os.open(os.fspath(repo_root), flags)
    except OSError as error:
        raise InstallationError(
            "REPOSITORY_ERROR", "repository root is unavailable") from error


def _open_child_directory(repo_root: Path, name: str, *,
                          create: bool) -> Optional[int]:
    parent = _open_repo_directory(repo_root)
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if create:
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            child = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise InstallationError(
                "CONFIG_UNSAFE",
                "configuration directory is a link or not a directory") from error
        status = os.fstat(child)
        if not stat.S_ISDIR(status.st_mode):
            os.close(child)
            raise InstallationError(
                "CONFIG_UNSAFE",
                "configuration directory is not a directory")
        return child
    finally:
        os.close(parent)


def _read_config_object(repo_root: Path) -> Tuple[bool, Optional[Dict]]:
    raw = _read_optional_file(repo_root, CONFIG_REL, CONTROL_MAX_BYTES)
    if raw is None:
        return False, None
    try:
        obj = json.loads(raw.decode("utf-8"),
                         object_pairs_hook=_reject_duplicates)
    except UnicodeDecodeError:
        raise InstallationError("CONFIG_INVALID", "hooks.json is not valid UTF-8")
    except json.JSONDecodeError:
        raise InstallationError("CONFIG_INVALID", "hooks.json is not valid JSON")
    except _DuplicateKey:
        raise InstallationError("CONFIG_INVALID", "hooks.json has a duplicate key")
    if not isinstance(obj, dict):
        raise InstallationError(
            "CONFIG_INVALID", "hooks.json root must be an object")
    _validate_hooks_shape(obj)
    return True, obj


def _validate_hooks_shape(obj: Dict) -> None:
    if "hooks" not in obj:
        return
    hooks = obj["hooks"]
    if not isinstance(hooks, dict):
        raise InstallationError(
            "CONFIG_INVALID", "hooks.json hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise InstallationError(
                "CONFIG_INVALID", "hooks.json event entries must be lists")
        for group in groups:
            if not isinstance(group, dict):
                raise InstallationError(
                    "CONFIG_INVALID", "hooks.json matcher groups must be objects")
            entries = group.get("hooks")
            if not isinstance(entries, list):
                raise InstallationError(
                    "CONFIG_INVALID", "hooks.json matcher groups need a hooks list")
            for hook in entries:
                if not isinstance(hook, dict):
                    raise InstallationError(
                        "CONFIG_INVALID", "hooks.json hook entries must be objects")


def _iter_hooks(obj: Optional[Dict]):
    if not obj:
        return
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            for hook_index, hook in enumerate(entries):
                yield event, group_index, hook_index, hook


def _owned_locations(obj: Optional[Dict], command: str) -> List[Tuple[str, int, int]]:
    return [(event, group_index, hook_index)
            for event, group_index, hook_index, hook in _iter_hooks(obj)
            if hook.get("command") == command]


def _foreign_spellguard_commands(obj: Optional[Dict], command: str) -> List[str]:
    foreign = []
    for _event, _group, _index, hook in _iter_hooks(obj):
        candidate = hook.get("command")
        if not isinstance(candidate, str) or candidate == command:
            continue
        lowered = candidate.lower()
        if "spellguard" in lowered or "repair_window" in lowered:
            foreign.append(candidate)
    return foreign


def _duplicate_owned_events(locations: List[Tuple[str, int, int]]) -> List[str]:
    seen: Dict[str, int] = {}
    for event, _group, _index in locations:
        seen[event] = seen.get(event, 0) + 1
    return sorted(event for event, count in seen.items() if count > 1)


def _append_owned(obj: Optional[Dict], command: str, event: str) -> Dict:
    post = copy.deepcopy(obj) if obj is not None else {}
    hooks = post.setdefault("hooks", {})
    hooks.setdefault(event, [])
    hooks[event].append({"hooks": [{
        "type": "command",
        "command": command,
        "timeout": HOOK_TIMEOUT,
    }]})
    return post


def _remove_owned(obj: Dict, command: str) -> Dict:
    post = copy.deepcopy(obj)
    hooks = post.get("hooks")
    if not isinstance(hooks, dict):
        return post
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        remaining_groups = []
        for group in groups:
            if not isinstance(group, dict):
                remaining_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                remaining_groups.append(group)
                continue
            kept = [hook for hook in entries
                    if not (isinstance(hook, dict)
                            and hook.get("command") == command)]
            if len(kept) == len(entries):
                remaining_groups.append(group)
                continue
            if kept:
                updated = dict(group)
                updated["hooks"] = kept
                remaining_groups.append(updated)
        hooks[event] = remaining_groups
    return post


def _config_is_empty(obj: Optional[Dict]) -> bool:
    if obj is None:
        return True
    if set(obj) - {"hooks"}:
        return False
    hooks = obj.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    return all(not (isinstance(groups, list) and groups)
               for groups in hooks.values())


def _write_config_object(repo_root: Path, obj: Dict) -> None:
    payload = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > CONTROL_MAX_BYTES:
        raise InstallationError(
            "CONFIG_TOO_LARGE", "resulting hooks.json exceeds 64 KiB")
    directory = _open_child_directory(repo_root, ".codex", create=True)
    assert directory is not None
    try:
        try:
            status = os.stat("hooks.json", dir_fd=directory,
                             follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(status.st_mode):
                raise InstallationError(
                    "CONFIG_UNSAFE", "hooks.json must be a regular file")
        _write_control(directory, "hooks.json", payload, mode=0o644)
    finally:
        os.close(directory)


def _delete_config_object(repo_root: Path) -> None:
    directory = _open_child_directory(repo_root, ".codex", create=False)
    if directory is None:
        return
    try:
        try:
            status = os.stat("hooks.json", dir_fd=directory,
                             follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(status.st_mode):
            raise InstallationError(
                "CONFIG_UNSAFE", "hooks.json must be a regular file")
        os.unlink("hooks.json", dir_fd=directory)
    finally:
        os.close(directory)


def _toml_conflict(repo_root: Path) -> Optional[str]:
    raw = _read_optional_file(repo_root, TOML_REL, CONTROL_MAX_BYTES)
    if raw is None:
        return None
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python >= 3.11 here
        text = raw.decode("utf-8", errors="replace")
        if re.search(r"(?m)^\s*\[?hooks\]?\s*=", text) or re.search(
                r"(?m)^\s*\[hooks", text):
            return "config.toml may already define hooks"
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "config.toml could not be parsed; refusing to guess its hooks"
    if isinstance(data, dict) and "hooks" in data:
        return ("config.toml already defines hooks; refusing to add a second "
                "hook source")
    return None


# ---------------------------------------------------------------------------
# Registry snapshot
# ---------------------------------------------------------------------------

def _registry_snapshot(repo_root: Path) -> Dict:
    try:
        raw = _read_optional_file(repo_root, REGISTRY_REL, REGISTRY_MAX_BYTES)
    except InstallationError:
        return {"state": "unreadable", "digest": None, "rules": []}
    if raw is None:
        return {"state": "absent", "digest": None, "rules": []}
    try:
        registry = parse_registry(raw)
    except RegistryError:
        return {"state": "unreadable", "digest": None, "rules": []}
    rules = [{
        "id": rule.id,
        "path": rule.protected_symbol.path,
        "symbol": rule.protected_symbol.symbol,
        "source_root": rule.protected_symbol.source_root,
        "lifecycle": rule.lifecycle,
        "reason": rule.reason,
        "desired_state": rule.desired_state,
    } for rule in registry.rules]
    return {"state": "present", "digest": registry.digest, "rules": rules}


def _refine_registry_state(snapshot: Dict, record: Optional[Dict]) -> str:
    if snapshot["state"] in ("absent", "unreadable"):
        return snapshot["state"]
    adopted = (record or {}).get("adopted_registry_digest")
    if not adopted:
        return "unbound"
    return "bound" if adopted == snapshot["digest"] else "drifted"


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------

def _blob(obj: Optional[Dict]) -> Dict:
    return {"present": obj is not None, "value": obj}


def _blob_matches(blob: Dict, current: Optional[Dict]) -> bool:
    return bool(blob.get("present")) == (current is not None) and blob.get("value") == current


def _plan_payload(plan: Dict) -> Dict:
    return {key: value for key, value in plan.items()
            if key not in ("plan_digest", "diagnostics")}


def _build_plan(root: Path, *, remove: bool) -> Dict:
    repo_root = repository_root(Path(root))
    installation_id, repo_root, common = _identity(repo_root)
    conflict = _toml_conflict(repo_root)
    if conflict is not None:
        raise InstallationError("CONFIG_TOML_CONFLICT", conflict)
    entry = resolve_entry()
    command = _hook_command(entry, installation_id)
    exists, obj = _read_config_object(repo_root)
    foreign = _foreign_spellguard_commands(obj, command)
    record = _load_record_optional(installation_id)
    registry = _registry_snapshot(repo_root)
    if not remove and registry["state"] == "unreadable":
        # A registry that exists but cannot be read must never be adopted as
        # "no registry"; otherwise the Hook would stay silent on a real fault.
        raise InstallationError(
            "REGISTRY_READ_FAILED",
            "the existing registry could not be read; refusing to treat it as "
            "absent")

    plan: Dict[str, Any] = {
        "schema_version": INSTALLATION_SCHEMA,
        "command": "setup",
        "operation": "remove" if remove else "install",
        "installation_id": installation_id,
        "repository_root": str(repo_root),
        "git_common_dir": str(common),
        "host": "codex",
        "config_path": CONFIG_REL,
        "entry": str(entry.path),
        "hook_command": command,
        "timeout": HOOK_TIMEOUT,
        "changes": [],
        "config_preimage": _blob(obj),
        "config_postimage": _blob(obj),
        "created_config": bool(record["created_config"]) if record else not exists,
        "requires_host_trust": True,
        "adopted_rules": registry["rules"],
    }

    if foreign:
        raise InstallationError(
            "LEGACY_HOOK_CONFLICT",
            "hooks.json contains a pre-existing Spellguard hook entry; "
            "remove it manually before setup")
    if obj is not None:
        duplicates = _duplicate_owned_events(_owned_locations(obj, command))
        if duplicates:
            raise InstallationError(
                "HOOK_DUPLICATE_CONFLICT",
                "hooks.json already contains more than one entry for this "
                "installation")

    if remove:
        _plan_remove(plan, obj, exists, record, command)
    else:
        _plan_install(plan, obj, exists, record, command)

    plan["registry_state"] = _refine_registry_state(registry, record)
    plan["registry_digest"] = registry["digest"]
    plan["diagnostics"] = []
    plan["plan_digest"] = _sha256_text(_canonical(_plan_payload(plan)))
    return plan


def _plan_install(plan: Dict, obj: Optional[Dict], exists: bool,
                  record: Optional[Dict], command: str) -> None:
    changes: List[Dict] = []
    owned_events = {event for event, _g, _h in _owned_locations(obj, command)}
    if owned_events and owned_events != set(EVENTS):
        raise InstallationError(
            "HOOK_OWNERSHIP_CONFLICT",
            "only part of this installation's hook entries are present; "
            "refusing to guess ownership")
    if not exists:
        changes.append({"action": "create_file", "file": CONFIG_REL})
    post = obj
    for event in EVENTS:
        if event in owned_events:
            continue
        post = _append_owned(post, command, event)
        changes.append({"action": "add_hook", "file": CONFIG_REL,
                        "event": event, "command": command,
                        "timeout": HOOK_TIMEOUT})
    if record is None or record.get("lifecycle") != "active":
        changes.append({"action": "write_manifest", "file": RECORD_NAME})
    plan["changes"] = changes
    plan["config_postimage"] = _blob(post)


def _plan_remove(plan: Dict, obj: Optional[Dict], exists: bool,
                 record: Optional[Dict], command: str) -> None:
    changes: List[Dict] = []
    if not exists:
        if record is not None and record.get("lifecycle") == "active":
            changes.append({"action": "disable_manifest", "file": RECORD_NAME})
        plan["changes"] = changes
        plan["config_postimage"] = _blob(None)
        return
    owned = _owned_locations(obj, command)
    events = sorted({event for event, _g, _h in owned})
    post: Optional[Dict] = obj
    if owned:
        post = _remove_owned(obj, command)
        for event in events:
            changes.append({"action": "remove_hook", "file": CONFIG_REL,
                            "event": event})
    created = bool(record["created_config"]) if record else False
    if _config_is_empty(post) and created:
        post = None
        changes.append({"action": "delete_file", "file": CONFIG_REL})
    if record is not None and record.get("lifecycle") == "active":
        changes.append({"action": "disable_manifest", "file": RECORD_NAME})
    plan["changes"] = changes
    plan["config_postimage"] = _blob(post)


def plan_setup(root: Path, *, remove: bool = False) -> Dict:
    """Render the exact install/remove plan without any side effect."""
    return _build_plan(Path(root), remove=remove)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _parse_record(raw: bytes) -> Dict:
    try:
        record = json.loads(raw.decode("utf-8"),
                            object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey):
        raise InstallationError(
            "INSTALLATION_INVALID", "installation record is not valid JSON")
    if not isinstance(record, dict):
        raise InstallationError(
            "INSTALLATION_INVALID", "installation record must be an object")
    unknown = set(record) - RECORD_FIELDS
    if unknown:
        raise InstallationError(
            "INSTALLATION_INVALID",
            "installation record has an unsupported field")
    missing = RECORD_FIELDS - set(record)
    if missing:
        raise InstallationError(
            "INSTALLATION_INVALID", "installation record is incomplete")
    if record["schema_version"] != INSTALLATION_SCHEMA:
        raise InstallationError(
            "INSTALLATION_INVALID", "unsupported installation schema")
    if not _DIGEST.match(str(record["installation_id"])):
        raise InstallationError(
            "INSTALLATION_INVALID", "invalid installation id")
    if record["lifecycle"] not in ("active", "disabled"):
        raise InstallationError(
            "INSTALLATION_INVALID", "invalid installation lifecycle")
    if not isinstance(record["created_config"], bool):
        raise InstallationError(
            "INSTALLATION_INVALID", "invalid created_config flag")
    return record


def _load_record_optional(installation_id: str) -> Optional[Dict]:
    if not _DIGEST.match(installation_id or ""):
        raise InstallationError(
            "INSTALLATION_ID", "invalid installation id")
    raw = _read_external(_installation_path(installation_id) / RECORD_NAME,
                         CONTROL_MAX_BYTES)
    if raw is None:
        return None
    return _parse_record(raw)


def _read_control_from_dir(directory: int, name: str) -> Optional[bytes]:
    return _read_control(directory, name, CONTROL_MAX_BYTES)


def _read_record_from_dir(directory: int) -> Optional[Dict]:
    raw = _read_control_from_dir(directory, RECORD_NAME)
    if raw is None:
        return None
    return _parse_record(raw)


def load_installation(root: Path, installation_id: str) -> Dict:
    """Load the external record for this repository and installation."""
    if not _DIGEST.match(installation_id or ""):
        raise InstallationError(
            "INSTALLATION_ID", "invalid installation id")
    repo_root = repository_root(Path(root))
    expected, canonical_root, _common = _identity(repo_root)
    if expected != installation_id:
        raise InstallationError(
            "INSTALLATION_UNKNOWN",
            "installation id does not belong to this repository")
    record = _load_record_optional(installation_id)
    if record is None:
        raise InstallationError(
            "INSTALLATION_NOT_FOUND", "no installation record exists")
    if record["repository_root"] != str(canonical_root):
        raise InstallationError(
            "INSTALLATION_MOVED",
            "the repository moved; run setup again from the new location")
    return record


def _commit_record(directory: int, plan: Dict, repo_root: Path) -> None:
    existing = None
    raw = _read_control_from_dir(directory, RECORD_NAME)
    if raw is not None:
        existing = _parse_record(raw)
    if plan["operation"] == "remove" and existing is None:
        return
    adopted = plan["registry_digest"]
    if plan["operation"] == "remove" and existing is not None:
        adopted = existing["adopted_registry_digest"]
    created_at = existing["created_at"] if existing else _now()
    record = {
        "schema_version": INSTALLATION_SCHEMA,
        "installation_id": plan["installation_id"],
        "repository_root": plan["repository_root"],
        "git_common_dir": plan["git_common_dir"],
        "host": plan["host"],
        "config_rel": plan["config_path"],
        "entry": plan["entry"],
        "hook_command": plan["hook_command"],
        "timeout": plan["timeout"],
        "created_config": bool(plan["created_config"]),
        "lifecycle": "active" if plan["operation"] == "install" else "disabled",
        "adopted_registry_digest": adopted,
        "registry_state_at_install": plan["registry_state"],
        "applied_plan_digest": plan["plan_digest"],
        "applied_operation": plan["operation"],
        "created_at": created_at,
        "updated_at": _now(),
    }
    payload = (json.dumps(record, ensure_ascii=True, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    _write_control(directory, RECORD_NAME, payload, mode=0o600)


# ---------------------------------------------------------------------------
# Pending records and execution
# ---------------------------------------------------------------------------

def _write_pending(directory: int, plan: Dict) -> None:
    payload = {"schema_version": INSTALLATION_SCHEMA,
               "plan_digest": plan["plan_digest"],
               "operation": plan["operation"],
               "plan": plan}
    data = (json.dumps(payload, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    _write_control(directory, PENDING_NAME, data, mode=0o600)


def _read_pending(directory: int) -> Optional[Dict]:
    raw = _read_control_from_dir(directory, PENDING_NAME)
    if raw is None:
        return None
    try:
        pending = json.loads(raw.decode("utf-8"),
                             object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey):
        raise InstallationError(
            "INSTALLATION_INVALID", "pending setup record is not valid JSON")
    if (not isinstance(pending, dict)
            or pending.get("schema_version") != INSTALLATION_SCHEMA
            or not isinstance(pending.get("plan"), dict)
            or pending.get("plan_digest") != pending["plan"].get("plan_digest")
            or pending.get("operation") != pending["plan"].get("operation")):
        raise InstallationError(
            "INSTALLATION_INVALID", "pending setup record has an unexpected shape")
    return pending


def _clear_pending(directory: int) -> None:
    _unlink_control(directory, PENDING_NAME)


def _apply_config_object(repo_root: Path, blob: Dict) -> None:
    if blob.get("present"):
        _write_config_object(repo_root, blob["value"])
    else:
        _delete_config_object(repo_root)


def _execute_setup(directory: int, plan: Dict, repo_root: Path) -> None:
    _apply_config_object(repo_root, plan["config_postimage"])
    _commit_record(directory, plan, repo_root)
    _clear_pending(directory)


def _resume_setup(directory: int, pending: Dict, repo_root: Path) -> Dict:
    plan = pending["plan"]
    exists, obj = _read_config_object(repo_root)
    del exists
    pre = plan["config_preimage"]
    post = plan["config_postimage"]
    if _blob_matches(pre, obj):
        _apply_config_object(repo_root, post)
    elif not _blob_matches(post, obj):
        raise InstallationError(
            "SETUP_CONFLICT",
            "host configuration changed to unrecognized content; refusing to "
            "overwrite it")
    _commit_record(directory, plan, repo_root)
    _clear_pending(directory)
    return _result(plan, already=False)


def _result(plan: Dict, *, already: bool) -> Dict:
    return {
        "schema_version": INSTALLATION_SCHEMA,
        "command": "setup",
        "operation": plan["operation"],
        "installation_id": plan["installation_id"],
        "plan_digest": plan["plan_digest"],
        "applied": True,
        "already_applied": already,
        "changes": plan["changes"],
        "config_path": plan["config_path"],
        "config_present": bool(plan["config_postimage"].get("present")),
        "lifecycle": "active" if plan["operation"] == "install" else "disabled",
        "registry_state": plan["registry_state"],
        "registry_digest": plan["registry_digest"],
        "requires_host_trust": True,
        "host_verified": False,
        "diagnostics": [],
    }


def _already_result(record: Dict, digest: str, repo_root: Path) -> Dict:
    registry = _registry_snapshot(repo_root)
    return {
        "schema_version": INSTALLATION_SCHEMA,
        "command": "setup",
        "operation": record.get("applied_operation") or "install",
        "installation_id": record["installation_id"],
        "plan_digest": digest,
        "applied": True,
        "already_applied": True,
        "changes": [],
        "config_path": record["config_rel"],
        "config_present": _read_config_object(repo_root)[0],
        "lifecycle": record["lifecycle"],
        "registry_state": _refine_registry_state(registry, record),
        "registry_digest": registry["digest"],
        "requires_host_trust": True,
        "host_verified": False,
        "diagnostics": [],
    }


def apply_setup(root: Path, expected_plan_digest: str) -> Dict:
    """Execute exactly the plan the caller previewed."""
    if not _DIGEST.match(expected_plan_digest or ""):
        raise InstallationError(
            "SETUP_PLAN_INVALID", "plan digest must be 64 hex characters")
    repo_root = repository_root(Path(root))
    installation_id, repo_root, _common = _identity(repo_root)
    record = _load_record_optional(installation_id)
    if record is not None and record.get("applied_plan_digest") == expected_plan_digest:
        return _already_result(record, expected_plan_digest, repo_root)
    with _installation_lock(installation_id) as directory:
        pending = _read_pending(directory)
        if pending is not None:
            if pending["plan_digest"] != expected_plan_digest:
                raise InstallationError(
                    "SETUP_BUSY",
                    "a different setup operation is pending; finish or clear it "
                    "before applying a new plan")
            return _resume_setup(directory, pending, repo_root)
        # A competing process may have committed this exact plan while we
        # waited for the lock; never repeat an authorized operation.
        committed = _read_record_from_dir(directory)
        if committed is not None and (
                committed.get("applied_plan_digest") == expected_plan_digest):
            return _already_result(committed, expected_plan_digest, repo_root)
        install_error = None
        try:
            install_plan = _build_plan(repo_root, remove=False)
        except InstallationError as error:
            # An unreadable registry blocks install but must not block removal.
            install_plan = None
            install_error = error
        remove_plan = _build_plan(repo_root, remove=True)
        if install_plan is not None and (
                expected_plan_digest == install_plan["plan_digest"]):
            plan = install_plan
        elif expected_plan_digest == remove_plan["plan_digest"]:
            plan = remove_plan
        elif install_error is not None:
            raise install_error
        else:
            raise InstallationError(
                "SETUP_PLAN_CHANGED",
                "the host configuration or registry changed since the preview; "
                "preview again and re-confirm")
        _write_pending(directory, plan)
        _execute_setup(directory, plan, repo_root)
        return _result(plan, already=False)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def installation_status(root: Path) -> Dict:
    repo_root = repository_root(Path(root))
    installation_id, canonical_root, _common = _identity(repo_root)
    registry = _registry_snapshot(canonical_root)
    status: Dict[str, Any] = {
        "schema_version": INSTALLATION_SCHEMA,
        "command": "status",
        "installation_id": installation_id,
        "repository_root": str(canonical_root),
        "host": "codex",
        "config_path": CONFIG_REL,
        "configured": False,
        "host_verified": False,
        "lifecycle": None,
        "registry_state": _refine_registry_state(registry, None),
        "registry_digest": registry["digest"],
        "confirmation_pending": False,
        "last_check": None,
        "diagnostics": [],
    }
    try:
        record = _load_record_optional(installation_id)
    except InstallationError as error:
        status["diagnostics"].append(
            {"code": error.code, "message": error.message})
        return status
    if record is None:
        return status
    status["lifecycle"] = record["lifecycle"]
    status["registry_state"] = _refine_registry_state(registry, record)
    try:
        pending = _read_external(
            _installation_path(installation_id) / CONFIRMATION_PENDING_NAME,
            CONTROL_MAX_BYTES)
    except InstallationError as error:
        status["confirmation_pending"] = True
        status["diagnostics"].append(
            {"code": error.code, "message": error.message})
    else:
        status["confirmation_pending"] = pending is not None
    try:
        exists, obj = _read_config_object(canonical_root)
    except InstallationError as error:
        status["diagnostics"].append(
            {"code": error.code, "message": error.message})
        return status
    del exists
    command = record["hook_command"]
    owned_events = {event for event, _g, _h
                    in _owned_locations(obj, command)}
    status["configured"] = (
        record["lifecycle"] == "active" and owned_events == set(EVENTS))
    foreign = _foreign_spellguard_commands(obj, command)
    if foreign:
        status["diagnostics"].append({
            "code": "LEGACY_HOOK_CONFLICT",
            "message": "hooks.json contains a different Spellguard hook entry",
        })
    return status


__all__ = [
    "InstallationError",
    "plan_setup",
    "apply_setup",
    "load_installation",
    "installation_status",
    "installation_identity",
]
