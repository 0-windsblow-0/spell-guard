"""Pure in-memory C `no_external_callers` (L30 Design §7).

Definite caller: a matched .h in the same source root contains only one
prototype of `name`, the caller literal-`#include`s that header (by its
base filename) and directly calls `name()`. No preprocessor/linker/compiler.
Unknown diags gate `complete` and clear definite callers.
"""

from __future__ import annotations

import bisect
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_c

from .models import Diagnostic, Snapshot
from .registry import TemporaryRule
from .windows import Consumer, MODULE_SCOPE, WindowResult

LANGUAGE = Language(tree_sitter_c.language())
UNKNOWN_CODES = {"C_SYNTAX_ERROR", "SYMBOL_UNVERIFIED", "UNSUPPORTED_REFERENCE",
                 "C_PREPROCESSOR_UNVERIFIED"}


class _CSyntax(Exception):
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
        raise _CSyntax()
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


def _top_level_free_function(tree: Node, source: bytes, name: str):
    found = []
    for child in tree.named_children:
        if child.type != "function_definition":
            continue
        is_static = any(
            piece.type == "storage_class_specifier" and _text(piece, source) == "static"
            for piece in child.named_children)
        if is_static:
            continue
        declarator = child.child_by_field_name("declarator")
        while declarator is not None and declarator.type != "identifier":
            declarator = declarator.child_by_field_name("declarator")
        if declarator is not None and _text(declarator, source) == name:
            found.append(child)
    return found


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


def _has_prototype(tree: Node, source: bytes, name: str) -> bool:
    for child in tree.named_children:
        if child.type != "declaration":
            continue
        for sub in _walk(child):
            if sub.type == "identifier" and _text(sub, source) == name:
                return True
    return False


def _enclosing_name(node: Node, parents: Dict[tuple, Node], source: bytes):
    current = parents.get(_node_key(node))
    while current is not None:
        if current.type == "function_definition":
            for item in current.named_children:
                for piece in item.named_children:
                    if piece.type == "identifier":
                        return _text(piece, source)
            return MODULE_SCOPE
        current = parents.get(_node_key(current))
    return MODULE_SCOPE


def _shadows(node: Node, parents: Dict[tuple, Node], name: str,
             source: bytes) -> bool:
    current = parents.get(_node_key(node))
    while current is not None:
        if current.type == "function_definition":
            for item in current.named_children:
                if item.type == "block":
                    for statement in item.named_children:
                        if statement.type == "declaration":
                            for spec in statement.named_children:
                                if spec.type == "init_declarator":
                                    for sub in spec.named_children:
                                        if sub.type == "identifier" and _text(
                                                sub, source) == name:
                                            return True
        current = parents.get(_node_key(current))
    return False


def check_c_window(snapshot: Snapshot, rule: TemporaryRule) -> WindowResult:
    if rule.lifecycle == "RESOLVED":
        return WindowResult(rule.id, "RESOLVED", (), (), True)

    files = {f.path: f for f in snapshot.files}
    trees: Dict[str, Tuple[Node, bytes]] = {}
    syntax_diagnostics: List[Diagnostic] = []
    for path in sorted(files):
        if not path.endswith((".c", ".h")):
            continue
        source = files[path].content.encode("utf-8")
        try:
            trees[path] = (_parse(source), source)
        except _CSyntax:
            syntax_diagnostics.append(Diagnostic(
                "C_SYNTAX_ERROR", "file fails to parse", path))

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
    keyed: Dict[Tuple[str, str], Set[int]] = {}

    # unknown anytime name can't be uniquely defined locally: no definite
    # caller is trusted until definition identity is confirmed.
    if len(definition_nodes) != 1:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected function {} must have one non-static free-function "
            "definition in {}".format(
                rule.protected_symbol.symbol, protected_path),
            protected_path))
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics),
                            False)
    header_base = PurePosixPath(
        rule.protected_symbol.path.rsplit(".", 1)[0] + ".h").name
    expected_header = rule.protected_symbol.path.rsplit(".", 1)[0] + ".h"
    if expected_header in files:
        header_tree, header_source = trees[expected_header]
        prototype_count = sum(
            1 for n in _walk(header_tree)
            if n.type == "identifier" and _text(
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
        # header identity unclear: no definite caller detection.
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics),
                            False)

    for path, (tree, source) in sorted(trees.items()):
        if path == protected_path or path.endswith(".h"):
            continue
        includes = _collect_includes(tree, source)
        includes_expected_header = any(
            inc == expected_header or inc.endswith(header_base)
            for inc in includes)
        if not includes_expected_header:
            continue  # no literal include of the expected header: unknown
        parents = _build_parents(tree)
        line_starts = _line_starts(source)
        for node in _walk(tree):
            if node.type != "call_expression":
                continue
            callee = None
            for item in node.named_children:
                if item.type == "identifier":
                    callee = item
                    break
            if callee is None:
                continue
            if _text(callee, source) != rule.protected_symbol.symbol:
                continue
            if _shadows(node, parents, _text(callee, source), source):
                continue
            enclosing = _enclosing_name(node, parents, source)
            keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                _line_of(line_starts, node.start_byte))

    consumers: List[Consumer] = []
    for (path, symbol), line_set in sorted(keyed.items()):
        consumers.append(Consumer(path, symbol, tuple(sorted(line_set))))
    complete = all(d.code not in UNKNOWN_CODES for d in diagnostics)
    status: str
    if consumers:
        status = "VIOLATED"
    elif diagnostics:
        status = "UNVERIFIED"
    else:
        status = "OPEN"
    return WindowResult(rule.id, status, tuple(consumers),
                        tuple(diagnostics), complete)
