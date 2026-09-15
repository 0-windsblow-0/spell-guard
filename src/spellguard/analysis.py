"""Parse collected Python and Go files into deterministic structural facts."""

import ast
import hashlib
import json
import sys
from dataclasses import replace
from typing import Any, Dict, List, Sequence, Tuple

from .models import AnalysisResult, CollectedSnapshot, Diagnostic, Fact, SourceRange
from .go_analysis import parse_go


SYNTAX_MODEL_VERSION = "python-ast-{}.{}".format(sys.version_info[0], sys.version_info[1])
_POSITION_FIELDS = {"lineno", "col_offset", "end_lineno", "end_col_offset", "type_comment"}
_LITERAL_KINDS = {"constant", "list", "tuple", "set", "dict"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def encode_ast(value: Any) -> Any:
    """Encode Python 3.9 AST values without positions or comment text."""

    if value is None:
        return {"tag": "none"}
    if isinstance(value, bool):
        return {"tag": "bool", "value": value}
    if isinstance(value, int):
        return {"tag": "int", "value": str(value)}
    if isinstance(value, float):
        return {"tag": "float", "hex": value.hex()}
    if isinstance(value, complex):
        return {
            "tag": "complex",
            "real_hex": value.real.hex(),
            "imag_hex": value.imag.hex(),
        }
    if isinstance(value, str):
        return {"tag": "str", "value": value}
    if isinstance(value, bytes):
        return {"tag": "bytes", "hex": value.hex()}
    if value is Ellipsis:
        return {"tag": "ellipsis"}
    if isinstance(value, ast.AST):
        return {
            "node": type(value).__name__,
            "fields": [
                [name, encode_ast(item)]
                for name, item in ast.iter_fields(value)
                if name not in _POSITION_FIELDS
            ],
        }
    if isinstance(value, list):
        return [encode_ast(item) for item in value]
    if isinstance(value, tuple):
        return {"tag": "tuple", "items": [encode_ast(item) for item in value]}
    if isinstance(value, set):
        items = [encode_ast(item) for item in value]
        items.sort(key=_canonical_json)
        return {"tag": "set", "items": items}
    if isinstance(value, dict):
        items = [[encode_ast(key), encode_ast(item)] for key, item in value.items()]
        items.sort(key=lambda pair: _canonical_json(pair[0]))
        return {"tag": "dict", "items": items}
    raise TypeError("unsupported AST value: {}".format(type(value).__name__))


def _literal_structure(node: ast.AST) -> Dict[str, Any]:
    if isinstance(node, ast.Constant):
        return {"kind": "constant", "value": encode_ast(node.value)}
    if isinstance(node, ast.List):
        items = [_literal_structure(item) for item in node.elts]
        if all(item["kind"] in _LITERAL_KINDS for item in items):
            return {"kind": "list", "items": items}
    if isinstance(node, ast.Tuple):
        items = [_literal_structure(item) for item in node.elts]
        if all(item["kind"] in _LITERAL_KINDS for item in items):
            return {"kind": "tuple", "items": items}
    if isinstance(node, ast.Set):
        items = [_literal_structure(item) for item in node.elts]
        if all(item["kind"] in _LITERAL_KINDS for item in items):
            items.sort(key=_canonical_json)
            return {"kind": "set", "items": items}
    if isinstance(node, ast.Dict):
        if all(key is not None for key in node.keys):
            pairs = [
                [_literal_structure(key), _literal_structure(value)]
                for key, value in zip(node.keys, node.values)
            ]
            if all(
                key["kind"] in _LITERAL_KINDS and value["kind"] in _LITERAL_KINDS
                for key, value in pairs
            ):
                pairs.sort(key=lambda pair: _canonical_json(pair[0]))
                return {"kind": "dict", "items": pairs}
    return {"kind": "expression", "ast": encode_ast(node)}


def _return_structure(node: ast.Return) -> Dict[str, Any]:
    if node.value is None:
        return {"kind": "bare-return"}
    return _literal_structure(node.value)


def _source_range(path: str, node: ast.AST) -> SourceRange:
    start_line = getattr(node, "lineno", 1)
    start_column = getattr(node, "col_offset", 0)
    end_line = getattr(node, "end_lineno", start_line)
    end_column = getattr(node, "end_col_offset", start_column)
    return SourceRange(path, start_line, start_column, end_line, end_column)


class _FactVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source: str):
        self.path = path
        self.source = source
        self.scope: List[str] = []
        self.exception_depth = 0
        self.facts: List[Fact] = []

    def _symbol(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        previous_depth = self.exception_depth
        self.scope.append(name)
        self.exception_depth = 0
        for child in getattr(node, "body", ()):  # FunctionDef, AsyncFunctionDef, ClassDef
            self.visit(child)
        self.scope.pop()
        self.exception_depth = previous_depth

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def _is_elif(self, node: ast.If) -> bool:
        lines = self.source.splitlines()
        if not lines or node.lineno < 1 or node.lineno > len(lines):
            return False
        text = lines[node.lineno - 1].lstrip()
        return text.startswith("elif") and (
            len(text) == 4 or text[4] in (" ", "\t", "(")
        )

    def _if_chain_structure(self, nodes: Sequence[ast.If]) -> Dict[str, Any]:
        arms = []
        for item in nodes:
            arms.append(
                {
                    "test": encode_ast(item.test),
                    "body": [encode_ast(statement) for statement in item.body],
                }
            )
        final_else = nodes[-1].orelse
        return {
            "arms": arms,
            "else": [encode_ast(statement) for statement in final_else],
        }

    def _scalar_literal(self, node: ast.AST) -> Any:
        if not isinstance(node, ast.Constant):
            return None
        value = node.value
        if value is None or not isinstance(value, (str, int, float, complex, bool)):
            return None
        return encode_ast(value)

    def _literal_case_comparison(self, node: ast.AST) -> Any:
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
            return None
        if not isinstance(node.ops[0], ast.Eq):
            return None
        if not isinstance(node.left, (ast.Name, ast.Attribute)):
            return None
        literal = self._scalar_literal(node.comparators[0])
        if literal is None:
            return None
        return {
            "subject": encode_ast(node.left),
            "value": literal,
        }

    def _literal_case_condition(self, node: ast.AST) -> Any:
        comparison = self._literal_case_comparison(node)
        if comparison is not None:
            return {"kind": "comparison", "value": comparison}
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            comparisons = [self._literal_case_comparison(item) for item in node.values]
            if len(comparisons) >= 2 and all(item is not None for item in comparisons):
                return {"kind": "and", "values": comparisons}
        return None

    def _record_nested_literal_cases(self, node: ast.If) -> None:
        outer = self._literal_case_condition(node.test)
        if outer is None:
            return
        nested = list(child for child in node.body if isinstance(child, ast.If))
        if not (
            len(node.orelse) == 1
            and isinstance(node.orelse[0], ast.If)
            and self._is_elif(node.orelse[0])
        ):
            nested.extend(child for child in node.orelse if isinstance(child, ast.If))
        for child in nested:
            inner = self._literal_case_condition(child.test)
            if inner is None:
                continue
            self.facts.append(
                Fact(
                    path=self.path,
                    symbol=self._symbol(),
                    source_range=_source_range(self.path, node),
                    fact_type="nested_literal_case",
                    syntax_model_version=SYNTAX_MODEL_VERSION,
                    normalized_structure={"outer": outer, "inner": inner},
                    evidence=self.source.splitlines()[node.lineno - 1].strip(),
                    related_ranges=(_source_range(self.path, child),),
                )
            )

    def visit_If(self, node: ast.If) -> None:
        chain = [node]
        current = node
        while (
            len(current.orelse) == 1
            and isinstance(current.orelse[0], ast.If)
            and self._is_elif(current.orelse[0])
        ):
            current = current.orelse[0]
            chain.append(current)

        if len(chain) >= 2:
            self.facts.append(
                Fact(
                    path=self.path,
                    symbol=self._symbol(),
                    source_range=_source_range(self.path, node),
                    fact_type="if_chain",
                    syntax_model_version=SYNTAX_MODEL_VERSION,
                    normalized_structure=self._if_chain_structure(chain),
                    evidence=self.source.splitlines()[node.lineno - 1].strip(),
                )
            )

        for item in chain:
            self._record_nested_literal_cases(item)
            for child in item.body:
                self.visit(child)
        if chain[-1].orelse:
            for child in chain[-1].orelse:
                self.visit(child)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.exception_depth += 1
        for child in node.body:
            self.visit(child)
        self.exception_depth -= 1

    def visit_Return(self, node: ast.Return) -> None:
        location = _source_range(self.path, node)
        lines = self.source.splitlines()
        evidence = lines[location.start_line - 1].strip() if lines else ""
        self.facts.append(
            Fact(
                path=self.path,
                symbol=self._symbol(),
                source_range=location,
                fact_type="return",
                syntax_model_version=SYNTAX_MODEL_VERSION,
                normalized_structure={
                    "in_exception_handler": self.exception_depth > 0,
                    "value": _return_structure(node),
                },
                evidence=evidence,
            )
        )
        self.generic_visit(node)


def _sort_diagnostics(diagnostics: Sequence[Diagnostic]) -> Tuple[Diagnostic, ...]:
    return tuple(sorted(diagnostics, key=lambda item: (item.path or "", item.code, item.message)))


def _sort_facts(facts: Sequence[Fact]) -> Tuple[Fact, ...]:
    return tuple(
        sorted(
            facts,
            key=lambda item: (
                item.path,
                item.source_range.start_line,
                item.source_range.start_column,
                item.symbol,
                item.fact_type,
            ),
        )
    )


def parse_snapshot(collected: CollectedSnapshot) -> AnalysisResult:
    """Parse all readable files and preserve collection diagnostics and counts."""

    facts: List[Fact] = []
    diagnostics = list(collected.coverage.diagnostics)
    parse_failures = 0

    for source_file in collected.snapshot.files:
        if source_file.path.endswith(".go"):
            try:
                facts.extend(parse_go(source_file.path, source_file.content))
            except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as error:
                parse_failures += 1
                code = "GO_SYNTAX_ERROR" if isinstance(error, SyntaxError) else "GO_PARSE_FAILED"
                diagnostics.append(Diagnostic(code, str(error), source_file.path))
            continue
        try:
            tree = ast.parse(source_file.content, filename=source_file.path, mode="exec")
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as error:
            parse_failures += 1
            code = "PYTHON_SYNTAX_ERROR" if isinstance(error, SyntaxError) else "PYTHON_PARSE_FAILED"
            diagnostics.append(Diagnostic(code, str(error), source_file.path))
            continue

        visitor = _FactVisitor(source_file.path, source_file.content)
        visitor.visit(tree)
        facts.extend(visitor.facts)

    if parse_failures > collected.coverage.analyzed_files:
        raise ValueError("analysis received inconsistent coverage counts")

    coverage = replace(
        collected.coverage,
        analyzed_files=collected.coverage.analyzed_files - parse_failures,
        failed_files=collected.coverage.failed_files + parse_failures,
        diagnostics=_sort_diagnostics(diagnostics),
        complete=collected.coverage.complete and parse_failures == 0,
    )
    return AnalysisResult(facts=_sort_facts(facts), coverage=coverage)
