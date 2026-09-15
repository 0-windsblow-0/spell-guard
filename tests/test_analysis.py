import ast
import json
import sys
import unittest
from unittest.mock import patch

from spellguard.analysis import encode_ast, parse_snapshot
from spellguard.models import CollectedSnapshot, Coverage, Snapshot, SourceFile


def collected(*contents):
    files = tuple(
        SourceFile("file{}.py".format(index), content, "digest-{}".format(index))
        for index, content in enumerate(contents)
    )
    count = len(files)
    return CollectedSnapshot(
        Snapshot("snapshot", "working-tree", files),
        Coverage(count, count, 0, 0, 0, (), True),
    )


class AnalysisTest(unittest.TestCase):
    def test_syntax_failure_preserves_valid_facts_and_marks_coverage_incomplete(self):
        result = parse_snapshot(
            collected(
                "def okay():\n    return 1\n",
                "def broken(:\n    return 2\n",
            )
        )

        self.assertEqual(result.coverage.eligible_files, 2)
        self.assertEqual(result.coverage.analyzed_files, 1)
        self.assertEqual(result.coverage.failed_files, 1)
        self.assertFalse(result.coverage.complete)
        self.assertEqual(len(result.facts), 1)
        self.assertEqual(result.coverage.diagnostics[0].code, "PYTHON_SYNTAX_ERROR")
        self.assertEqual(result.coverage.diagnostics[0].path, "file1.py")

    def test_returns_inside_nested_functions_and_classes_keep_scope_boundary(self):
        result = parse_snapshot(
            collected(
                """def outer():
    try:
        call()
    except Exception:
        def inner():
            return 0
        class Nested:
            def method(self):
                return []
        return 1
"""
            )
        )

        returns = {(fact.symbol, fact.normalized_structure["in_exception_handler"]) for fact in result.facts}
        self.assertIn(("outer.inner", False), returns)
        self.assertIn(("outer.Nested.method", False), returns)
        self.assertIn(("outer", True), returns)

    def test_recursion_failure_preserves_other_files_and_marks_coverage_incomplete(self):
        with patch.object(
            ast,
            "parse",
            side_effect=[
                ast.parse("def okay():\n    return 1\n"),
                RecursionError("controlled parser recursion failure"),
            ],
        ):
            result = parse_snapshot(
                collected(
                    "def okay():\n    return 1\n",
                    "value = 1\n",
                )
            )

        self.assertFalse(result.coverage.complete)
        self.assertEqual(result.coverage.analyzed_files, 1)
        self.assertEqual(result.coverage.failed_files, 1)
        self.assertEqual(len(result.facts), 1)
        self.assertEqual(result.coverage.diagnostics[0].code, "PYTHON_PARSE_FAILED")
        self.assertEqual(result.coverage.diagnostics[0].path, "file1.py")

    @unittest.skipUnless(sys.version_info >= (3, 14), "requires Python 3.14+")
    def test_modern_python_syntax_parses_and_keeps_runtime_ast_identity(self):
        source = """class Box[T]:
    def get(self, value: T) -> T:
        match value:
            case 1:
                return value
        return value

def handle(value):
    try:
        return value
    except* ValueError:
        pass

type Alias[T] = list[T]
"""

        result = parse_snapshot(collected(source))
        encoded = encode_ast(ast.parse(source))

        self.assertTrue(result.coverage.complete)
        self.assertEqual(result.coverage.diagnostics, ())
        self.assertEqual(
            {fact.syntax_model_version for fact in result.facts},
            {"python-ast-3.14"},
        )
        self.assertEqual(len([fact for fact in result.facts if fact.fact_type == "return"]), 3)
        self.assertIn('"node":"Match"', json.dumps(encoded, separators=(",", ":")))
        self.assertIn('"node":"TypeAlias"', json.dumps(encoded, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
