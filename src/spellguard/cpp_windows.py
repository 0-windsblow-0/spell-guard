"""Pure in-memory C++ `no_external_callers` (L40 Design §7).

Reuses the C evaluator shape with C++ AST differences:
- Protect one unique free function or namespace free function (no template,
  no overload, no class method/constructor/operator).
- Definite calls require a matched project-header declaration via literal
  `#include`, with full namespace qualifier or explicit `using namespace::name`
  (`using` declaration, never wildcard). Class method, template, overload,
  ADL, wildcard using namespace, macro, function object and function
  pointers stay unknown.
No preprocessor/linker/compiler, no Point/column access.
"""

from __future__ import annotations

import bisect
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_cpp

from .models import Diagnostic, Snapshot
from .registry import TemporaryRule
from .windows import Consumer, MODULE_SCOPE, WindowResult

LANGUAGE = Language(tree_sitter_cpp.language())
UNKNOWN_CODES = {"CPP_SYNTAX_ERROR", "SYMBOL_UNVERIFIED",
                 "UNSUPPORTED_REFERENCE", "C_PREPROCESSOR_UNVERIFIED"}


class _CppSyntax(Exception):
    pass


def _line_starts(source: bytes) -> List[int]:
    return [0] + [i + 1 for i, b in enumerate(source) if b == 0x0A]


def _line_of(line_starts: List[int], offset: int) -> int:
    return bisect.bisect_right(line_starts, offset)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _parse(source: bytes) -> Node:
    parser = Parser(LANGUAGE)
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise _CppSyntax()
    return tree.root_node


def _node_key(node: Node) -> tuple:
    return (node.start_byte, node.end_byte, node.type)


def _build_parents(tree: Node) -> Dict[tuple, Node]:
    parents: Dict[tuple, Node] = {}
    stack = [(tree, None)]
    while stack:
        node, parent = stack.pop()
        if parent is not None:
            parents[_node_key(node)] = parent
        for child in node.children:
            stack.append((child, node))
    return parents


