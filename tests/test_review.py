import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from spellguard.models import Finding, SourceRange
from spellguard.review import analyze_review, compare_findings


def finding(fingerprint, line, path="sample.py", comparison_key=""):
    return Finding(
        rule_id="SG001",
        rule_version="1",
        fingerprint_version="1",
        fingerprint=fingerprint,
        severity="medium",
        confidence="high",
        primary_location=SourceRange(path, line, 4, line, 12),
        related_locations=(),
        fact_description="literal return",
        investigation_prompt="check reachability",
        comparison_key=comparison_key,
    )


class ReviewMatchingTest(unittest.TestCase):
    def test_new_finding_is_introduced_and_existing_finding_persists(self):
        baseline = [finding("old", 5)]
        current = [finding("old", 8), finding("new", 12)]

        delta = compare_findings(baseline, current)

        self.assertEqual(len(delta.persisting), 1)
        self.assertEqual(delta.persisting[0].before.primary_location.start_line, 5)
        self.assertEqual(delta.persisting[0].after.primary_location.start_line, 8)
        self.assertEqual([item.after.fingerprint for item in delta.introduced], ["new"])
        self.assertEqual(delta.resolved, ())
        self.assertEqual(delta.unverified, ())

    def test_missing_finding_is_resolved_but_different_structure_is_not_persisting(self):
        delta = compare_findings([finding("old", 5)], [finding("new", 5)])

        self.assertEqual([item.before.fingerprint for item in delta.resolved], ["old"])
        self.assertEqual([item.after.fingerprint for item in delta.introduced], ["new"])
        self.assertEqual(delta.persisting, ())

    def test_duplicate_fingerprints_with_both_sides_are_unverified(self):
        baseline = [finding("same", 5), finding("same", 15)]
        current = [finding("same", 7), finding("same", 17)]

        delta = compare_findings(baseline, current)

        self.assertEqual(delta.persisting, ())
        self.assertEqual(delta.introduced, ())
        self.assertEqual(delta.resolved, ())
        self.assertEqual(len(delta.unverified), 4)
        self.assertEqual(
            [item.reason for item in delta.unverified],
            ["ambiguous-fingerprint"] * 4,
        )

    def test_duplicate_fingerprint_growth_does_not_claim_new_location(self):
        baseline = [finding("same", 5), finding("same", 15)]
        current = [finding("same", 5), finding("same", 10), finding("same", 15)]

        delta = compare_findings(baseline, current)

        self.assertEqual(delta.persisting, ())
        self.assertEqual(delta.introduced, ())
        self.assertEqual(delta.resolved, ())
        self.assertEqual(len(delta.unverified), 5)
        self.assertEqual(
            [item.after.primary_location.start_line for item in delta.unverified if item.after],
            [5, 10, 15],
        )

    def test_duplicate_fingerprint_only_on_one_side_keeps_count_change(self):
        baseline = [finding("same", 5), finding("same", 15)]
        current = []

        delta = compare_findings(baseline, current)

        self.assertEqual(delta.introduced, ())
        self.assertEqual(delta.persisting, ())
        self.assertEqual(
            [item.before.primary_location.start_line for item in delta.resolved],
            [5, 15],
        )

    def test_unique_explicit_rename_persists_with_both_paths(self):
        baseline = [finding("old-fingerprint", 5, "old.py", "structure-a")]
        current = [finding("new-fingerprint", 5, "new.py", "structure-a")]

        delta = compare_findings(baseline, current, renames=(("old.py", "new.py"),))

        self.assertEqual(len(delta.persisting), 1)
        self.assertEqual(delta.persisting[0].reason, "explicit-rename")
        self.assertEqual(delta.persisting[0].before.primary_location.path, "old.py")
        self.assertEqual(delta.persisting[0].after.primary_location.path, "new.py")
        self.assertEqual(delta.introduced, ())
        self.assertEqual(delta.resolved, ())
        self.assertEqual(delta.unverified, ())

    def test_duplicate_structure_after_rename_is_unverified_on_both_sides(self):
        baseline = [
            finding("old-one", 5, "old.py", "same-structure"),
            finding("old-two", 15, "old.py", "same-structure"),
        ]
        current = [
            finding("new-one", 5, "new.py", "same-structure"),
            finding("new-two", 15, "new.py", "same-structure"),
        ]

        delta = compare_findings(baseline, current, renames=(("old.py", "new.py"),))

        self.assertEqual(delta.persisting, ())
        self.assertEqual(delta.introduced, ())
        self.assertEqual(delta.resolved, ())
        self.assertEqual(len(delta.unverified), 4)
        self.assertEqual(
            [item.reason for item in delta.unverified],
            ["ambiguous-rename"] * 4,
        )


class ReviewAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "spellguard-test@example.invalid")
        self._git("config", "user.name", "Spellguard Test")
        self.old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.old_cwd)
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

    def test_analyze_review_uses_base_and_current_independently(self):
        (self.root / "sample.py").write_text(
            """def sample():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        self._commit()
        (self.root / "sample.py").write_text(
            """def sample():
    try:
        work()
    except Exception:
        return 1
""",
            encoding="utf-8",
        )
        (self.root / "new.py").write_text(
            """def new():
    try:
        work()
    except Exception:
        return []
""",
            encoding="utf-8",
        )

        result = analyze_review(self.root, "HEAD")

        self.assertTrue(result.complete)
        self.assertEqual(result.base_snapshot.source.split(":", 1)[0], "commit")
        self.assertEqual(result.current_snapshot.source, "working-tree")
        self.assertEqual(result.change_set.added_paths, ("new.py",))
        self.assertEqual(len(result.base_findings), 1)
        self.assertEqual(len(result.current_findings), 2)
        self.assertEqual(len(result.delta.introduced), 2)
        self.assertEqual(len(result.delta.resolved), 1)

    def test_deleted_current_support_files_still_allow_complete_review(self):
        (self.root / "sample.py").write_text(
            """def sample():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        self._commit()
        (self.root / "sample.py").unlink()

        result = analyze_review(self.root, "HEAD")

        self.assertTrue(result.complete)
        self.assertEqual(result.current_findings, ())
        self.assertEqual(len(result.delta.resolved), 1)

    def test_both_review_sides_without_supported_files_are_incomplete(self):
        (self.root / "README.txt").write_text("no Python\n", encoding="utf-8")
        self._commit()

        result = analyze_review(self.root, "HEAD")

        self.assertFalse(result.complete)
        self.assertEqual(result.base_findings, ())
        self.assertIn("NO_SUPPORTED_FILES", [item.code for item in result.diagnostics])

    def test_incomplete_analysis_does_not_mark_old_finding_resolved(self):
        (self.root / "sample.py").write_text(
            """def sample():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self._commit()
        (self.root / "sample.py").unlink()

        result = analyze_review(self.root, "HEAD")

        self.assertFalse(result.complete)
        self.assertEqual(result.delta.resolved, ())
        self.assertEqual(len(result.delta.unverified), 1)
        self.assertEqual(result.delta.unverified[0].reason, "incomplete-analysis")

    def test_exception_type_broadening_is_currently_persisting_scope_gap(self):
        (self.root / "sample.py").write_text(
            """def sample():
    try:
        work()
    except ValueError:
        return 0
""",
            encoding="utf-8",
        )
        self._commit()
        (self.root / "sample.py").write_text(
            """def sample():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )

        result = analyze_review(self.root, "HEAD")

        self.assertTrue(result.complete)
        self.assertEqual(len(result.delta.persisting), 1)
        self.assertEqual(result.delta.introduced, ())
        self.assertEqual(result.delta.resolved, ())


if __name__ == "__main__":
    unittest.main()
