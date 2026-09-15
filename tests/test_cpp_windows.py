"""L40: C++ `no_external_callers` evaluation, pure in-memory."""

import hashlib
import json
import unittest

from spellguard.models import Snapshot, SourceFile
from spellguard.registry import parse_registry
from spellguard.windows import check_window

CPP_RULE = {
    "schema_version": 1,
    "rules": [{"id": "TEMP-001", "classification": "temporary",
               "lifecycle": "ACTIVE", "reason": "Synthetic",
               "desired_state": "Remove helper",
               "window": "no_external_callers",
               "resolution_reason": None,
               "protected_symbol": {"path": "backend/legacy/adapt.cpp",
                                    "symbol": "inventory_adjust",
                                    "source_root": "backend"}}]}


def rule():
    return parse_registry(json.dumps(CPP_RULE).encode()).rules[0]


def snapshot_of(files: dict):
    return Snapshot("synthetic", "working-tree", tuple(
        SourceFile(p, t, hashlib.sha256(t.encode()).hexdigest())
        for p, t in sorted(files.items())
    ))

HEADER = 'int inventory_adjust(long id, long qty);\n'
DEFINITION = '''#include "adapt.h"

int inventory_adjust(long id, long qty) { return 0; }
'''


class CppWindowTest(unittest.TestCase):
    def test_include_with_prototype_is_external(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.cpp": DEFINITION,
                 "backend/legacy/use.cpp":
                 '#include "adapt.h"\n\nint use() { return inventory_adjust(1, 2); }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)

    def test_namespace_function_is_external(self):
        # namespace free function resolves via a matched header declaration
        # and namespace-qualification in a caller; currently the design
        # scope protects only a matched header prototype. When the protected
        # file sits inside namespace inv, a direct inv::call identifies.
        header = 'namespace inv { int inventory_adjust(long id, long qty); }\n'
        definition = '#include "adapt.h"\n\n' + \
                     'namespace inv { int inventory_adjust(long id, long qty) { return 0; } }\n'
        files = {"backend/legacy/adapt.h": header,
                 "backend/legacy/adapt.cpp": definition,
                 "backend/legacy/use.cpp":
                 '#include "adapt.h"\n\nint use() { return inv::inventory_adjust(1, 2); }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)
        self.assertEqual(
            [(c.path, c.symbol) for c in result.consumers],
            [("backend/legacy/use.cpp", "use")])

    def test_unqualified_namespace_call_is_unknown(self):
        header = 'namespace inv { int inventory_adjust(long id, long qty); }\n'
        definition = '#include "adapt.h"\n\n' + \
                     'namespace inv { int inventory_adjust(long id, long qty) { return 0; } }\n'
        files = {"backend/legacy/adapt.h": header,
                 "backend/legacy/adapt.cpp": definition,
                 "backend/legacy/use.cpp":
                 '#include "adapt.h"\n\n'
                 'int use() { return inventory_adjust(1, 2); }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED", result.diagnostics)
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE",
                      {d.code for d in result.diagnostics})

    def test_template_is_unknown(self):
        # A template definition is not a protectable free function.
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.cpp":
                 '#include "adapt.h"\n\ntemplate<class T> T inventory_adjust(T x) { return x; }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED", result.diagnostics)
        self.assertFalse(result.complete)
        self.assertIn("SYMBOL_UNVERIFIED",
                      {d.code for d in result.diagnostics})

    def test_caller_without_header_include_is_open(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.cpp": DEFINITION,
                 "backend/other/use.cpp":
                 "int use() { return inventory_adjust(1, 2); }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")

    def test_missing_protected_function_is_symbol_unverified(self):
        files = {"backend/legacy/adapt.h": 'int other_fn(void);\n',
                 "backend/legacy/adapt.cpp":
                 '#include "adapt.h"\n\nint other_fn() { return 0; }\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)

    def test_cpp_syntax_error_is_unverified(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.cpp": "int broken(:\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertIn("CPP_SYNTAX_ERROR", {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_resolved_rule_not_evaluated(self):
        raw = json.loads(json.dumps(CPP_RULE))
        raw["rules"][0]["lifecycle"] = "RESOLVED"
        raw["rules"][0]["resolution_reason"] = "removed"
        result = check_window(
            snapshot_of({"backend/legacy/adapt.cpp":
                         '#include "adapt.h"\n'
                         "int inventory_adjust(long a) { return 0; }\n",
                         "backend/legacy/adapt.h": HEADER}),
            parse_registry(json.dumps(raw).encode()).rules[0])
        self.assertEqual(result.status, "RESOLVED")

    def test_removing_caller_recovers_open(self):
        files = {"backend/legacy/adapt.h": HEADER,
                 "backend/legacy/adapt.cpp": DEFINITION,
                 "backend/other/use.cpp": "int use() { return 1; }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())


if __name__ == "__main__":
    unittest.main()