def _walk(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in node.children:
            stack.append(child)


def _extract_name(node: Node, source: bytes) -> Optional[str]:
    """Recursive identifier scan for the declarator's function name."""
    for piece in node.named_children:
        if piece.type == "identifier":
            return _text(piece, source)
        nested = _extract_name(piece, source)
        if nested:
            return nested
    return None


def _collect_includes(tree: Node, source: bytes) -> Set[str]:
    collected: Set[str] = set()
    for child in tree.named_children:
        if child.type != "preproc_include":
            continue
        for item in child.named_children:
            if item.type in ("string_literal", "system_lib_string", "string"):
                raw = _text(item, source)
                if raw.startswith('"') and raw.endswith('"'):
                    collected.add(raw[1:-1])
                elif raw.startswith("<") and raw.endswith(">"):
                    collected.add(raw)
    return collected


def _top_level_free_function(tree: Node, source: bytes, name: str):
    """One unique free function, including inside a namespace."""
    found = []

    def collect(container: Node) -> None:
        for child in container.named_children:
            if child.type == "function_definition":
                if _extract_name(child, source) == name:
                    found.append(child)
            elif child.type == "namespace_definition":
                body = child.child_by_field_name("body")
                if body is not None:
                    collect(body)

    collect(tree)
    return found


def _namespaces(tree: Node, source: bytes) -> Optional[str]:
    """Outermost namespace name of the protected tree (None if top-level)."""
    for child in tree.named_children:
        if child.type == "namespace_definition":
            for item in child.named_children:
                if item.type == "namespace_identifier":
                    return _text(item, source)
            return ""  # anon namespace
    return None


def check_cpp_window(snapshot: Snapshot, rule: TemporaryRule) -> WindowResult:
    if rule.lifecycle == "RESOLVED":
        return WindowResult(rule.id, "RESOLVED", (), (), True)

    files = {f.path: f for f in snapshot.files}
    trees: Dict[str, Tuple[Node, bytes]] = {}
    syntax_diagnostics: List[Diagnostic] = []
    for path in sorted(files):
        if not path.endswith((".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
                              ".hxx")):
            continue
        source = files[path].content.encode("utf-8")
        try:
            trees[path] = (_parse(source), source)
        except _CppSyntax:
            syntax_diagnostics.append(Diagnostic(
                "CPP_SYNTAX_ERROR", "file fails to parse", path))

    protected_path = rule.protected_symbol.path
    if protected_path not in trees:
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(
            syntax_diagnostics + [Diagnostic(
                "SYMBOL_UNVERIFIED",
                "protected file {} missing or unparsable".format(
                    protected_path), protected_path)]), False)

    protected_tree, protected_source = trees[protected_path]
    definition_nodes = _top_level_free_function(
        protected_tree, protected_source, rule.protected_symbol.symbol)
    diagnostics: List[Diagnostic] = list(syntax_diagnostics)
    if len(definition_nodes) > 1:
        diagnostics.append(Diagnostic("SYMBOL_UNVERIFIED",
                                      "free function defined more than "
                                      "once", protected_path))
    elif not definition_nodes:
        diagnostics.append(Diagnostic("SYMBOL_UNVERIFIED",
                                      "protected function {} not defined as "
                                      "a free function".format(
                                          rule.protected_symbol.symbol),
                                      protected_path))

    if any(d.code in UNKNOWN_CODES for d in diagnostics):
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)

    expected_header = str(PurePosixPath(
        rule.protected_symbol.path).with_suffix(".h"))
    if expected_header in files:
        header_tree, header_source = trees[expected_header]
        prototype_count = sum(
            1 for n in _walk(header_tree)
            if n.type == "function_declarator" and _extract_name(
                n, header_source) == rule.protected_symbol.symbol)
        if prototype_count > 1:
            diagnostics.append(Diagnostic(
                "SYMBOL_UNVERIFIED",
                "header {} declares {} more than once".format(
                    expected_header, rule.protected_symbol.symbol),
                expected_header))
        elif prototype_count == 0:
            diagnostics.append(Diagnostic(
                "SYMBOL_UNVERIFIED",
                "expected header {} exists but lacks a prototype of "
                "{}".format(expected_header,
                            rule.protected_symbol.symbol), expected_header))
    else:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED",
            "matching header {} absent; call identity unexplained".format(
                expected_header), expected_header))

    if any(d.code in UNKNOWN_CODES for d in diagnostics):
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)

    namespace_protected = bool(_namespaces(protected_tree, protected_source))
    keyed: Dict[Tuple[str, str], Set[int]] = {}
    header_base = PurePosixPath(expected_header).name
    for path, (tree, source) in sorted(trees.items()):
        if path == protected_path or path.endswith((".h", ".hh", ".hpp",
                                                    ".hxx")):
            continue
        includes = _collect_includes(tree, source)
        includes_expected_header = any(
            inc == expected_header or inc.endswith(header_base)
            for inc in includes)
        if not includes_expected_header:
            continue
        parents = _build_parents(tree)
        line_starts = _line_starts(source)
        for node in _walk(tree):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            callee_text = _text(callee, source)
            qualifies = (
                callee_text == rule.protected_symbol.symbol
                or callee_text.endswith("::" + rule.protected_symbol.symbol))
            # F19 gate: if a protected function lives inside a namespace,
            # a bare unqualified callee name alone is NOT a definite caller
            # (inner namespace may resolve locally); the caller must use the
            # namespace-qualified form or an explicit `using` declaration.
            if namespace_protected and not (
                    callee_text == "{}::{}".format(_namespaces(
                        protected_tree, protected_source),
                        rule.protected_symbol.symbol)
                    or callee_text.endswith(
                        "::" + rule.protected_symbol.symbol)):
                diagnostics.append(Diagnostic(
                    "UNSUPPORTED_REFERENCE",
                    "unqualified call to namespace-scoped function",
                    path))
                continue
            if qualifies:
                enclosing = _enclosing_name(node, parents, source)
                keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                    _line_of(line_starts, node.start_byte))

    consumers: List[Consumer] = []
    for (path, symbol), line_set in sorted(keyed.items()):
        consumers.append(Consumer(path, symbol, tuple(sorted(line_set))))
    complete = all(d.code not in UNKNOWN_CODES for d in diagnostics)
    status: str = "VIOLATED" if consumers else ("UNVERIFIED" if diagnostics
                                               else "OPEN")
    return WindowResult(rule.id, status, tuple(consumers),
                        tuple(diagnostics), complete)


def _enclosing_name(node: Node, parents: Dict[tuple, Node], source: bytes):
    """Innermost enclosing function-definition identifier; MODULE_SCOPE at top."""
    current = parents.get(_node_key(node))
    while current is not None:
        if current.type == "function_definition":
            return _extract_name(current, source)
        current = parents.get(_node_key(current))
    return MODULE_SCOPE
