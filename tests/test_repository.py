import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spellguard import repository
from spellguard.repository import (
    RepositoryError,
    collect_change_set,
    collect_commit,
    collect_working_tree,
)


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "spellguard-test@example.invalid")
        self._git("config", "user.name", "Spellguard Test")

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.root)] + list(arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _commit(self):
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def test_lists_tracked_untracked_and_non_ascii_paths_but_not_ignored(self):
        (self.root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        (self.root / "tracked file.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "中文.py").write_text("value = 2\n", encoding="utf-8")
        (self.root / "ignored.py").write_text("should_not_be_seen = True\n", encoding="utf-8")
        (self.root / "new file.py").write_text("value = 3\n", encoding="utf-8")
        (self.root / "README.txt").write_text("not Python\n", encoding="utf-8")
        self._commit()

        collected = collect_working_tree(self.root)

        self.assertEqual(
            [item.path for item in collected.snapshot.files],
            ["new file.py", "tracked file.py", "中文.py"],
        )
        self.assertEqual(collected.coverage.eligible_files, 3)
        self.assertEqual(collected.coverage.analyzed_files, 3)
        self.assertEqual(collected.coverage.unsupported_files, 2)
        self.assertTrue(collected.coverage.complete)
        self.assertNotIn("ignored.py", [item.path for item in collected.snapshot.files])

    def test_reads_latest_disk_content_after_staging(self):
        path = self.root / "staged.py"
        path.write_text("value = 'staged'\n", encoding="utf-8")
        self._commit()
        self._git("add", "staged.py")
        path.write_text("value = 'latest disk content'\n", encoding="utf-8")

        collected = collect_working_tree(self.root)

        self.assertEqual(collected.snapshot.files[0].content, "value = 'latest disk content'\n")

    def test_excludes_symlink_without_reading_target(self):
        outside = Path(self.tempdir.name) / "outside.py"
        marker = "outside = 'unchanged'\n"
        outside.write_text(marker, encoding="utf-8")
        execution_marker = Path(self.tempdir.name) / "execution-marker"
        (self.root / "would_execute.py").write_text(
            "from pathlib import Path\n"
            "Path({!r}).write_text('executed')\n".format(str(execution_marker)),
            encoding="utf-8",
        )
        link = self.root / "linked.py"
        link.symlink_to(outside)
        self._commit()

        collected = collect_working_tree(self.root)

        self.assertEqual(outside.read_text(encoding="utf-8"), marker)
        self.assertFalse(execution_marker.exists())
        self.assertNotIn("linked.py", [item.path for item in collected.snapshot.files])
        self.assertEqual(collected.coverage.excluded_files, 1)
        self.assertIn("SYMLINK_EXCLUDED", [item.code for item in collected.coverage.diagnostics])

    def test_excludes_parent_symlink_without_reading_outside_repository(self):
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        outside_file = outside / "inside.py"
        outside_file.write_text("outside = True\n", encoding="utf-8")
        package = self.root / "package"
        package.mkdir()
        (package / "inside.py").write_text("inside = True\n", encoding="utf-8")
        self._commit()

        shutil.rmtree(package)
        package.symlink_to(outside, target_is_directory=True)

        collected = collect_working_tree(self.root)

        self.assertEqual(outside_file.read_text(encoding="utf-8"), "outside = True\n")
        self.assertNotIn("package/inside.py", [item.path for item in collected.snapshot.files])
        self.assertEqual(collected.coverage.excluded_files, 2)
        self.assertIn(
            "SYMLINK_PARENT_EXCLUDED",
            [item.code for item in collected.coverage.diagnostics],
        )

    def test_repeated_collection_is_stable_and_change_during_collection_is_visible(self):
        path = self.root / "stable.py"
        path.write_text("value = 1\n", encoding="utf-8")
        self._commit()

        first = collect_working_tree(self.root)
        second = collect_working_tree(self.root)
        self.assertEqual(first.snapshot.identity, second.snapshot.identity)
        self.assertTrue(first.coverage.complete)
        self.assertEqual(
            first.coverage.eligible_files,
            first.coverage.analyzed_files + first.coverage.failed_files,
        )

        original_read = repository._read_bytes
        calls = {"count": 0}

        def change_on_second_read(candidate):
            calls["count"] += 1
            content = original_read(candidate)
            if calls["count"] == 2:
                return content + b"\nchanged during collection\n"
            return content

        with patch.object(repository, "_read_bytes", side_effect=change_on_second_read):
            unstable = collect_working_tree(self.root)

        self.assertFalse(unstable.coverage.complete)
        self.assertEqual(unstable.snapshot.identity, "")
        self.assertEqual(
            unstable.coverage.eligible_files,
            unstable.coverage.analyzed_files + unstable.coverage.failed_files,
        )
        self.assertIn("SNAPSHOT_UNSTABLE", [item.code for item in unstable.coverage.diagnostics])

    def test_invalid_python_encoding_is_visible_as_failed_coverage(self):
        path = self.root / "invalid_encoding.py"
        path.write_bytes(b"# coding: not-a-real-encoding\nvalue = 1\n")
        self._commit()

        collected = collect_working_tree(self.root)

        self.assertEqual(collected.coverage.eligible_files, 1)
        self.assertEqual(collected.coverage.analyzed_files, 0)
        self.assertEqual(collected.coverage.failed_files, 1)
        self.assertFalse(collected.coverage.complete)
        self.assertIn("SOURCE_READ_FAILED", [item.code for item in collected.coverage.diagnostics])

    def test_read_failure_is_visible_as_failed_coverage(self):
        path = self.root / "unreadable.py"
        path.write_text("value = 1\n", encoding="utf-8")
        self._commit()

        with patch.object(repository, "_read_bytes", side_effect=OSError("controlled read failure")):
            collected = collect_working_tree(self.root)

        self.assertEqual(collected.coverage.eligible_files, 1)
        self.assertEqual(collected.coverage.analyzed_files, 0)
        self.assertEqual(collected.coverage.failed_files, 1)
        self.assertFalse(collected.coverage.complete)
        self.assertIn("SOURCE_READ_FAILED", [item.code for item in collected.coverage.diagnostics])

    def test_non_git_directory_fails_explicitly(self):
        non_git = Path(self.tempdir.name) / "not-a-repo"
        non_git.mkdir()

        with self.assertRaises(RepositoryError):
            collect_working_tree(non_git)

    def test_collect_commit_reads_old_content_without_touching_worktree(self):
        path = self.root / "changed file.py"
        path.write_text("value = 'baseline'\n", encoding="utf-8")
        self._commit()
        baseline = self._git("rev-parse", "HEAD").stdout.decode().strip()
        path.write_text("value = 'working tree'\n", encoding="utf-8")
        before_status = self._git("status", "--porcelain=v1", "-z").stdout

        collected = collect_commit(self.root, "HEAD")

        after_status = self._git("status", "--porcelain=v1", "-z").stdout
        self.assertEqual(collected.snapshot.identity, baseline)
        self.assertEqual(collected.snapshot.source, "commit:{}".format(baseline))
        self.assertEqual(collected.snapshot.files[0].content, "value = 'baseline'\n")
        self.assertTrue(collected.coverage.complete)
        self.assertEqual(before_status, after_status)
        self.assertEqual(path.read_text(encoding="utf-8"), "value = 'working tree'\n")

    def test_collect_commit_rejects_missing_ref_and_empty_repository(self):
        with self.assertRaises(RepositoryError):
            collect_commit(self.root, "missing-base")

        empty = Path(self.tempdir.name) / "empty-repo"
        empty.mkdir()
        self._git("init", "-q", "--", str(empty))
        with self.assertRaises(RepositoryError):
            collect_commit(empty, "HEAD")

    def test_collect_change_set_reports_rename_modification_addition_and_deletion(self):
        old_path = self.root / "old.py"
        old_path.write_text("value = 'baseline'\n", encoding="utf-8")
        (self.root / "removed.py").write_text("value = 'removed'\n", encoding="utf-8")
        (self.root / "modified.py").write_text("value = 'before'\n", encoding="utf-8")
        self._commit()

        self._git("mv", "old.py", "renamed.py")
        (self.root / "modified.py").write_text("value = 'after'\n", encoding="utf-8")
        (self.root / "removed.py").unlink()
        (self.root / "added.py").write_text("value = 'added'\n", encoding="utf-8")
        current = collect_working_tree(self.root)

        changes = collect_change_set(self.root, "HEAD", current)

        self.assertEqual(changes.renames, (("old.py", "renamed.py"),))
        self.assertEqual(changes.added_paths, ("added.py",))
        self.assertEqual(changes.deleted_paths, ("removed.py",))
        self.assertEqual(changes.modified_paths, ("modified.py",))
        self.assertEqual(changes.target_snapshot_identity, current.snapshot.identity)


class GoMetadataCollectionTest(unittest.TestCase):
    """G01: go.mod/go.work snapshot metadata behavior."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "spellguard-test@example.invalid")
        self._git("config", "user.name", "Spellguard Test")
        (self.root / "backend").mkdir()
        (self.root / "backend" / "go.mod").write_text("module example.test/demo\n")
        (self.root / "backend" / "legacy").mkdir()
        (self.root / "backend" / "legacy" / "adapt.go").write_text(
            "package legacy\nfunc Adapt() int { return 0 }\n")
        self._commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        subprocess.run(["git", "-C", str(self.root)] + list(arguments),
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _commit(self):
        self._git("add", "--all")
        self._git("commit", "-qm", "fixture")

    def test_metadata_default_off_identity_unchanged(self):
        default = collect_working_tree(self.root)
        self.assertNotIn("backend/go.mod", {f.path for f in default.snapshot.files})
        commit = repository.collect_commit(self.root, "HEAD")
        self.assertNotIn("backend/go.mod", {f.path for f in commit.snapshot.files})
        # go.mod counts as unsupported by default
        default_ids = {f.path for f in default.snapshot.files}
        self.assertIn("backend/legacy/adapt.go", default_ids)

    def test_metadata_on_includes_go_mod_in_snapshot(self):
        collected = collect_working_tree(self.root, include_go_metadata=True)
        files = {f.path for f in collected.snapshot.files}
        self.assertIn("backend/go.mod", files)
        self.assertIn("backend/legacy/adapt.go", files)
        self.assertTrue(collected.coverage.complete)

    def test_metadata_content_change_changes_snapshot(self):
        first = collect_working_tree(self.root, include_go_metadata=True)
        (self.root / "backend" / "go.mod").write_text(
            "module example.test/renamed\n")
        second = collect_working_tree(self.root, include_go_metadata=True)
        mod_a = next(f for f in first.snapshot.files
                     if f.path == "backend/go.mod")
        mod_b = next(f for f in second.snapshot.files
                     if f.path == "backend/go.mod")
        self.assertNotEqual(mod_a.digest, mod_b.digest)

    def test_metadata_failed_read_keeps_diagnostic(self):
        # drop read permission on go.mod
        path = self.root / "backend" / "go.mod"
        path.chmod(0o000)
        try:
            collected = collect_working_tree(self.root, include_go_metadata=True)
        finally:
            path.chmod(0o644)
        codes = {d.code for d in collected.coverage.diagnostics}
        self.assertTrue(codes & {"SOURCE_READ_FAILED", "FILE_STAT_FAILED",
                                 "GIT_METADATA_EXCLUDED"},
                        str(codes))


if __name__ == "__main__":
    unittest.main()
