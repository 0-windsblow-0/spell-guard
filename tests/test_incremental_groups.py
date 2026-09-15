"""Behavior checks for growing, shrinking, and accepting repeated groups."""

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from spellguard.cli import main
from spellguard.review import analyze_review


def decision(name):
    return (
        "def {}(x):\n"
        "    if x == 1:\n"
        "        return 10\n"
        "    elif x == 2:\n"
        "        return 20\n\n"
    ).format(name)


class IncrementalGroupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Spellguard Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True
        )

    def write(self, names, path="sample.py"):
        (self.root / path).write_text("".join(decision(name) for name in names))

    def baseline(self, names=("first", "second")):
        self.write(names)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def run_cli(self, *args):
        previous = Path.cwd()
        output = StringIO()
        try:
            os.chdir(self.root)
            with redirect_stdout(output):
                code = main(args)
        finally:
            os.chdir(previous)
        return code, output.getvalue()

    def test_growth_has_before_after_and_new_identity(self):
        self.baseline()
        self.write(("first", "second", "third"))
        result = analyze_review(self.root, "HEAD")
        self.assertTrue(result.complete)
        self.assertEqual(len(result.delta.introduced), 1)
        entry = result.delta.introduced[0]
        self.assertEqual(entry.reason, "group-expanded")
        self.assertEqual(entry.before.group_members, ("first", "second"))
        self.assertEqual(entry.after.group_members, ("first", "second", "third"))
        self.assertNotEqual(entry.before.fingerprint, entry.after.fingerprint)
        self.assertEqual(result.delta.persisting, ())
        self.assertEqual(result.delta.resolved, ())

    def test_reorder_and_comment_do_not_create_risk(self):
        self.baseline()
        self.write(("second", "first"))
        path = self.root / "sample.py"
        path.write_text("# moved lines\n\n" + path.read_text())
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(len(delta.persisting), 1)
        self.assertEqual(delta.introduced, ())

    def test_shrink_is_not_introduced_and_single_member_resolves(self):
        self.baseline(("first", "second", "third"))
        self.write(("first", "second"))
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(delta.introduced, ())
        self.assertEqual(delta.persisting[0].reason, "group-reduced")
        self.assertEqual(self.run_cli("review", "--base", "HEAD", "--fail-on", "medium")[0], 0)
        self.write(("first",))
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(len(delta.resolved), 1)
        self.assertEqual(delta.introduced, ())

    def test_file_rename_then_growth_keeps_relation(self):
        self.baseline()
        self.git("mv", "sample.py", "renamed.py")
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(delta.persisting[0].reason, "explicit-rename")
        self.write(("first", "second", "third"), "renamed.py")
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(delta.introduced[0].reason, "group-expanded")
        self.assertEqual(delta.introduced[0].before.primary_location.path, "sample.py")
        self.assertEqual(delta.resolved, ())

    def test_old_acceptance_does_not_accept_growth_and_reports_explanation(self):
        self.baseline()
        finding = analyze_review(self.root, "HEAD").current_findings[0]
        registry = {"schema_version": 1, "exceptions": [{
            "fingerprint": finding.fingerprint, "rule_id": "SG002",
            "reason": "Two protocol entry points retained until migration",
            "owner": "test", "created_at": "2026-01-01", "expires_at": "2099-01-01",
        }]}
        path = self.root / ".spellguard-exceptions.json"
        path.write_text(json.dumps(registry))
        self.write(("first", "second", "third"))
        code, output = self.run_cli("review", "--base", "HEAD", "--fail-on", "medium", "--format", "json")
        self.assertEqual(code, 1)
        entry = json.loads(output)["delta"]["introduced"][0]
        self.assertEqual(entry["after"]["governance_status"], "unregistered")
        self.assertEqual(entry["after"]["group_members"], ["first", "second", "third"])
        _, text = self.run_cli("review", "--base", "HEAD")
        self.assertIn("group size: 2 -> 3", text)
        self.assertIn("new members: third", text)
        registry["exceptions"][0]["fingerprint"] = entry["after"]["fingerprint"]
        path.write_text(json.dumps(registry))
        self.assertEqual(self.run_cli("review", "--base", "HEAD", "--fail-on", "medium")[0], 0)

    def test_growth_with_another_parse_failure_returns_two(self):
        self.baseline()
        self.write(("first", "second", "third"))
        (self.root / "broken.py").write_text("def broken(:\n")
        code, output = self.run_cli("review", "--base", "HEAD", "--fail-on", "medium", "--format", "json")
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])
        self.assertEqual(report["delta"]["resolved"], [])
        self.assertEqual(report["delta"]["introduced"][0]["reason"], "group-expanded")

    def test_additional_occurrence_in_existing_function_is_growth(self):
        self.baseline()
        extra_body = decision("unused").split("\n", 1)[1]
        (self.root / "sample.py").write_text(decision("first") + extra_body + decision("second"))
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(delta.introduced[0].reason, "group-expanded")
        self.assertEqual(delta.introduced[0].after.group_members, ("first", "first", "second"))

    def test_member_replacement_is_not_silently_accepted(self):
        self.baseline()
        self.write(("first", "replacement"))
        delta = analyze_review(self.root, "HEAD").delta
        self.assertEqual(delta.introduced[0].reason, "group-expanded")
        self.assertEqual(delta.introduced[0].after.group_members, ("first", "replacement"))

    def test_syntax_version_is_part_of_group_identity(self):
        from dataclasses import replace
        from spellguard.analysis import parse_snapshot
        from spellguard.repository import collect_working_tree
        from spellguard.rules.repeated_decision import detect_repeated_decisions

        self.baseline()
        facts = parse_snapshot(collect_working_tree(self.root)).facts
        old = detect_repeated_decisions(facts)[0]
        changed = detect_repeated_decisions(tuple(
            replace(fact, syntax_model_version="test-only-other-version") for fact in facts
        ))[0]
        self.assertNotEqual(old.fingerprint, changed.fingerprint)
        self.assertNotEqual(old.comparison_key, changed.comparison_key)
