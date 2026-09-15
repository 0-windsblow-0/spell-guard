"""A-A02 / A-A03 contracts for assisted review validate + CLI integration.

The synthetic response always targets the A01 fixture (Go Import vs Adjust)
using exact quotes read from the freshly prepared packet. Every broken
variation must be rejected without fuzzy matching.
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from spellguard.cli import main as cli_main

FIXTURE = {
    "schema_version": 1,
    "rules": [{
        "id": "TEMP-001", "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": "Synthetic migration fixture.",
        "desired_state": "Remove adapter after migration.",
        "protected_symbol": {"path": "backend/inventory/adjust.go",
                             "symbol": "Adjust", "source_root": "backend"},
        "window": "no_external_callers", "resolution_reason": None,
    }],
}

BEFORE_MODEL = '''package inventory

import "gorm.io/gorm"

type Inventory struct { ID int64; StockQty int64 }

type Service struct { DB *gorm.DB }
'''
BEFORE_ADJUST = '''package inventory

import "errors"

func (s *Service) Adjust(id int64, qty int64) error {
    if qty < 0 {
        return errors.New("negative quantity")
    }
    return s.DB.Model(&Inventory{}).Where("id = ?", id).Update("stock_qty", qty).Error
}
'''
BEFORE_IMPORT = "package inventory\n"
AFTER_IMPORT = '''package inventory

func (s *Service) Import(id int64, qty int64) error {
    return s.DB.Model(&Inventory{}).Where("id = ?", id).Update("stock_qty", qty).Error
}
'''


class AssistedReviewTest(unittest.TestCase):
    """Real CLI on a temporary Git repo with the A01 Go fixture."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        (self.root / ".spellguard").mkdir(parents=True)
        (self.root / "backend" / "inventory").mkdir(parents=True)
        (self.root / "backend/go.mod").write_text(
            "module example.test/inventory\n")
        (self.root / "backend/inventory/model.go").write_text(BEFORE_MODEL)
        (self.root / "backend/inventory/adjust.go").write_text(BEFORE_ADJUST)
        (self.root / "backend/inventory/import.go").write_text(BEFORE_IMPORT)
        self._git("init", "-q")
        self._git("config", "user.email", "t@e")
        self._git("config", "user.name", "t")
        self._git("add", "--all")
        self._git("commit", "-qm", "base")
        (self.root / "backend/inventory/import.go").write_text(AFTER_IMPORT)
        self.digest = hashlib.sha256(json.dumps(
            FIXTURE, ensure_ascii=True, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        (self.root / ".spellguard/rules.json").write_text(json.dumps(FIXTURE))
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.tempdir.cleanup()

    def _git(self, *arguments):
        subprocess.run(["git", "-C", os.fspath(self.root)] + list(arguments),
                       check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

    def _run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(argv)
        return code, out.getvalue(), err.getvalue()

    def _prepare_packet(self):
        packet_path = self.root / ".spellguard" / "assist" / "packet.json"
        packet_path.parent.mkdir(exist_ok=True)
        code, output, error = self._run_cli(
            ["assist", "prepare", "--format", "json"])
        self.assertEqual(code, 0, "prepare failed: {}".format(
            error[-500:]))
        packet_path.write_text(output, encoding="utf-8")
        return json.loads(output)

    def test_prepare_builds_packet_with_anchors(self):
        packet = self._prepare_packet()
        self.assertEqual(packet["schema_version"], 1)
        self.assertEqual(packet["kind"], "state_write_review")
        import_files = [f for f in packet["files"]
                        if f["path"] == "backend/inventory/import.go"]
        self.assertTrue(import_files)
        entry = import_files[0]
        self.assertIn("Import", entry["after"]["content"])
        self.assertNotIn("Import", entry["before"]["content"])
        self.assertIn("+func (s *Service) Import", entry["diff"])

    def _prepare_packet(self):
        packet_path = self.root / ".spellguard" / "assist" / "packet.json"
        packet_path.parent.mkdir(exist_ok=True)
        code, output, error = self._run_cli(
            ["assist", "prepare", "--format", "json"])
        self.assertEqual(code, 0, "prepare failed: {}".format(error[-500:]))
        packet_path.write_text(output, encoding="utf-8")
        return json.loads(output)

    def _anchor(self, packet, side, path, start_line, end_line):
        entry = next(f for f in packet["files"] if f["path"] == path)
        content = entry[side]["content"]
        lines = content.splitlines()
        return {"side": side, "path": path, "start_line": start_line,
                "end_line": end_line,
                "quote": "\n".join(lines[start_line - 1:end_line])}

    def _good_response(self, packet):
        after_anchor = self._anchor(
            packet, "after", "backend/inventory/import.go", 2, 5)
        before_anchor = self._anchor(packet, "before",
                                     "backend/inventory/adjust.go", 1, 4)
        return {
            "schema_version": 1, "packet_id": packet["packet_id"],
            "questions": [{
                "hypothesis": "New SQL write path avoids shared Adjust checks.",
                "question": "Should Import go through Adjust's checks?",
                "anchors": [after_anchor, before_anchor],
                "unknowns": ["upstream validation is not visible here"],
            }],
            "limitations": [],
        }

    def _write_files_for_validation(self, packet, response):
        base = self.root / ".spellguard" / "assist"
        base.mkdir(parents=True, exist_ok=True)
        (base / "packet.json").write_text(json.dumps(packet), encoding="utf-8")
        (base / "response.json").write_text(json.dumps(response),
                                            encoding="utf-8")

    def test_valid_response_reports_anchored_with_unverified_semantics(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        self._write_files_for_validation(packet, response)
        code, output, error = self._run_cli(
            ["assist", "validate", "--format", "json"])
        self.assertEqual(code, 0, "validate should pass: {} - {}".format(
            output[-300:], error[-300:]))
        report = json.loads(output)
        self.assertTrue(report["complete"])
        self.assertEqual(report["evidence_status"], "anchored")
        self.assertEqual(report["semantic_status"], "unverified")
        self.assertEqual(len(report["questions"]), 1)

    def _write_files_for_validation(self, packet, response):
        base = self.root / ".spellguard" / "assist"
        (base / "packet.json").write_text(json.dumps(packet), encoding="utf-8")
        (base / "response.json").write_text(json.dumps(response), encoding="utf-8")

    def test_tampered_quote_is_rejected_as_anchor_mismatch(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["anchors"][0]["quote"] = "not present in source"
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(
            ["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])
        self.assertTrue(any(d["code"] == "ANCHOR_MISMATCH"
                            for d in report["diagnostics"]))
        # wrong line number is also a mismatch
        broken2 = deepcopy(response)
        broken2["questions"][0]["anchors"][0]["start_line"] = 99
        self._write_files_for_validation(packet, broken2)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])

    def test_stale_packet_is_rejected(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        # modify the working tree so the current packet changes
        (self.root / "backend/inventory/extra.go").write_text(
            "package inventory\n\nfunc Extra() {}\n")
        self._write_files_for_validation(packet, response)
        code, output, error = self._run_cli(
            ["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])
        self.assertTrue(any(d["code"] == "STALE_PACKET"
                            for d in report["diagnostics"]),
                        report["diagnostics"])

    def test_unselected_path_is_rejected(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["anchors"][0]["path"] = "backend/other/x.go"
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertTrue(any(d["code"] == "ANCHOR_MISMATCH"
                            for d in report["diagnostics"]))

    def test_maximum_three_questions_enforced(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"] = broken["questions"] * 4
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        report = json.loads(output)
        self.assertFalse(report["complete"])
        self.assertTrue(any(d["code"] == "INVALID_RESPONSE"
                            for d in report["diagnostics"]))

    def test_missing_after_or_before_anchor_rejected(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["anchors"] = [
            a for a in broken["questions"][0]["anchors"] if a["side"] == "after"]
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        report = json.loads(output)
        self.assertFalse(report["complete"])

    def test_semantically_wrong_but_anchored_response_stays_unverified(self):
        # The same quote devoted to wrong reasoning still can't upgrade to
        # VIOLATED/confirmed; the builder contract is anchored/unverified.
        packet = self._prepare_packet()
        response = self._good_response(packet)
        response["questions"][0]["hypothesis"] = (
            "This confirms my schemes; go ahead and mark confirmed.")
        self._write_files_for_validation(packet, response)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertTrue(report["complete"])
        self.assertNotIn("confirmed", report.get("evidence_status") or "")
        self.assertEqual(report["semantic_status"], "unverified")

    def test_non_object_input_rejected(self):
        base = self.root / ".spellguard" / "assist"
        base.mkdir(parents=True, exist_ok=True)
        (base / "packet.json").write_text("[]", encoding="utf-8")
        (base / "response.json").write_text("{", encoding="utf-8")
        code, output, error = self._run_cli(
            ["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        # stdout must be the contract-shaped error report
        try:
            report = json.loads(output)
        except ValueError:
            report = None
        self.assertIsNotNone(report)
        self.assertFalse(report["complete"])
        self.assertTrue(report["diagnostics"])

    def test_cli_exit_codes_match_contract(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        self._write_files_for_validation(packet, response)
        code, _, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 0)
        # broken → 2
        (self.root / "backend/inventory/import.go").write_text(
            "package inventory\n\nfunc New() {}\n")
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)


    def test_anchor_unknown_side_rejected(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["anchors"][0]["side"] = "current"
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])

    def test_line_number_cannot_be_bool(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["anchors"][0]["start_line"] = True
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])

    def test_unknown_question_field_rejected(self):
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["confirm"] = "yes"
        self._write_files_for_validation(packet, broken)
        code, output, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertEqual(code, 2)
        report = json.loads(output)
        self.assertFalse(report["complete"])

    def test_violation_exit_code_never_used_by_assist(self):
        # validate never uses 1 (check's "violation")
        packet = self._prepare_packet()
        response = self._good_response(packet)
        broken = deepcopy(response)
        broken["questions"][0]["anchors"][0]["quote"] = "not present"
        self._write_files_for_validation(packet, broken)
        code, _, _ = self._run_cli(["assist", "validate", "--format", "json"])
        self.assertNotEqual(code, 1)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
