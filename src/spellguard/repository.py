"""Read-only collection of a Git working tree into a stable snapshot."""

import hashlib
import io
import os
import subprocess
import tokenize
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from stat import S_ISLNK, S_ISREG
from typing import Dict, List, Optional, Sequence, Tuple

from .models import ChangeSet, CollectedSnapshot, Coverage, Diagnostic, Snapshot, SourceFile


class RepositoryError(RuntimeError):
    """Raised when the requested path is not a readable Git work tree."""


@dataclass(frozen=True)
class _Collection:
    files: Tuple[SourceFile, ...]
    coverage: Coverage
    inventory: Tuple[Tuple[str, str], ...]


def _run_git(root: Path, arguments: Sequence[str]) -> bytes:
    command = ["git", "-C", os.fspath(root)] + list(arguments)
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise RepositoryError("could not start Git: {}".format(error)) from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RepositoryError(message or "Git command failed")
    return completed.stdout


def _repository_root(root: Path) -> Path:
    candidate = Path(root).expanduser().resolve()
    if not candidate.is_dir():
        raise RepositoryError("repository path is not a directory: {}".format(candidate))
    raw_top_level = _run_git(candidate, ["rev-parse", "--show-toplevel"])
    top_level = Path(os.fsdecode(raw_top_level).strip()).resolve()
    if not top_level.is_dir():
        raise RepositoryError("Git top-level is not a directory: {}".format(top_level))
    return top_level


def repository_root(root: Path) -> Path:
    """Return the real Git work-tree root for a path inside the repository."""

    return _repository_root(root)


def git_dir(root: Path) -> Path:
    """Return the resolved Git metadata directory for the repository.

    Phase-0 marking keeps agent TEMP proposals inside this directory so they
    never enter the work tree, the index, or exported snapshots. Works for
    regular .git directories; nested worktrees are explicitly out of scope
    for this entry (linked worktrees are an unsupported Phase-0 boundary).
    """
    candidate = Path(root).expanduser().resolve()
    raw = _run_git(candidate, ["rev-parse", "--absolute-git-dir"])
    resolved = Path(os.fsdecode(raw).strip()).resolve()
    if not resolved.is_dir():
        raise RepositoryError(
            "Git metadata directory is not a directory: {}".format(resolved))
    return resolved


