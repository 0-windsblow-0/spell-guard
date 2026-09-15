"""Go Repair Window evaluation tests (G02): pure in-memory trees."""

import hashlib
import json
import unittest

from spellguard.models import Snapshot, SourceFile
from spellguard.registry import parse_registry
from spellguard.windows import check_window

GO_RULE = {
    "schema_version": 1,
    "rules": [{"id": "TEMP-001", "classification": "temporary",
               "lifecycle": "ACTIVE", "reason": "Synthetic migration",
               "desired_state": "Remove adapter",
               "window": "no_external_callers",
               "resolution_reason": None,
               "protected_symbol": {"path": "backend/legacy/adapt.go",
                                    "symbol": "Adapt",
                                    "source_root": "backend"}}]}
GOMOD = "backend/go.mod"


def rule(module="example.test/demo"):
    raw = json.loads(json.dumps(GO_RULE))
    return parse_registry(json.dumps(raw).encode()).rules[0]


def snapshot_of(files):
    return Snapshot("synthetic", "working-tree", tuple(
        SourceFile(p, text, hashlib.sha256(text.encode()).hexdigest())
        for p, text in sorted(files.items()))
    )


GOMOD = {GOMOD: "module example.test/demo\n"}
DEFINITION = {"backend/legacy/adapt.go":
              "package legacy\n\nfunc Adapt() int { return 0 }\n"}


class GoWindowTest(unittest.TestCase):
    def rule(self):
        return rule()

    def test_same_package_other_file_is_external(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go":
                 "package legacy\n\nfunc Run() int { return Adapt() }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "VIOLATED")
        self.assertTrue(result.complete)
        self.assertEqual([(c.path, c.symbol, c.lines) for c in result.consumers],
                         [("backend/legacy/use.go", "Run", (3,))])

    def test_same_file_call_is_NOT_external(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/adapt.go":
                 "package legacy\n\nfunc Adapt() int { return adaptHelper() }\n"
                 "func adaptHelper() int { return 0 }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())

    def test_other_package_same_name_no_identification_confusion(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/other/adapt.go":
                 "package other\n\nfunc Adapt() int { return 0 }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "OPEN")

    def test_module_import_selector_is_caller(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/cmd/main.go":
                 'package main\n\nimport "example.test/demo/legacy"\n\n'
                 "func run() int { return legacy.Adapt() }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(result.consumers[0].lines, (5,))

    def test_aliased_import_selector_is_caller(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/cmd/main.go":
                 'package main\n\nimport l "example.test/demo/legacy"\n\n'
                 "func run() int { return l.Adapt() }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(result.consumers[0].lines, (5,))

    def test_unrelated_package_import_never_detects(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/cmd/main.go":
                 'package main\n\nimport "example.test/demo/other"\n\n'
                 "func run() int { return other.Adapt() }\n",
                 "backend/other/adapt.go":
                 "package other\n\nfunc Adapt() int { return 0 }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "OPEN")

    def test_parameter_shadows_protected_name(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go":
                 "package legacy\n\nfunc Run(Adapt int) int { return Adapt }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "OPEN")

    def test_local_function_declaration_shadows_import(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go":
                 'package legacy\n\nimport l "example.test/demo/legacy"\n\n'
                 "func l () int { return 0 }\n"
                 "func Run() int { return l() }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "OPEN", result.consumers)

    def test_function_value_assignment_is_unsupported(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go":
                 "package legacy\n\nfunc run() func() int { return Adapt }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("UNSUPPORTED_REFERENCE", {d.code for d in result.diagnostics})

    def test_missing_protected_function_is_symbol_unverified(self):
        files = {**GOMOD,
                 "backend/legacy/adapt.go": "package legacy\n\nfunc other() {}\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED", {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_go_syntax_error_is_unverified(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go": "package legacy\nfunc broken(:\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("GO_SYNTAX_ERROR", {d.code for d in result.diagnostics})
        self.assertFalse(result.complete)

    def test_missing_gomod_is_module_unverified(self):
        files = dict(DEFINITION,
                     **{"backend/legacy/use.go":
                        "package legacy\n\nfunc Run() int { return Adapt() }\n"})
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("GO_MODULE_UNVERIFIED",
                      {d.code for d in result.diagnostics})

    def test_resolved_rule_requires_no_evaluation(self):
        raw = json.loads(json.dumps(GO_RULE))
        raw["rules"][0]["lifecycle"] = "RESOLVED"
        raw["rules"][0]["resolution_reason"] = "removed"
        result = check_window(snapshot_of({**GOMOD, **DEFINITION}),
                              parse_registry(json.dumps(raw).encode()).rules[0])
        self.assertEqual(result.status, "RESOLVED")

    def test_removing_caller_recovers_open(self):
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go": "package legacy\n\nfunc Run() int { return 1 }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())

    def test_long_file_stays_accurate(self):
        padding = "\n".join("// pad {}".format(i) for i in range(1000))
        files = {**GOMOD, **DEFINITION,
                 "backend/legacy/use.go":
                 "package legacy\n\n" + padding +
                 "\nfunc Run() int { return Adapt() }\n"}
        result = check_window(snapshot_of(files), self.rule())
        self.assertEqual(result.status, "VIOLATED")
        # line 1 package, 2 blank, 3..1002 pads, func line = 1003
        self.assertEqual(result.consumers[0].lines[0], 1003)


if __name__ == "__main__":
    unittest.main()
