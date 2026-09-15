"""L10: JS/TS `no_external_callers` evaluation, pure in-memory."""

import hashlib
import json
import unittest

from spellguard.models import Snapshot, SourceFile
from spellguard.registry import parse_registry
from spellguard.windows import check_window

JS_RULE = {
    "schema_version": 1,
    "rules": [{"id": "TEMP-001", "classification": "temporary",
               "lifecycle": "ACTIVE", "reason": "Synthetic migration",
               "desired_state": "Remove helper",
               "window": "no_external_callers",
               "resolution_reason": None,
               "protected_symbol": {"path": "backend/helper.js",
                                    "symbol": "helper",
                                    "source_root": "backend"}}]}


def rule():
    return parse_registry(json.dumps(JS_RULE).encode()).rules[0]


def snapshot_of(files: dict):
    return Snapshot("synthetic", "working-tree", tuple(
        SourceFile(p, t, hashlib.sha256(t.encode()).hexdigest())
        for p, t in sorted(files.items())
    ))


DEFINITION = {'.js': "export function helper() { return 1 }\n",
              '.ts': "export function helper(): number { return 1 }\n"}
CALLER = {'.js': "import { helper } from \"./helper.js\"\n"
                 "export function main() { return helper() }\n",
          '.ts': "import { helper } from \"./helper\"\n"
                 "export function main(): number { return helper() }\n"}


class TestJavaScriptWindow(unittest.TestCase):
    def test_relative_named_import_is_external(self):
        files = {"backend/helper.js": "export function helper() { return 1 }\n",
                 "backend/use.js":
                 'import { helper } from "./helper.js"\n'
                 "export function main() { return helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            [(c.path, c.symbol) for c in result.consumers],
            [("backend/use.js", "main")])

    def test_relative_ts_named_import_is_external(self):
        files = {"backend/helper.ts": "export function helper(): number {\n"
                                      "    return 1 }\n",
                 "backend/main.ts":
                 'import { helper } from "./helper"\n'
                 "export function main(): number { return helper() }\n"}
        raw = json.loads(json.dumps(JS_RULE))
        raw["rules"][0]["protected_symbol"]["path"] = "backend/helper.ts"
        ts_rule = parse_registry(json.dumps(raw).encode()).rules[0]
        result = check_window(snapshot_of(files), ts_rule)
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)

    def test_relative_named_alias_keeps_source_identity(self):
        files = {"backend/helper.js":
                 "export function helper() { return 1 }\n",
                 "backend/use.js":
                 'import { helper as h } from "./helper.js"\n'
                 "export function main() { return h() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)
        self.assertEqual(
            [(c.path, c.symbol) for c in result.consumers],
            [("backend/use.js", "main")])

    def test_unrelated_export_renamed_to_target_is_not_a_caller(self):
        files = {"backend/helper.js":
                 "export function helper() { return 1 }\n"
                 "export function other() { return 2 }\n",
                 "backend/use.js":
                 'import { other as helper } from "./helper.js"\n'
                 "export function main() { return helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN", result.diagnostics)
        self.assertEqual(result.consumers, ())

    def test_relative_namespace_import_keeps_source_identity(self):
        files = {"backend/helper.js":
                 "export function helper() { return 1 }\n",
                 "backend/use.js":
                 'import * as helpers from "./helper.js"\n'
                 "export function main() { return helpers.helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)
        self.assertEqual(
            [(c.path, c.symbol) for c in result.consumers],
            [("backend/use.js", "main")])

    def test_named_default_import_keeps_source_identity(self):
        files = {"backend/helper.js":
                 "export default function helper() { return 1 }\n",
                 "backend/use.js":
                 'import localHelper from "./helper.js"\n'
                 "export function main() { return localHelper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "VIOLATED", result.diagnostics)
        self.assertEqual(
            [(c.path, c.symbol) for c in result.consumers],
            [("backend/use.js", "main")])

    def test_tsx_uses_tsx_grammar(self):
        files = {"backend/helper.tsx":
                 "export function helper() { return <div /> }\n"}
        raw = json.loads(json.dumps(JS_RULE))
        raw["rules"][0]["protected_symbol"]["path"] = "backend/helper.tsx"
        tsx_rule = parse_registry(json.dumps(raw).encode()).rules[0]
        result = check_window(snapshot_of(files), tsx_rule)
        self.assertEqual(result.status, "OPEN", result.diagnostics)
        self.assertTrue(result.complete)

    def test_same_file_call_is_not_external(self):
        files = {"backend/helper.js":
                 "export function helper() { return helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())

    def test_local_shadowing_disables_detection(self):
        files = {"backend/helper.js": "export function helper() { return 1 }\n",
                 "backend/use.js":
                 "import { helper } from \"./helper.js\"\n"
                 "function use(helper) { return helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN", result.consumers)

    def test_require_call_is_not_definite(self):
        files = {"backend/helper.js": "export function helper() { return 1 }\n",
                 "backend/use.js":
                 "const helper = require(\"./helper.js\")\n"
                 "function main() { return helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")

    def test_bare_package_import_never_identifies(self):
        files = {"backend/helper.js": "export function helper() { return 1 }\n",
                 "backend/use.js":
                 'import { helper } from "my-package"\n'
                 "function main() { return helper() }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")

    def test_dynamic_import_is_not_a_definite_call(self):
        files = {"backend/helper.js": "export function helper() { return 1 }\n",
                 "backend/use.js":
                 "async function main() {\n"
                 "  const mod = await import('./helper.js')\n"
                 "  return mod.helper()\n}\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")

    def test_function_value_transfer_is_not_call(self):
        files = {"backend/helper.js":
                 "export function helper() { return 1 }\n",
                 "backend/use.js":
                 'import { helper } from "./helper.js"\n'
                 "const copy = helper\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")

    def test_missing_protected_function_is_symbol_unverified(self):
        files = {"backend/helper.js": "export function other() { return 1 }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED",
                      {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_js_syntax_error_is_unverified(self):
        files = {"backend/helper.js": "function hello(:\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("JS_SYNTAX_ERROR", {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_resolved_rule_is_reported_not_evaluated(self):
        raw = json.loads(json.dumps(JS_RULE))
        raw["rules"][0]["lifecycle"] = "RESOLVED"
        raw["rules"][0]["resolution_reason"] = "removed"
        result = check_window(
            snapshot_of({"backend/helper.js":
                         "export function helper() { return 1 }\n"}),
            parse_registry(json.dumps(raw).encode()).rules[0])
        self.assertEqual(result.status, "RESOLVED")

    def test_removing_caller_recovers_open(self):
        files = {"backend/helper.js": "export function helper() { return 1 }\n",
                 "backend/use.js": "export function main() { return 2 }\n"}
        result = check_window(snapshot_of(files), rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())



if __name__ == "__main__":
    unittest.main()
