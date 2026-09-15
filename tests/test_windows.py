"""no_external_callers window checks, pure AST over in-memory snapshots (R03).

Cases mirror the R03 table plus B05/B06/B09/B21 extras. The window checker must
never treat "cannot parse" as "no dependency": every unknown boundary reports
UNVERIFIED with a diagnostic, and direct calls inside tests/generated/dead
branches are structural evidence, never silently excluded.
"""

import hashlib
import json
import os
import pathlib
import tempfile
import unittest

from spellguard.models import Snapshot, SourceFile
from spellguard.registry import parse_registry
from spellguard.windows import Consumer, check_window, WindowResult

FIXTURE = {
    "schema_version": 1,
    "rules": [{
        "id": "TEMP-001",
        "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": "Synthetic fixture: compatibility helper retained during migration.",
        "desired_state": "Synthetic fixture: remove helper after migration.",
        "protected_symbol": {"path": "src/demo/workaround.py",
                             "symbol": "fallback", "source_root": "src"},
        "window": "no_external_callers",
        "resolution_reason": None,
    }],
}

RULE = parse_registry(json.dumps(FIXTURE).encode()).rules[0]

PACKAGE = {"src/demo/__init__.py": ""}
DEFINITION = {"src/demo/workaround.py": "def fallback():\n    return 0\n"}


def snapshot_of(files: dict[str, str]) -> Snapshot:
    return Snapshot("fixture", "working-tree", tuple(
        SourceFile(path, content, hashlib.sha256(content.encode()).hexdigest())
        for path, content in sorted(files.items())
    ))


def resolved_rule() -> Snapshot:
    changed = json.loads(json.dumps(FIXTURE))
    changed["rules"][0]["lifecycle"] = "RESOLVED"
    changed["rules"][0]["resolution_reason"] = "helper removed after migration."
    return parse_registry(json.dumps(changed).encode()).rules[0]


