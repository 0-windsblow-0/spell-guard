import json
import tempfile
import unittest
from pathlib import Path

from spellguard.governance import GovernanceError, load_and_associate
from spellguard.models import Finding, SourceRange


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, value):
        (self.root / ".spellguard-exceptions.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_invalid_registry_shapes_fail_explicitly(self):
        invalid_values = [
            {"schema_version": 2, "exceptions": []},
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "same",
                        "rule_id": "SG001",
                        "reason": "one",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2027-01-01",
                    },
                    {
                        "fingerprint": "same",
                        "rule_id": "SG001",
                        "reason": "two",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2027-01-01",
                    },
                ],
            },
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "fp",
                        "rule_id": "SG001",
                        "reason": "",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2027-01-01",
                    }
                ],
            },
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "fp",
                        "rule_id": "SG001",
                        "reason": "reason",
                        "owner": "team",
                        "created_at": "bad",
                        "expires_at": "2027-01-01",
                    }
                ],
            },
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                self._write(value)
                with self.assertRaises(GovernanceError):
                    load_and_associate(self.root, (), evaluated_at="2026-06-01")

    def test_expiry_is_evaluated_on_fixed_date_and_expires_on_boundary(self):
        self._write(
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "fp",
                        "rule_id": "SG001",
                        "reason": "Known compatibility behavior.",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2026-06-01",
                    }
                ],
            }
        )
        finding = Finding(
            rule_id="SG001",
            rule_version="1",
            fingerprint_version="1",
            fingerprint="fp",
            severity="medium",
            confidence="high",
            primary_location=SourceRange("sample.py", 1, 0, 1, 1),
            related_locations=(),
            fact_description="fact",
            investigation_prompt="check",
        )

        before_expiry = load_and_associate(
            self.root, (finding,), evaluated_at="2026-05-31"
        )
        on_expiry = load_and_associate(
            self.root, (finding,), evaluated_at="2026-06-01"
        )

        self.assertEqual(before_expiry.evaluated_at, "2026-05-31")
        self.assertEqual(before_expiry.statuses[0][2], "accepted")
        self.assertEqual(on_expiry.evaluated_at, "2026-06-01")
        self.assertEqual(on_expiry.statuses[0][2], "expired")

    def test_rule_id_mismatch_stays_unregistered_and_unmatched(self):
        self._write(
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "fp",
                        "rule_id": "SG999",
                        "reason": "Different rule record.",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2999-01-01",
                    }
                ],
            }
        )
        finding = Finding(
            rule_id="SG001",
            rule_version="1",
            fingerprint_version="1",
            fingerprint="fp",
            severity="medium",
            confidence="high",
            primary_location=SourceRange("sample.py", 1, 0, 1, 1),
            related_locations=(),
            fact_description="fact",
            investigation_prompt="check",
        )

        result = load_and_associate(self.root, (finding,), evaluated_at="2026-06-01")

        self.assertEqual(result.statuses[0][2], "unregistered")
        self.assertEqual(len(result.unmatched), 1)

    def test_duplicate_current_locations_do_not_share_one_acceptance(self):
        self._write(
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "fp",
                        "rule_id": "SG001",
                        "reason": "Known compatibility behavior.",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2999-01-01",
                    }
                ],
            }
        )
        findings = tuple(
            Finding(
                rule_id="SG001",
                rule_version="1",
                fingerprint_version="1",
                fingerprint="fp",
                severity="medium",
                confidence="high",
                primary_location=SourceRange("sample.py", line, 0, line, 1),
                related_locations=(),
                fact_description="fact",
                investigation_prompt="check",
            )
            for line in (1, 20)
        )

        result = load_and_associate(self.root, findings, evaluated_at="2026-06-01")

        self.assertEqual([status for _, _, status in result.statuses], ["ambiguous", "ambiguous"])
        self.assertEqual(result.unmatched, ())

        single = load_and_associate(
            self.root, (findings[0],), evaluated_at="2026-06-01"
        )
        self.assertEqual(single.statuses[0][2], "accepted")

        self._write(
            {
                "schema_version": 1,
                "exceptions": [
                    {
                        "fingerprint": "fp",
                        "rule_id": "SG001",
                        "reason": "Expired compatibility behavior.",
                        "owner": "team",
                        "created_at": "2026-01-01",
                        "expires_at": "2026-06-01",
                    }
                ],
            }
        )
        expired = load_and_associate(
            self.root, findings, evaluated_at="2026-06-01"
        )
        self.assertEqual(
            [status for _, _, status in expired.statuses], ["ambiguous", "ambiguous"]
        )


if __name__ == "__main__":
    unittest.main()
