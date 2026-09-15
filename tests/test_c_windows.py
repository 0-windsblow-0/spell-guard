"""L30: C `no_external_callers` evaluation, pure in-memory."""

import hashlib
import json
import unittest

from spellguard.models import Snapshot, SourceFile
from spellguard.registry import parse_registry
from spellguard.windows import check_window

C_RULE = {
    "schema_version": 1,
    "rules": [{"id": "TEMP-001", "classification": "temporary",
               "lifecycle": "ACTIVE", "reason": "Synthetic",
               "desired_state": "Remove helper", "window": "no_external_callers",
               "resolution_reason": None,
               "protected_symbol": {"path": "backend/legacy/adapt.c",
                                    "symbol": "inventory_adjust",
                                    "source_root": "backend"}}]}


def rule():
    return parse_registry(json.dumps(C_RULE).encode()).rules[0]


def snapshot_of(files: dict):
    return Snapshot("synthetic", "working-tree", tuple(
        SourceFile(p, t, hashlib.sha256(t.encode()).hexdigest())
        for p, t in sorted(files.items())
    ))

DEFINITION = '''#include "adapt.h"

int inventory_adjust(long id, long qty) { return 0; }
'''
HEADER = 'int inventory_adjust(long id, long qty);\n'


class CWindowTest(unittest.TestCase):
    def test_include_with_prototype_is_external(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.c": DEFINITION,
                 "backend/legacy/use.c":
                 "#include \"adapt.h\"\n\nint use() { return inventory_adjust(1, 2); }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)

    def test_missing_header_is_symbol_unverified(self):
        files = {"backend/legacy/adapt.c": DEFINITION,
                 "backend/legacy/use.h":
                 "int inventory_adjust(long a, long b);\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)

    def test_static_definition_is_unknown(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.c":
                 'int inventory_adjust(long);  /* prototype */\n'
                 'static int inventory_adjust(long id, long qty) { return 0; }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED", result.consumers)

    def test_caller_without_header_include_is_open(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.c": DEFINITION,
                 "backend/other/use.c":
                 "int use() { return inventory_adjust(1, 2); }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")

    def test_duplicate_prototype_is_symbol_unverified(self):
        files = {"backend/legacy/adapt.h": HEADER + HEADER,
                 "backend/legacy/adapt.c": DEFINITION,
                 "backend/use.c":
                 '#include "adapt.h"\n\nint use() { return inventory_adjust(1, 2); }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED", result.diagnostics)

    def test_missing_protected_function_is_symbol_unverified(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.c":
                 '#include "adapt.h"\n\n'
                 'int other_fn() { return inventory_adjust(1, 2); }\n',
                 "backend/legacy/use.c":
                 '#include "adapt.h"\n\n'
                 'int use() { return inventory_adjust(1, 2); }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertEqual(result.consumers, ())

    def test_c_syntax_error_is_unverified(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.c": "int broken(:\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertIn("C_SYNTAX_ERROR", {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_resolved_rule_not_evaluated(self):
        raw = json.loads(json.dumps(C_RULE))
        raw["rules"][0]["lifecycle"] = "RESOLVED"
        raw["rules"][0]["resolution_reason"] = "removed"
        result = check_window(
            snapshot_of({"backend/legacy/adapt.c":
                         "#include \"adapt.h\"\nint inventory_adjust(long a) { return 0; }\n",
                         "backend/legacy/adapt.h": HEADER}),
            parse_registry(json.dumps(raw).encode()).rules[0])
        self.assertEqual(result.status, "RESOLVED")

    def test_removing_caller_recovers_open(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.c": DEFINITION,
                 "backend/other/use.c": "int use() { return 1; }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())


if __name__ == "__main__":
    unittest.main()