SUPPORTED_SUFFIXES = (".py", ".go", ".js", ".jsx", ".mjs",
                      ".ts", ".tsx", ".mts", ".java", ".c", ".h",
                      ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")


def _listed_paths(root: Path) -> Tuple[str, ...]:
    raw = _run_git(
        root,
        [
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--full-name",
            "--deduplicate",
        ],
    )
    paths = []
    for raw_path in raw.split(b"\0"):
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        pure_path = PurePosixPath(path)
        if pure_path.is_absolute() or ".." in pure_path.parts:
            raise RepositoryError("Git returned an unsafe path: {}".format(path))
        paths.append(path)
    return tuple(sorted(set(paths)))


def _read_bytes(path: Path) -> bytes:
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_flags = file_flags | getattr(os, "O_DIRECTORY", 0)
    parts = path.parts
    if path.is_absolute():
        descriptor = os.open(path.anchor, directory_flags)
        components = parts[1:]
    else:
        descriptor = os.open(".", directory_flags)
        components = parts
    for component in components[:-1]:
        next_descriptor = os.open(
            component,
            directory_flags,
            dir_fd=descriptor,
        )
        os.close(descriptor)
        descriptor = next_descriptor
    file_descriptor = os.open(
        components[-1],
        file_flags,
        dir_fd=descriptor,
    )
    os.close(descriptor)
    descriptor = file_descriptor
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def read_repository_file(root: Path, relative_path: str, max_bytes: int) -> bytes:
    """Read one file inside the repository, refusing links, traversal and
    oversized content, without reading unbounded input first."""
    from .registry import RegistryError

    if not isinstance(relative_path, str) or not relative_path:
        raise RegistryError("REGISTRY_READ_FAILED", "path must be a non-empty string")
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RegistryError("REGISTRY_READ_FAILED", "path must stay inside the repository")
    if candidate.parts and (candidate.parts[0] == "/" or "\\" in relative_path):
        raise RegistryError("REGISTRY_READ_FAILED", "path must be POSIX relative")
    target = root.joinpath(*candidate.parts)
    limit = int(max_bytes)
    if limit <= 0:
        raise RegistryError("REGISTRY_READ_FAILED", "max_bytes must be positive")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(os.fspath(root), directory_flags)
    try:
        for component in candidate.parts[:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(candidate.parts[-1], flags, dir_fd=descriptor)
    except OSError:
        descriptor_ended = descriptor
        try:
            os.close(descriptor_ended)
        except OSError:
            pass
        raise RegistryError("REGISTRY_READ_FAILED", "file could not be opened")
    os.close(descriptor)
    descriptor = -1
    file_stream = os.fdopen(file_descriptor, "rb")
    try:
        data = file_stream.read(limit + 1)
    except OSError:
        file_stream.close()
        raise RegistryError("REGISTRY_READ_FAILED", "file could not be read")
    finally:
        file_stream.close()
    if len(data) > limit:
        raise RegistryError("REGISTRY_READ_FAILED", "file exceeds max_bytes")
    try:
        mode = os.lstat(os.fspath(target)).st_mode
        if not S_ISREG(mode):
            raise RegistryError("REGISTRY_READ_FAILED", "path is not a regular file")
    except OSError:
        raise RegistryError("REGISTRY_READ_FAILED", "file could not be stat-ed")
    return data


def _symlink_parent(root: Path, relative_path: PurePosixPath) -> Optional[str]:
    current = root
    for part in relative_path.parts[:-1]:
        current = current / part
        try:
            if S_ISLNK(os.lstat(os.fspath(current)).st_mode):
                return str(PurePosixPath(part))
        except OSError:
            return None
    return None


def _decode_python(source: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    return source.decode(encoding)


def _decode_source(path: str, source: bytes) -> str:
    return source.decode("utf-8") if path.endswith(".go") else _decode_python(source)


def _digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def _sort_diagnostics(diagnostics: Sequence[Diagnostic]) -> Tuple[Diagnostic, ...]:
    return tuple(sorted(diagnostics, key=lambda item: (item.path or "", item.code, item.message)))


def _coverage(
    eligible_files: int,
    analyzed_files: int,
    excluded_files: int,
    unsupported_files: int,
    failed_files: int,
    diagnostics: Sequence[Diagnostic],
    complete: bool,
) -> Coverage:
    return Coverage(
        eligible_files=eligible_files,
        analyzed_files=analyzed_files,
        excluded_files=excluded_files,
        unsupported_files=unsupported_files,
        failed_files=failed_files,
        diagnostics=_sort_diagnostics(diagnostics),
        complete=complete,
    )


def _collect_once(
    root: Path, paths: Sequence[str], ignore_missing_paths: Sequence[str] = (),
    *, include_go_metadata: bool = False,
) -> _Collection:
    files: List[SourceFile] = []
    diagnostics: List[Diagnostic] = []
    inventory: Dict[str, str] = {}
    eligible_files = 0
    analyzed_files = 0
    excluded_files = 0
    unsupported_files = 0
    failed_files = 0
    ignored_missing = set(ignore_missing_paths)

    for relative_path in paths:
        pure_path = PurePosixPath(relative_path)
        path = root.joinpath(*pure_path.parts)

        if ".git" in pure_path.parts:
            excluded_files += 1
            inventory[relative_path] = "excluded:git"
            diagnostics.append(
                Diagnostic("GIT_METADATA_EXCLUDED", "Git metadata path was excluded", relative_path)
            )
            continue

        symlink_parent = _symlink_parent(root, pure_path)
        if symlink_parent is not None:
            excluded_files += 1
            inventory[relative_path] = "excluded:symlink-parent"
            diagnostics.append(
                Diagnostic(
                    "SYMLINK_PARENT_EXCLUDED",
                    "A parent directory is a symbolic link; path was not read",
                    relative_path,
                )
            )
            continue

        try:
            status = os.lstat(os.fspath(path))
        except OSError as error:
            if relative_path in ignored_missing:
                inventory[relative_path] = "deleted:missing"
                continue
            if relative_path.endswith(SUPPORTED_SUFFIXES):
                eligible_files += 1
                failed_files += 1
                inventory[relative_path] = "failed:stat"
                diagnostics.append(
                    Diagnostic("FILE_STAT_FAILED", str(error), relative_path)
                )
            else:
                unsupported_files += 1
                inventory[relative_path] = "unsupported:stat"
            continue

        if S_ISLNK(status.st_mode):
            excluded_files += 1
            inventory[relative_path] = "excluded:symlink"
            diagnostics.append(
                Diagnostic("SYMLINK_EXCLUDED", "Symbolic link was not read", relative_path)
            )
            continue

        if not S_ISREG(status.st_mode):
            excluded_files += 1
            inventory[relative_path] = "excluded:non-regular"
            diagnostics.append(
                Diagnostic("NON_REGULAR_EXCLUDED", "Non-regular path was not read", relative_path)
            )
            continue

        if include_go_metadata and PurePosixPath(relative_path).name in (
                "go.mod", "go.work"):
            pass  # Go module metadata: collected as a snapshot file (G01)
        elif not relative_path.endswith(SUPPORTED_SUFFIXES):
            unsupported_files += 1
            inventory[relative_path] = "unsupported:extension"
            continue

        eligible_files += 1
        try:
            source = _read_bytes(path)
            content = _decode_source(relative_path, source)
        except (OSError, SyntaxError, UnicodeError, LookupError) as error:
            failed_files += 1
            inventory[relative_path] = "failed:read"
            diagnostics.append(Diagnostic("SOURCE_READ_FAILED", str(error), relative_path))
            continue

        digest = _digest(source)
        files.append(SourceFile(relative_path, content, digest))
        analyzed_files += 1
        inventory[relative_path] = "source:" + digest

    files.sort(key=lambda item: item.path)
    coverage = _coverage(
        eligible_files=eligible_files,
        analyzed_files=analyzed_files,
        excluded_files=excluded_files,
        unsupported_files=unsupported_files,
        failed_files=failed_files,
        diagnostics=diagnostics,
        complete=failed_files == 0,
    )
    return _Collection(tuple(files), coverage, tuple(sorted(inventory.items())))


def _snapshot_identity(source: str, files: Sequence[SourceFile]) -> str:
    digest = hashlib.sha256()
    digest.update(source.encode("utf-8"))
    for item in sorted(files, key=lambda value: value.path):
        digest.update(b"\0")
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.digest.encode("ascii"))
    return digest.hexdigest()


def _resolve_commit(root: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise RepositoryError("base commit reference is required")
    raw = _run_git(
        root,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            "{}^{{commit}}".format(ref),
        ],
    )
    commit = os.fsdecode(raw).strip()
    if not commit:
        raise RepositoryError("Git returned an empty commit identity")
    return commit


def _commit_tree(root: Path, commit: str) -> Tuple[Tuple[str, str, str], ...]:
    raw = _run_git(root, ["ls-tree", "-r", "-z", "--full-tree", commit])
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, entry_type, _object_id = header.split(b" ", 2)
        except ValueError as error:
            raise RepositoryError("Git returned a malformed tree entry") from error
        path = os.fsdecode(raw_path)
        pure_path = PurePosixPath(path)
        if pure_path.is_absolute() or ".." in pure_path.parts:
            raise RepositoryError("Git returned an unsafe path: {}".format(path))
        entries.append(
            (os.fsdecode(mode), os.fsdecode(entry_type), path)
        )
    return tuple(sorted(entries, key=lambda item: item[2]))


def _read_commit_bytes(root: Path, commit: str, relative_path: str) -> bytes:
    return _run_git(root, ["cat-file", "blob", "{}:{}".format(commit, relative_path)])


def _commit_diff(root: Path, commit: str) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    raw = _run_git(
        root,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            commit,
            "--",
        ],
    )
    records = raw.split(b"\0")
    changes = []
    index = 0
    while index < len(records):
        if not records[index]:
            index += 1
            continue
        status = os.fsdecode(records[index])
        index += 1
        path_count = 2 if status[:1] in ("R", "C") else 1
        if index + path_count > len(records):
            raise RepositoryError("Git returned an incomplete diff entry")
        paths = tuple(os.fsdecode(item) for item in records[index : index + path_count])
        index += path_count
        changes.append((status, paths))
    return tuple(changes)


def collect_change_set(
    root: Path, ref: str, target: Optional[CollectedSnapshot] = None
) -> ChangeSet:
    """Describe a fixed commit versus the current work tree without mutating it."""

    repository_root = _repository_root(Path(root))
    commit = _resolve_commit(repository_root, ref)
    base_entries = _commit_tree(repository_root, commit)
    base_paths = {path for _mode, _entry_type, path in base_entries}
    current_paths = set(_listed_paths(repository_root))
    added = set(current_paths - base_paths)
    deleted = set(base_paths - current_paths)
    modified = set()
    renames = []

    for status, paths in _commit_diff(repository_root, commit):
        kind = status[:1]
        if kind == "R":
            old_path, new_path = paths
            if old_path in deleted:
                deleted.remove(old_path)
            if new_path in added:
                added.remove(new_path)
            renames.append((old_path, new_path))
        elif kind == "M" or kind == "T":
            modified.add(paths[0])
        elif kind == "A":
            added.add(paths[0])
        elif kind == "D":
            deleted.add(paths[0])

    if len({old for old, _new in renames}) != len(renames):
        raise RepositoryError("Git returned duplicate rename sources")
    if len({new for _old, new in renames}) != len(renames):
        raise RepositoryError("Git returned duplicate rename destinations")

    return ChangeSet(
        base_commit=commit,
        target_snapshot_identity=(target.snapshot.identity if target else ""),
        added_paths=tuple(sorted(added)),
        modified_paths=tuple(sorted(modified)),
        deleted_paths=tuple(sorted(deleted)),
        renames=tuple(sorted(renames)),
    )


def collect_commit(root: Path, ref: str,
                   *, include_go_metadata: bool = False) -> CollectedSnapshot:
    """Collect a fixed Git commit without changing the working tree."""

    repository_root = _repository_root(Path(root))
    commit = _resolve_commit(repository_root, ref)
    entries = _commit_tree(repository_root, commit)
    files: List[SourceFile] = []
    diagnostics: List[Diagnostic] = []
    eligible_files = 0
    analyzed_files = 0
    excluded_files = 0
    unsupported_files = 0
    failed_files = 0

    for mode, entry_type, relative_path in entries:
        pure_path = PurePosixPath(relative_path)
        if ".git" in pure_path.parts:
            excluded_files += 1
            diagnostics.append(
                Diagnostic("GIT_METADATA_EXCLUDED", "Git metadata path was excluded", relative_path)
            )
            continue
        if entry_type != "blob" or mode == "120000":
            excluded_files += 1
            code = "SYMLINK_EXCLUDED" if mode == "120000" else "NON_REGULAR_EXCLUDED"
            message = "Symbolic link was not read" if mode == "120000" else "Non-regular path was not read"
            diagnostics.append(Diagnostic(code, message, relative_path))
            continue
        is_metadata = include_go_metadata and PurePosixPath(
            relative_path).name in ("go.mod", "go.work")
        if not relative_path.endswith(SUPPORTED_SUFFIXES) and not is_metadata:
            unsupported_files += 1
            continue

        eligible_files += 1
        source = _read_commit_bytes(repository_root, commit, relative_path)
        try:
            content = _decode_source(relative_path, source)
        except (SyntaxError, UnicodeError, LookupError) as error:
            failed_files += 1
            diagnostics.append(Diagnostic("SOURCE_READ_FAILED", str(error), relative_path))
            continue
        files.append(SourceFile(relative_path, content, _digest(source)))
        analyzed_files += 1

    coverage = _coverage(
        eligible_files=eligible_files,
        analyzed_files=analyzed_files,
        excluded_files=excluded_files,
        unsupported_files=unsupported_files,
        failed_files=failed_files,
        diagnostics=diagnostics,
        complete=failed_files == 0,
    )
    snapshot = Snapshot(
        identity=commit,
        source="commit:{}".format(commit),
        files=tuple(sorted(files, key=lambda item: item.path)),
    )
    return CollectedSnapshot(snapshot=snapshot, coverage=coverage)


def _with_unstable_diagnostic(collection: _Collection) -> _Collection:
    diagnostic = Diagnostic(
        "SNAPSHOT_UNSTABLE",
        "working tree content changed while collecting the snapshot",
    )
    coverage = replace(
        collection.coverage,
        diagnostics=_sort_diagnostics(collection.coverage.diagnostics + (diagnostic,)),
        complete=False,
    )
    return _Collection(collection.files, coverage, collection.inventory)


def collect_working_tree(
    root: Path, ignore_missing_paths: Sequence[str] = (),
    *, include_go_metadata: bool = False,
) -> CollectedSnapshot:
    """Collect readable Python and Go files from the current Git work tree.

    The collection is read-only. It performs a second inventory/read pass so a
    change during collection becomes an explicit unstable-snapshot diagnostic.
    """

    repository_root = _repository_root(Path(root))
    first_paths = _listed_paths(repository_root)
    first = _collect_once(repository_root, first_paths, ignore_missing_paths,
                          include_go_metadata=include_go_metadata)
    second_paths = _listed_paths(repository_root)
    second = _collect_once(repository_root, second_paths, ignore_missing_paths,
                           include_go_metadata=include_go_metadata)

    if first_paths != second_paths or first.inventory != second.inventory:
        result = _with_unstable_diagnostic(first)
    else:
        result = second

    snapshot = Snapshot(
        identity=(
            _snapshot_identity("working-tree", result.files)
            if result.coverage.complete and result.inventory == second.inventory
            else ""
        ),
        source="working-tree",
        files=result.files,
    )
    return CollectedSnapshot(snapshot=snapshot, coverage=result.coverage)
