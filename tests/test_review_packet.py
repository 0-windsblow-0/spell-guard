"""A-A01: fixed review packets built from in-memory snapshots.

Assisted review prepares a deterministic evidence packet for a fixed question
about a new persistent-state write path in a Go inventory sample. Snapshots
are synthetic; target source is parsed only, never executed.
"""

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from spellguard.models import ChangeSet
from spellguard.review_packet import build_review_packet
from spellguard.registry import parse_registry

GOMOD = "module example.test/inventory\n\nrequire gorm.io/gorm v1.25.0\n"

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


def snap(paths: dict):
    return {
        path: hashlib.sha256(content.encode()).hexdigest()
        for path, content in sorted(files_of(paths).items())
    }


def _model_shell():  # helper unused after rename; kept for clarity
    return {}


REGISTRY = {
    "schema_version": 1,
    "rules": [{
        "id": "TEMP-001", "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": "Synthetic: shared adjust path is the sanctioned writer.",
        "desired_state": "Remove direct update after migration.",
        "protected_symbol": {"path": "backend/inventory/adjust.go",
                             "symbol": "Adjust", "source_root": "backend"},
        "window": "no_external_callers", "resolution_reason": None,
    }],
}


def files_of(content_by_path: dict):
    return content_by_path


def snapshot_files(content_by_path: dict, prefix="backend"):
    out = []
    for path, content in sorted(content_by_path.items()):
        wrapped = "/".join([prefix, path]) if not path.startswith(prefix) else path
        out.append((wrapped, content))
    digest = hashlib.sha256
    from spellguard.models import SourceFile
    return [SourceFile(p, c, digest(c.encode()).hexdigest()) for p, c in out]


class ReviewPacketTest(unittest.TestCase):
    """Build a packet from an in-memory ChangeSet and Go snapshots."""

    def setUp(self):
        self.before = {
            "inventory/model.go": BEFORE_MODEL,
            "inventory/adjust.go": BEFORE_ADJUST,
            "inventory/import.go": BEFORE_IMPORT,
        }
        self.after = {
            "inventory/model.go": BEFORE_MODEL,
            "inventory/adjust.go": BEFORE_ADJUST,
            "inventory/import.go": AFTER_IMPORT,
        }

    def _packet(self, before=None, after=None):
        base = snapshot_files(before if before is not None else self.before)
        current = snapshot_files(after if after is not None else self.after)
        registry = parse_registry(json.dumps(REGISTRY).encode())
        # Changes: inventory/import.go modified
        changes = ChangeSet(
            base_commit="test-base",
            target_snapshot_identity=current[0].digest,
            added_paths=(),
            modified_paths=("backend/inventory/import.go",),
            deleted_paths=(),
            renames=(),
        )
        return build_review_packet(
            base_snap=self._wrap(base),
            current_snap=self._wrap(current),
            changes=changes,
            registry=registry)

    def _wrap(self, files):
        from spellguard.models import Snapshot
        return Snapshot("test-base", "working-tree", tuple(files))

    def test_base_and_after_evidence_present(self):
        packet = self._packet()
        self.assertEqual(packet["schema_version"], 1)
        self.assertEqual(packet["kind"], "state_write_review")
        # files include each Go source file
        paths = [f["path"] for f in packet["files"]]
        self.assertIn("backend/inventory/import.go", paths)
        self.assertIn("backend/inventory/adjust.go", paths)
        import_file = next(f for f in packet["files"]
                           if f["path"] == "backend/inventory/import.go")
        self.assertIn("Import", import_file["after"]["content"])
        self.assertNotIn("Import", import_file["before"]["content"])
        self.assertGreater(len(import_file["changed_after_lines"]), 0)

    def test_diff_and_change_lines_accurate(self):
        packet = self._packet()
        import_file = next(f for f in packet["files"]
                           if f["path"] == "backend/inventory/import.go")
        self.assertIn("+func (s *Service) Import", import_file["diff"])
        self.assertEqual(import_file["changed_after_lines"], [2, 3, 4, 5])

    def test_unmodified_files_have_empty_diff_and_no_change_lines(self):
        packet = self._packet()
        model = next(f for f in packet["files"]
                     if f["path"] == "backend/inventory/model.go")
        self.assertEqual(model["diff"], "")
        self.assertEqual(model["changed_after_lines"], [])

    def test_deterministic_canonical_bytes(self):
        first = self._packet()
        second = self._packet()
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")).encode(),
            json.dumps(second, sort_keys=True, separators=(",", ":")).encode())
        # no time, no cwd path, no random ID
        raw = json.dumps(first, sort_keys=True)
        for forbidden in ("/" + "Users/", "/private/var", "tempfile"):
            self.assertNotIn(forbidden, raw)

    def test_no_registry_renders_confirmed_intent_empty(self):
        packet = self._packet()
        registry = None
        packet = build_review_packet(
            base_snap=self._wrap(snapshot_files(self.before)),
            current_snap=self._wrap(snapshot_files(self.after)),
            changes=ChangeSet(
                base_commit="abc", target_snapshot_identity="x",
                added_paths=(), modified_paths=("backend/inventory/import.go",),
                deleted_paths=(), renames=()),
            registry=None)
        self.assertEqual(packet["confirmed_intent"], [])

    def test_budget_oversize_rejects_with_exit_2_shape(self):
        # Oversize source content should lead to atomic exclusion and a
        # bounded error; test via a helper that replaces files with a large
        # filler model > 32 KiB
        big = self.before["inventory/model.go"] + "// pad\n" * 20000
        before = {**self.before}
        before["inventory/model.go"] = big
        after = {**self.after}
        after["inventory/model.go"] = big
        packet = self._packet(before=before, after=after)
        # oversized source should have been atomically excluded, not truncated
        paths = [f["path"] for f in packet["files"]]
        self.assertNotIn("backend/inventory/model.go", paths)
        self.assertGreater(packet["coverage"]["omitted_count"], 0)

    def test_no_go_changes_renders_empty_files(self):
        # exhaustive ChangeSet with no modification: contents before==after
        packet = self._packet(before=self.after, after=self.after)
        # nothing changed: import.go equal; no modified files → empty files
        changes = ChangeSet(
            base_commit="abc", target_snapshot_identity="x", added_paths=(),
            modified_paths=(), deleted_paths=(), renames=())
        packet = build_review_packet(
            base_snap=self._wrap(snapshot_files(self.after)),
            current_snap=self._wrap(snapshot_files(self.after)),
            changes=changes, registry=None)
        self.assertEqual(packet["files"], [])


if __name__ == "__main__":
    unittest.main()
