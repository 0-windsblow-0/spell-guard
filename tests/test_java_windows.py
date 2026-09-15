"""L20: Java `no_external_callers` evaluation, pure in-memory."""

import hashlib
import json
import unittest

from spellguard.models import Snapshot, SourceFile
from spellguard.registry import parse_registry
from spellguard.windows import check_window

JAVA_RULE = {
    "schema_version": 1,
    "rules": [{"id": "TEMP-001", "classification": "temporary",
               "lifecycle": "ACTIVE", "reason": "Synthetic",
               "desired_state": "Remove direct update",
               "window": "no_external_callers",
               "resolution_reason": None,
               "protected_symbol": {"path": "backend/inventory/InventoryService.java",
                                    "symbol": "adjust",
                                    "source_root": "backend"}}]}


def rule(path="backend/inventory/InventoryService.java"):
    raw = json.loads(json.dumps(JAVA_RULE))
    raw["rules"][0]["protected_symbol"]["path"] = path
    return parse_registry(json.dumps(raw).encode()).rules[0]


def snapshot_of(files: dict):
    return Snapshot("synthetic", "working-tree", tuple(
        SourceFile(p, t, hashlib.sha256(t.encode()).hexdigest())
        for p, t in sorted(files.items())
    ))

DEFINITION = '''package com.example.demo;

public class InventoryService {
    public static int adjust(long id, long qty) {
        if (qty < 0) { return -1; }
        return (int)(id + qty);
    }
}
'''


class JavaWindowTest(unittest.TestCase):
    def test_explicit_type_import_is_external(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\nimport com.example.demo.InventoryService;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return InventoryService.adjust(1, 2); }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)

    def test_fully_qualified_is_external(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return com.example.demo.InventoryService.adjust(1, 2); }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)

    def test_static_import_plain_call_is_external(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'import static com.example.demo.InventoryService.adjust;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return adjust(1, 2); }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)

    def test_same_definition_file_call_is_not_external(self):
        files = {"backend/inventory/InventoryService.java":
                 '''package com.example.demo;

public class InventoryService {
    public static int adjust(long id, long qty) { return adjust(id, qty); }
}
'''}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")
    def test_shadowing_parameter_disables_detection(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'import com.example.demo.InventoryService;\n\n'
                 'public class Runner {\n'
                 '    public static int adjust(InventoryService service) {\n'
                 '        return service.adjust(1, 2);\n'
                 '    }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN", result.consumers)

        self.assertEqual(result.status, "OPEN", result.consumers)

    def test_unrelated_type_name_import_never_fires(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'import com.other.Service;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return Service.adjust(1, 2); }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN", result.consumers)

    def test_wildcard_static_import_is_unknown(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'import static com.example.demo.InventoryService.*;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return adjust(1, 2); }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED", result.diagnostics)
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE",
                      {d.code for d in result.diagnostics})

    def test_instance_method_or_method_ref_is_unknown(self):
        # dispatching through an instance is not provable via direct static
        # syntax; a plain `Service` substring call without import stays OPEN.
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return 42; }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN", result.consumers)

    def test_missing_protected_method_is_symbol_unverified(self):
        files = {"backend/inventory/InventoryService.java":
                 'package com.example.demo;\n\n'
                 'public class InventoryService {\n'
                 '    public static int otherMethod() { return 1; }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)

    def test_java_syntax_error_is_unverified(self):
        files = {"backend/inventory/InventoryService.java": "class broken(\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertIn("JAVA_SYNTAX_ERROR", {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_resolved_rule_not_evaluated(self):
        raw = json.loads(json.dumps(JAVA_RULE))
        raw["rules"][0]["lifecycle"] = "RESOLVED"
        raw["rules"][0]["resolution_reason"] = "removed"
        result = check_window(
            snapshot_of({"backend/inventory/InventoryService.java": DEFINITION}),
            parse_registry(json.dumps(raw).encode()).rules[0])
        self.assertEqual(result.status, "RESOLVED")

    def test_removing_caller_recovers_open(self):
        files = {"backend/inventory/InventoryService.java": DEFINITION,
                 "backend/run/Runner.java":
                 'package com.example.demomain;\n\n'
                 'public class Runner {\n'
                 '    public static int main() { return 42; }\n'
                 '}\n'}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())


if __name__ == "__main__":
    unittest.main()
