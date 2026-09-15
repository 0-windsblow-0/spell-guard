import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from spellguard.cli import main


class CliTest(unittest.TestCase):
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

    def _run(self, arguments):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_candidate(self):
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

    def _write_many_candidates(self, count):
        functions = []
        for index in range(count):
            functions.append(
                "def candidate_{0}():\n"
                "    try:\n"
                "        work()\n"
                "    except Exception:\n"
                "        return {0}\n".format(index)
            )
        (self.root / "history.py").write_text(
            "\n".join(functions) + "\n", encoding="utf-8"
        )

    def test_scan_json_is_stable_and_reports_experimental_rule(self):
        self._write_candidate()
        before = self._git("status", "--porcelain=v1", "-z").stdout

        code, output, error = self._run(["scan", "--format", "json"])
        first = json.loads(output)
        second_code, second_output, second_error = self._run(["scan", "--format", "json"])
        second = json.loads(second_output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(second_code, 0)
        self.assertEqual(second_error, "")
        self.assertEqual(first, second)
        self.assertEqual(first["analysis_mode"], "experimental")
        self.assertEqual(first["enabled_rules"], ["SG001", "SG002", "SG003"])
        self.assertTrue(first["coverage"]["complete"])
        self.assertEqual(first["findings"][0]["rule_id"], "SG001")
        self.assertEqual(first["governance"]["source"], "missing")
        self.assertEqual(first["findings"][0]["governance_status"], "unregistered")
        self.assertEqual(before, self._git("status", "--porcelain=v1", "-z").stdout)

    def test_scan_review_and_debt_share_ruleset_contract(self):
        self._write_candidate()

        scan_code, scan_output, scan_error = self._run(["scan", "--format", "json"])
        review_code, review_output, review_error = self._run(
            ["review", "--base", "HEAD", "--format", "json"]
        )
        debt_code, debt_output, debt_error = self._run(["debt", "--format", "json"])
        reports = [
            json.loads(scan_output),
            json.loads(review_output),
            json.loads(debt_output),
        ]

        self.assertEqual(scan_code, 0)
        self.assertEqual(review_code, 0)
        self.assertEqual(debt_code, 0)
        self.assertEqual(scan_error, "")
        self.assertEqual(review_error, "")
        self.assertEqual(debt_error, "")
        self.assertEqual({report["schema_version"] for report in reports}, {"1"})
        self.assertEqual(
            {report["ruleset_version"] for report in reports}, {"candidate-5"}
        )
        self.assertEqual(
            {tuple(report["enabled_rules"]) for report in reports},
            {("SG001", "SG002", "SG003")},
        )
        self.assertEqual({report["analysis_mode"] for report in reports}, {"experimental"})

    def test_corrupt_registry_fails_all_commands_with_same_json_diagnostic(self):
        self._write_candidate()
        (self.root / ".spellguard-exceptions.json").write_text(
            '{"schema_version": 99, "exceptions": []}\n',
            encoding="utf-8",
        )

        for arguments in (
            ["scan", "--format", "json"],
            ["review", "--base", "HEAD", "--format", "json"],
            ["debt", "--format", "json"],
        ):
            with self.subTest(command=arguments[0]):
                code, output, error = self._run(arguments)
                report = json.loads(output)

                self.assertEqual(code, 2)
                self.assertEqual(error, "")
                self.assertTrue(report["error"])
                self.assertEqual(
                    report["diagnostics"][0]["code"], "GOVERNANCE_ERROR"
                )

    def test_review_uses_current_registry_not_base_registry(self):
        (self.root / "base.py").write_text("value = 1\n", encoding="utf-8")
        self._commit()
        (self.root / "candidate.py").write_text(
            """def candidate():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        _, scan_output, _ = self._run(["scan", "--format", "json"])
        finding = json.loads(scan_output)["findings"][0]
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "exceptions": [
                        {
                            "fingerprint": finding["fingerprint"],
                            "rule_id": finding["rule_id"],
                            "reason": "Accepted before the current review.",
                            "owner": "team-spellguard",
                            "created_at": "2026-01-01",
                            "expires_at": "2999-01-01",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self._git("add", ".spellguard-exceptions.json")
        self._git("commit", "-qm", "accept future candidate in base registry")
        (self.root / ".spellguard-exceptions.json").unlink()

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        report = json.loads(output)

        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertEqual(report["governance"]["source"], "missing")
        self.assertEqual(
            report["delta"]["introduced"][0]["after"]["governance_status"],
            "unregistered",
        )

    def test_old_fingerprint_stays_unregistered_across_all_commands(self):
        (self.root / "base.py").write_text("value = 1\n", encoding="utf-8")
        self._commit()
        (self.root / "candidate.py").write_text(
            """def candidate():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "exceptions": [
                        {
                            "fingerprint": "legacy-fingerprint-v0",
                            "rule_id": "SG001",
                            "reason": "Recorded under an older candidate.",
                            "owner": "team-spellguard",
                            "created_at": "2026-01-01",
                            "expires_at": "2999-01-01",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        scan_code, scan_output, _ = self._run(["scan", "--format", "json"])
        debt_code, debt_output, _ = self._run(["debt", "--format", "json"])
        review_code, review_output, _ = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        scan_report = json.loads(scan_output)
        debt_report = json.loads(debt_output)
        review_report = json.loads(review_output)

        self.assertEqual(scan_code, 0)
        self.assertEqual(debt_code, 0)
        self.assertEqual(review_code, 1)
        self.assertEqual(scan_report["findings"][0]["governance_status"], "unregistered")
        self.assertEqual(debt_report["debt"]["unregistered"], debt_report["findings"])
        self.assertEqual(len(debt_report["governance"]["unmatched"]), 1)
        self.assertEqual(
            review_report["delta"]["introduced"][0]["after"]["governance_status"],
            "unregistered",
        )
        self.assertEqual(scan_report["ruleset_version"], "candidate-5")
        self.assertEqual(debt_report["ruleset_version"], "candidate-5")
        self.assertEqual(review_report["ruleset_version"], "candidate-5")

    def test_scan_text_contains_evidence_and_experimental_mode(self):
        self._write_candidate()

        code, output, error = self._run(["scan"])

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("analysis_mode: experimental", output)
        self.assertIn("[SG001]", output)
        self.assertIn("Check whether the return is reachable", output)

    def test_scan_uses_shared_rule_entry_for_repeated_decisions(self):
        (self.root / "decisions.py").write_text(
            """def first(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2

def second(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2
""",
            encoding="utf-8",
        )
        self._commit()

        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["rule_id"], "SG002")
        self.assertEqual(report["findings"][0]["related_locations"][0]["start_line"], 8)

    def test_scan_uses_shared_rule_entry_for_nested_literal_cases(self):
        (self.root / "nested.py").write_text(
            """def sample(value):
    if value == \"outer\":
        if value == \"inner\":
            return 1
""",
            encoding="utf-8",
        )
        self._commit()

        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["rule_id"], "SG003")
        self.assertEqual(
            report["findings"][0]["related_locations"][0]["start_line"],
            3,
        )

    def test_scan_associates_valid_exception_registry_without_hiding_finding(self):
        self._write_candidate()
        _, initial_output, _ = self._run(["scan", "--format", "json"])
        initial = json.loads(initial_output)
        finding = initial["findings"][0]
        registry = {
            "schema_version": 1,
            "exceptions": [
                {
                    "fingerprint": finding["fingerprint"],
                    "rule_id": finding["rule_id"],
                    "reason": "Legacy compatibility path is intentional.",
                    "owner": "team-spellguard",
                    "created_at": "2026-01-01",
                    "expires_at": "2999-01-01",
                }
            ],
        }
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(registry) + "\n", encoding="utf-8"
        )

        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(report["governance"]["source"], ".spellguard-exceptions.json")
        self.assertEqual(report["findings"][0]["governance_status"], "accepted")
        self.assertEqual(len(report["findings"]), 1)

    def test_partial_parse_returns_json_and_exit_two(self):
        (self.root / "valid.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self._commit()

        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["failed_files"], 1)
        self.assertEqual(report["diagnostics"][0]["path"], "broken.py")

    def test_no_supported_files_returns_two_without_claiming_success(self):
        (self.root / "README.txt").write_text("not Python\n", encoding="utf-8")
        self._commit()

        code, output, _ = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["diagnostics"][0]["code"], "NO_SUPPORTED_FILES")

    def test_invalid_registry_is_json_error(self):
        self._write_candidate()
        (self.root / ".spellguard-exceptions.json").write_text(
            '{"schema_version": 99, "exceptions": []}\n',
            encoding="utf-8",
        )

        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertTrue(report["error"])
        self.assertEqual(report["diagnostics"][0]["code"], "GOVERNANCE_ERROR")

    def test_invalid_registry_is_json_error_for_review(self):
        self._write_candidate()
        (self.root / ".spellguard-exceptions.json").write_text(
            '{"schema_version": 99, "exceptions": []}\n',
            encoding="utf-8",
        )

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json"]
        )
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertTrue(report["error"])
        self.assertEqual(report["diagnostics"][0]["code"], "GOVERNANCE_ERROR")

    def test_missing_review_base_is_structured_json_error(self):
        self._write_candidate()

        code, output, error = self._run(
            ["review", "--base", "missing-ref", "--format", "json"]
        )
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertTrue(report["error"])
        self.assertEqual(report["diagnostics"][0]["code"], "REPOSITORY_ERROR")

    def test_scan_from_subdirectory_uses_repository_root_registry(self):
        self._write_candidate()
        (self.root / ".spellguard-exceptions.json").write_text(
            '{"schema_version": 99, "exceptions": []}\n',
            encoding="utf-8",
        )
        nested = self.root / "nested"
        nested.mkdir()
        os.chdir(nested)

        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertEqual(report["diagnostics"][0]["code"], "GOVERNANCE_ERROR")

    def test_review_json_reports_delta_and_current_governance(self):
        self._write_candidate()
        (self.root / "new.py").write_text(
            """def new():
    try:
        work()
    except Exception:
        return []
""",
            encoding="utf-8",
        )

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json"]
        )
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertTrue(report["complete"])
        self.assertEqual(report["change_set"]["added_paths"], ["new.py"])
        self.assertEqual(len(report["delta"]["introduced"]), 1)
        self.assertEqual(
            report["delta"]["introduced"][0]["after"]["governance_status"],
            "unregistered",
        )

        text_code, text_output, text_error = self._run(
            ["review", "--base", "HEAD"]
        )
        self.assertEqual(text_code, 0)
        self.assertEqual(text_error, "")
        self.assertIn("introduced: 1", text_output)
        self.assertIn("Check whether the return is reachable", text_output)

    def test_review_fail_on_medium_returns_one_only_for_unaccepted_introduced(self):
        (self.root / "base.py").write_text("value = 1\n", encoding="utf-8")
        self._commit()
        (self.root / "candidate.py").write_text(
            """def candidate():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )

        default_code, _, _ = self._run(["review", "--base", "HEAD", "--format", "json"])
        threshold_code, _, _ = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )

        self.assertEqual(default_code, 0)
        self.assertEqual(threshold_code, 1)

    def test_review_high_threshold_and_persisting_findings_do_not_block(self):
        self._write_candidate()

        default_code, output, _ = self._run(
            ["review", "--base", "HEAD", "--format", "json"]
        )
        high_code, _, _ = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "high"]
        )
        medium_code, _, _ = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        report = json.loads(output)

        self.assertEqual(default_code, 0)
        self.assertEqual(high_code, 0)
        self.assertEqual(medium_code, 0)
        self.assertEqual(len(report["delta"]["persisting"]), 1)
        self.assertEqual(report["delta"]["introduced"], [])

    def test_review_expired_current_registry_still_blocks_new_finding(self):
        (self.root / "base.py").write_text("value = 1\n", encoding="utf-8")
        self._commit()
        (self.root / "candidate.py").write_text(
            """def candidate():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        _, output, _ = self._run(["review", "--base", "HEAD", "--format", "json"])
        finding = json.loads(output)["delta"]["introduced"][0]["after"]
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "exceptions": [
                        {
                            "fingerprint": finding["fingerprint"],
                            "rule_id": finding["rule_id"],
                            "reason": "Expired compatibility record.",
                            "owner": "team-spellguard",
                            "created_at": "2020-01-01",
                            "expires_at": "2021-01-01",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        report = json.loads(output)

        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertEqual(
            report["delta"]["introduced"][0]["after"]["governance_status"],
            "expired",
        )

    def test_review_cli_reports_deleted_support_file_as_resolved(self):
        self._write_candidate()
        (self.root / "sample.py").unlink()

        code, output, error = self._run(["review", "--base", "HEAD", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["delta"]["resolved"]), 1)
        self.assertEqual(report["delta"]["unverified"], [])

    def test_review_cli_reports_empty_both_sides_as_incomplete(self):
        (self.root / "README.txt").write_text("no Python\n", encoding="utf-8")
        self._commit()

        code, output, error = self._run(["review", "--base", "HEAD", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertFalse(report["complete"])
        self.assertEqual(report["delta"]["resolved"], [])
        self.assertIn("NO_SUPPORTED_FILES", [item["code"] for item in report["diagnostics"]])

    def test_review_cli_keeps_ambiguous_rename_unverified_without_blocking(self):
        (self.root / "old.py").write_text(
            """def duplicated():
    try:
        work()
    except Exception:
        return 0

    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        self._commit()
        self._git("mv", "old.py", "new.py")

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(report["delta"]["introduced"], [])
        self.assertEqual(report["delta"]["resolved"], [])
        self.assertEqual(len(report["delta"]["unverified"]), 4)
        self.assertEqual(
            {item["reason"] for item in report["delta"]["unverified"]},
            {"ambiguous-rename"},
        )
        text_code, text_output, text_error = self._run(
            ["review", "--base", "HEAD", "--all"]
        )
        self.assertEqual(text_code, 0)
        self.assertEqual(text_error, "")
        self.assertIn("reason: ambiguous-rename", text_output)
        self.assertNotIn("instance correspondence uncertain", text_output)

    def test_review_cli_keeps_duplicate_fingerprint_ambiguity_out_of_new_blocking(self):
        source = """def duplicated():
    try:
        work()
    except Exception:
        return 0

    try:
        work()
    except Exception:
        return 0
"""
        (self.root / "sample.py").write_text(source, encoding="utf-8")
        self._commit()
        (self.root / "sample.py").write_text(
            source
            + """
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        (self.root / "new.py").write_text(
            """def new_candidate():
    try:
        work()
    except Exception:
        return []
""",
            encoding="utf-8",
        )

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        report = json.loads(output)

        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertEqual(len(report["delta"]["introduced"]), 1)
        self.assertEqual(report["delta"]["introduced"][0]["after"]["primary_location"]["path"], "new.py")
        self.assertEqual(report["delta"]["resolved"], [])
        self.assertEqual(len(report["delta"]["unverified"]), 5)
        self.assertEqual(
            {item["reason"] for item in report["delta"]["unverified"]},
            {"ambiguous-fingerprint"},
        )

        text_code, text_output, text_error = self._run(
            ["review", "--base", "HEAD", "--all"]
        )
        second_text_code, second_text_output, second_text_error = self._run(
            ["review", "--base", "HEAD", "--all"]
        )
        self.assertEqual(text_code, 0)
        self.assertEqual(text_error, "")
        self.assertEqual(second_text_code, 0)
        self.assertEqual(second_text_error, "")
        self.assertEqual(text_output, second_text_output)
        self.assertIn("unverified: 5", text_output)
        self.assertIn("instance correspondence uncertain", text_output)
        self.assertIn("baseline: 2 locations — sample.py: 5, 10", text_output)
        self.assertIn("current: 3 locations — sample.py: 5, 10, 15", text_output)
        self.assertIn("count increased by 1; exact new location is unknown.", text_output)
        self.assertIn("governance: unregistered", text_output)
        self.assertNotIn("reason: ambiguous-fingerprint", text_output)

    def test_duplicate_current_findings_are_ambiguous_in_debt(self):
        source = """def duplicated():
    try:
        work()
    except Exception:
        return 0

    try:
        work()
    except Exception:
        return 0
"""
        (self.root / "sample.py").write_text(source, encoding="utf-8")
        self._commit()
        code, scan_output, _ = self._run(["scan", "--format", "json"])
        finding = json.loads(scan_output)["findings"][0]
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "exceptions": [
                        {
                            "fingerprint": finding["fingerprint"],
                            "rule_id": finding["rule_id"],
                            "reason": "Known compatibility behavior.",
                            "owner": "team",
                            "created_at": "2026-01-01",
                            "expires_at": "2999-01-01",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        debt_code, debt_output, debt_error = self._run(["debt", "--format", "json"])
        report = json.loads(debt_output)

        self.assertEqual(code, 0)
        self.assertEqual(debt_code, 0)
        self.assertEqual(debt_error, "")
        self.assertEqual(len(report["debt"]["ambiguous"]), 2)
        self.assertEqual(report["debt"]["accepted"], [])
        self.assertEqual(
            {item["governance_status"] for item in report["debt"]["ambiguous"]},
            {"ambiguous"},
        )

        scan_code, scan_output, scan_error = self._run(["scan", "--format", "json"])
        scan_report = json.loads(scan_output)
        review_code, review_output, review_error = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )
        review_report = json.loads(review_output)
        self.assertEqual(scan_code, 0)
        self.assertEqual(scan_error, "")
        self.assertEqual(
            {item["governance_status"] for item in scan_report["findings"]},
            {"ambiguous"},
        )
        self.assertEqual(review_code, 0)
        self.assertEqual(review_error, "")
        self.assertEqual(review_report["delta"]["introduced"], [])
        self.assertEqual(len(review_report["delta"]["unverified"]), 4)
        self.assertEqual(
            {item["governance_status"] for item in review_report["current_findings"]},
            {"ambiguous"},
        )
        text_code, text_output, text_error = self._run(
            ["review", "--base", "HEAD", "--all"]
        )
        self.assertEqual(text_code, 0)
        self.assertEqual(text_error, "")
        self.assertIn("count unchanged; exact correspondence remains unknown.", text_output)

    def test_review_cli_preserves_two_hundred_history_items_and_prioritizes_new(self):
        self._write_many_candidates(200)
        self._commit()
        (self.root / "new.py").write_text(
            """def new_candidate():
    try:
        work()
    except Exception:
        return []
""",
            encoding="utf-8",
        )

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json"]
        )
        report = json.loads(output)
        text_code, text_output, text_error = self._run(
            ["review", "--base", "HEAD"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(len(report["delta"]["persisting"]), 200)
        self.assertEqual(len(report["delta"]["introduced"]), 1)
        self.assertEqual(text_code, 0)
        self.assertEqual(text_error, "")
        self.assertIn("persisting: 200", text_output)
        self.assertLess(text_output.index("introduced: 1"), text_output.index("persisting: 200"))

    def test_review_fail_on_medium_respects_current_accepted_registry(self):
        (self.root / "base.py").write_text("value = 1\n", encoding="utf-8")
        self._commit()
        (self.root / "candidate.py").write_text(
            """def candidate():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        _, output, _ = self._run(["review", "--base", "HEAD", "--format", "json"])
        finding = json.loads(output)["delta"]["introduced"][0]["after"]
        registry = {
            "schema_version": 1,
            "exceptions": [
                {
                    "fingerprint": finding["fingerprint"],
                    "rule_id": finding["rule_id"],
                    "reason": "Intentional compatibility behavior.",
                    "owner": "team-spellguard",
                    "created_at": "2026-01-01",
                    "expires_at": "2999-01-01",
                }
            ],
        }
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(registry) + "\n", encoding="utf-8"
        )

        code, _, _ = self._run(
            ["review", "--base", "HEAD", "--format", "json", "--fail-on", "medium"]
        )

        self.assertEqual(code, 0)

    def test_review_incomplete_analysis_returns_two_and_keeps_old_finding_unverified(self):
        self._write_candidate()
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self._git("add", "broken.py")
        self._git("commit", "-qm", "broken baseline")
        (self.root / "sample.py").unlink()

        code, output, error = self._run(
            ["review", "--base", "HEAD", "--format", "json"]
        )
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertFalse(report["complete"])
        self.assertEqual(report["delta"]["resolved"], [])
        self.assertEqual(len(report["delta"]["unverified"]), 1)

    def test_debt_json_groups_current_findings_and_unmatched_registry_read_only(self):
        self._write_candidate()
        (self.root / "second.py").write_text(
            """def second():
    try:
        work()
    except Exception:
        return []
""",
            encoding="utf-8",
        )
        _, scan_output, _ = self._run(["scan", "--format", "json"])
        findings = json.loads(scan_output)["findings"]
        registry = {
            "schema_version": 1,
            "exceptions": [
                {
                    "fingerprint": findings[0]["fingerprint"],
                    "rule_id": findings[0]["rule_id"],
                    "reason": "Accepted existing compatibility behavior.",
                    "owner": "team-spellguard",
                    "created_at": "2020-01-01",
                    "expires_at": "2999-01-01",
                },
                {
                    "fingerprint": findings[1]["fingerprint"],
                    "rule_id": findings[1]["rule_id"],
                    "reason": "Expired compatibility record.",
                    "owner": "team-spellguard",
                    "created_at": "2020-01-01",
                    "expires_at": "2021-01-01",
                },
                {
                    "fingerprint": "not-currently-present",
                    "rule_id": "SG001",
                    "reason": "Historical record awaiting review.",
                    "owner": "team-spellguard",
                    "created_at": "2020-01-01",
                    "expires_at": "2999-01-01",
                },
            ],
        }
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(registry) + "\n", encoding="utf-8"
        )
        before = self._git("status", "--porcelain=v1", "-z").stdout

        code, output, error = self._run(["debt", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertTrue(report["complete"])
        self.assertTrue(report["candidate_closure"])
        self.assertEqual(len(report["debt"]["accepted"]), 1)
        self.assertEqual(len(report["debt"]["expired"]), 1)
        self.assertEqual(len(report["debt"]["unregistered"]), 0)
        self.assertEqual(len(report["governance"]["unmatched"]), 1)
        self.assertEqual(before, self._git("status", "--porcelain=v1", "-z").stdout)

        text_code, text_output, text_error = self._run(["debt"])
        self.assertEqual(text_code, 0)
        self.assertEqual(text_error, "")
        self.assertIn("accepted: 1", text_output)
        self.assertIn("expired: 1", text_output)
        self.assertIn("candidate-closure", text_output)

    def test_debt_incomplete_analysis_returns_two_without_candidate_closure(self):
        (self.root / "valid.py").write_text(
            """def valid():
    try:
        work()
    except Exception:
        return 0
""",
            encoding="utf-8",
        )
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self._commit()

        code, output, error = self._run(["debt", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertFalse(report["complete"])
        self.assertFalse(report["candidate_closure"])
        self.assertEqual(report["coverage"]["failed_files"], 1)

    def test_debt_no_supported_files_returns_two(self):
        (self.root / "README.txt").write_text("no Python\n", encoding="utf-8")
        self._commit()

        code, output, error = self._run(["debt", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertFalse(report["complete"])
        self.assertEqual(report["diagnostics"][0]["code"], "NO_SUPPORTED_FILES")

    def test_debt_invalid_registry_is_structured_json_error(self):
        self._write_candidate()
        (self.root / ".spellguard-exceptions.json").write_text(
            '{"schema_version": 99, "exceptions": []}\n',
            encoding="utf-8",
        )

        code, output, error = self._run(["debt", "--format", "json"])
        report = json.loads(output)

        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertTrue(report["error"])
        self.assertEqual(report["diagnostics"][0]["code"], "GOVERNANCE_ERROR")

    def test_non_git_json_error_and_debt_succeeds(self):
        non_git = Path(self.tempdir.name) / "not-a-repo"
        non_git.mkdir()
        os.chdir(non_git)
        code, output, error = self._run(["scan", "--format", "json"])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(error, "")
        self.assertEqual(report["diagnostics"][0]["code"], "REPOSITORY_ERROR")

        debt_error_code, debt_error_output, debt_error = self._run(
            ["debt", "--format", "json"]
        )
        debt_error_report = json.loads(debt_error_output)
        self.assertEqual(debt_error_code, 2)
        self.assertEqual(debt_error, "")
        self.assertEqual(
            debt_error_report["diagnostics"][0]["code"], "REPOSITORY_ERROR"
        )

        os.chdir(self.root)
        self._write_candidate()
        review_code, review_output, _ = self._run(["review", "--base", "HEAD", "--format", "json"])
        debt_code, debt_output, _ = self._run(["debt", "--format", "json"])
        self.assertEqual(review_code, 0)
        self.assertEqual(debt_code, 0)
        self.assertTrue(json.loads(review_output)["complete"])
        self.assertTrue(json.loads(debt_output)["complete"])


if __name__ == "__main__":
    unittest.main()