class WindowContractTest(unittest.TestCase):
    def test_frozen_control_blocks_share_function_scope(self):
        for block in (
                "if True:", "for item in (1,):", "while True:",
                "with manager():"):
            with self.subTest(block=block):
                source = ("from demo.workaround import fallback as f\ndef run():\n"
                          "    " + block + "\n        def f():\n            return 2\n"
                          "    return f()\n")
                result = check_window(snapshot_of({**PACKAGE, **DEFINITION,
                    "src/demo/use.py": source}), RULE)
                self.assertEqual(result.status, "OPEN")
                self.assertTrue(result.complete)
                self.assertFalse(result.consumers)

    def test_resolved_rule_never_evaluates(self):
        result = check_window(
            snapshot_of({**PACKAGE, **DEFINITION,
                         "src/demo/use.py":
                         "from demo.workaround import fallback as f\n"
                         "def run():\n    return f()\n"}),
            resolved_rule())
        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(result.consumers, ())
        self.assertTrue(result.complete)

    def test_open_when_only_definition_file_calls(self):
        files = {**PACKAGE, "src/demo/workaround.py":
                 "def fallback():\n    return 0\n"
                 "def local():\n    return fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())
        self.assertTrue(result.complete)

    def test_open_when_comment_and_string_mention_symbol(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "# fallback()\n"
                 "s = 'fallback()'\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())
        self.assertTrue(result.complete)

    def test_open_other_module_same_symbol_name(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from other import fallback\n"
                 "def run():\n    return fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())
        self.assertTrue(result.complete)

    def test_from_alias_import_is_violated(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertTrue(result.complete)
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3,)),))

    def test_module_alias_import_is_violated(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "import demo.workaround as w\n"
                 "def run():\n    return w.fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3,)),))

    def test_absolute_import_is_violated(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "import demo.workaround\n"
                 "def run():\n    return demo.workaround.fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3,)),))

    def test_from_demo_import_workaround_module_alias(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo import workaround as w\n"
                 "def run():\n    return w.fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3,)),))

    def test_relative_package_import_is_violated(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from .workaround import fallback\n"
                 "def run():\n    return fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3,)),))

    def test_two_calls_same_function_single_consumer(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n"
                 "    a = f()\n"
                 "    b = f()\n"
                 "    return a + b\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3, 4)),))

    def test_two_functions_each_call_once_two_consumers(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def one():\n    return f()\n"
                 "def two():\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "one", (3,)),
             Consumer("src/demo/use.py", "two", (5,))))

    def test_comment_and_blank_line_move_are_still_same_consumer(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "\n"
                 "# moved comment\n"
                 "def run():\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (5,)),))

    def test_module_level_call_consumer_is_module(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "<module>", (2,)),))

    def test_parameter_shadowing_import_is_not_false_positive(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run(f):\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())
        self.assertTrue(result.complete)

    def test_passing_function_value_is_unsupported_reference(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return f\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE", {
            d.code for d in result.diagnostics})

    def test_conditional_import_is_unsupported_reference(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "if COND:\n"
                 "    from demo.workaround import fallback as f\n"
                 "def run():\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE", {
            d.code for d in result.diagnostics})

    def test_function_level_import_is_unsupported_reference(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "def run():\n"
                 "    from demo.workaround import fallback as f\n"
                 "    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE", {
            d.code for d in result.diagnostics})

    def test_function_level_import_of_other_symbol_is_open(self):
        files = {**PACKAGE,
                 "src/demo/workaround.py":
                 "def fallback():\n    return 0\n"
                 "def helper():\n    return 1\n",
                 "src/demo/use.py":
                 "def run():\n"
                 "    from demo.workaround import helper\n"
                 "    return helper()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertTrue(result.complete)
        self.assertEqual(result.consumers, ())

    def test_star_import_is_unsupported_reference(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import *\n"
                 "def run():\n    return fallback()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE", {
            d.code for d in result.diagnostics})

    def test_getattr_is_unsupported_reference(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "import demo.workaround as w\n"
                 "def run():\n    return getattr(w, 'fallback')()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE", {
            d.code for d in result.diagnostics})

    def test_module_level_rebinding_is_unsupported_reference(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "f = lambda: 1\n"
                 "def run():\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("UNSUPPORTED_REFERENCE", {
            d.code for d in result.diagnostics})

    def test_deleted_protected_function_is_symbol_unverified(self):
        files = {**PACKAGE,
                 "src/demo/workaround.py": "def helper():\n    return 0\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_missing_protected_file_is_symbol_unverified(self):
        files = {"src/demo/__init__.py": ""}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)

    def test_renamed_protected_function_is_symbol_unverified(self):
        files = {**PACKAGE,
                 "src/demo/workaround.py": "def fallback_renamed():\n    return 0\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_duplicate_protected_definition_is_symbol_unverified(self):
        files = {**PACKAGE,
                 "src/demo/workaround.py":
                 "def fallback():\n    return 0\n"
                 "def fallback():\n    return 1\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_decorated_protected_function_is_symbol_unverified(self):
        files = {**PACKAGE,
                 "src/demo/workaround.py":
                 "@deco\n"
                 "def fallback():\n    return 0\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_conditional_definition_is_symbol_unverified(self):
        files = {**PACKAGE,
                 "src/demo/workaround.py":
                 "if COND:\n"
                 "    def fallback():\n        return 0\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_missing_package_init_is_symbol_unverified(self):
        files = {"src/demo/workaround.py": "def fallback():\n    return 0\n",
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_violation_with_other_syntax_error_keeps_both_complete_false(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return f()\n",
                 "src/demo/broken.py": "def broken(:\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertFalse(result.complete)
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (3,)),))
        self.assertIn("PYTHON_SYNTAX_ERROR", {
            d.code for d in result.diagnostics})

    def test_no_violation_with_other_syntax_error_is_unverified(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/broken.py": "def broken(:\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("PYTHON_SYNTAX_ERROR", {
            d.code for d in result.diagnostics})

    def test_direct_call_in_dead_branch_is_structural_evidence(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "if False:\n    f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "<module>", (3,)),))

    def test_direct_call_in_test_file_is_not_implicitly_ignored(self):
        files = {**PACKAGE, **DEFINITION,
                 "tests/test_use.py":
                 "from demo.workaround import fallback as f\n"
                 "def test_use():\n    assert f() == 0\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("tests/test_use.py", "test_use", (3,)),))

    def test_removing_external_call_recovers_open(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return 1\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.consumers, ())
        self.assertTrue(result.complete)

    def test_duplicate_consumer_symbol_is_unverified_keeping_locations(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return f()\n"
                 "def run():\n    return 0\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "UNVERIFIED")
        self.assertFalse(result.complete)
        self.assertIn("SYMBOL_UNVERIFIED", {
            d.code for d in result.diagnostics})

    def test_f01_unrelated_binding_value_reference_is_open(self):
        """F01: a file with only `from json import dumps; formatter = dumps`
        must not make the whole check UNVERIFIED; unrelated bindings are not
        in scope for the protected symbol analysis."""
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from json import dumps\n"
                 "formatter = dumps\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertTrue(result.complete)
        self.assertEqual(result.consumers, ())

    def test_f02_local_def_shadows_import_is_open(self):
        """F02: import fallback as f, then define local f and call f() — the
        local def shadows the import, so this is not a protected call."""
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n"
                 "    def f():\n"
                 "        return 1\n"
                 "    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertTrue(result.complete)
        self.assertEqual(result.consumers, ())

    def test_f08_nested_independent_def_does_not_shadow_outer_scope(self):
        """F08: an inner def named f inside another nested function belongs to
        its own scope; it does NOT shadow the module import for run's call."""
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "from demo.workaround import fallback as f\n"
                 "def run():\n"
                 "    def unrelated():\n"
                 "        def f():\n"
                 "            return 2\n"
                 "    return f()\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (6,)),))

    def test_f09_unrelated_known_repo_module_does_not_pollute(self):
        """F09: `import demo.other as o; formatter = o` in a consumer file must
        not produce UNSUPPORTED_REFERENCE just because demo.other is a known
        in-repo module unrelated to the protected one."""
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/other.py": "value = 1\n",
                 "src/demo/use.py":
                 "import demo.other as o\n"
                 "formatter = o\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertTrue(result.complete)

    def test_unrelated_known_child_module_import_does_not_pollute(self):
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/other.py": "value = 1\n",
                 "src/demo/use.py":
                 "from demo import other\n"
                 "formatter = other\n"}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "OPEN")
        self.assertTrue(result.complete)
        self.assertEqual(result.consumers, ())

    def test_import_side_effects_are_never_executed(self):
        canary = pathlib.Path(tempfile.gettempdir()) / "spellguard-canary-r03"
        try:
            canary.unlink()
        except FileNotFoundError:
            pass
        files = {**PACKAGE, **DEFINITION,
                 "src/demo/use.py":
                 "import os\n"
                 "os.system('touch {}')\n"
                 "from demo.workaround import fallback as f\n"
                 "def run():\n    return f()\n".format(canary)}
        result = check_window(snapshot_of(files), RULE)
        self.assertEqual(result.status, "VIOLATED")
        self.assertFalse(canary.exists())
        self.assertEqual(
            result.consumers,
            (Consumer("src/demo/use.py", "run", (5,)),))


if __name__ == "__main__":
    unittest.main()
