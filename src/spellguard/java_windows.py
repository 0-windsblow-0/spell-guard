"""Pure in-memory Java `no_external_callers` evaluation using Tree-sitter.

Scope (design §6, L20): the protected symbol is the unique, named, non-
overloaded `static` method inside one top-level type of the definition file.
Definite calls require an explicit import (`Type.method()`), a fully
qualified call (`package.Type.method()`), or an unambiguous static import
followed by a direct `method()`. Same-definition-file calls are not external.
Instance dispatch, overloads, inheritance, wildcard static imports,
reflection, method references and instance-method conflicts stay unknown.
Nothing here invokes javac, Maven, Gradle or a classpath.

Parent map keys on (start_byte, end_byte, type): tree-sitter returns fresh
Node objects per traversal, so raw id() is not stable. No disk, network,
subprocess, and no Point/column access.
"""

from __future__ import annotations

import bisect
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from .models import Diagnostic, Snapshot
from .registry import TemporaryRule
from .windows import Consumer, MODULE_SCOPE, WindowResult

LANGUAGE = Language(tree_sitter_java.language())


class _JavaSyntax(Exception):
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
        raise _JavaSyntax()
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


def _top_type_name(tree: Node, source: bytes) -> Optional[str]:
    for child in tree.named_children:
        if child.type in ("class_declaration", "interface_declaration"):
            for item in child.named_children:
                if item.type == "identifier":
                    return _text(item, source)
    return None


def _package_name(tree: Node, source: bytes) -> Optional[str]:
    for child in tree.named_children:
        if child.type == "package_declaration":
            cleaned = _text(child, source).replace("package", "")
            return cleaned.strip().rstrip(";").strip()
    return None


def _static_methods(tree: Node, source: bytes) -> Dict[str, List[Node]]:
    found: Dict[str, List[Node]] = {}
    for child in tree.named_children:
        if child.type not in ("class_declaration",):
            continue
        body = child.child_by_field_name("body")
        if body is None:
            continue
        for item in body.named_children:
            if item.type != "method_declaration":
                continue
            modifiers_list = [c for c in item.named_children
                              if c.type == "modifiers"]
            modifiers = modifiers_list[0] if modifiers_list else None
            if modifiers is None or "static" not in _text(
                    modifiers, source).split():
                continue
            name = item.child_by_field_name("name")
            if name is None:
                continue
            found.setdefault(_text(name, source), []).append(item)
    return found


def _collect_imports(tree: Node, source: bytes) -> List[Tuple[str, bool]]:
    collected: List[Tuple[str, bool]] = []
    for child in tree.named_children:
        if child.type != "import_declaration":
            continue
        text = _text(child, source)
        cleaned = text.replace("import ", "").replace(";", "").strip()
        is_static = cleaned.startswith("static ")
        if is_static:
            cleaned = cleaned[len("static "):].strip()
        collected.append((cleaned, is_static))
    return collected


def _enclosing_function(node: Node, parents: Dict[tuple, Node],
                        source: bytes) -> Optional[str]:
    current = parents.get(_node_key(node))
    while current is not None:
        if current.type == "method_declaration":
            name = current.child_by_field_name("name")
            if name is not None:
                return _text(name, source)
            return MODULE_SCOPE
        current = parents.get(_node_key(current))
    return MODULE_SCOPE


def _shadows_name(call_node: Node, parents: Dict[tuple, Node], name: str,
                  source: bytes) -> bool:
    """Parameters and local declarations shadow an imported binding."""
    current = parents.get(_node_key(call_node))
    while current is not None:
        if current.type == "formal_parameter":
            for piece in current.named_children:
                if piece.type == "identifier" and _text(piece, source) == name:
                    return True
        if current.type in ("block", "constructor_body"):
            for statement in current.named_children:
                if statement.type in ("local_variable_declaration",):
                    for declarator in statement.named_children:
                        if declarator.type == "variable_declarator":
                            for piece in declarator.named_children:
                                if piece.type == "identifier" and _text(
                                        piece, source) == name:
                                    return True
        current = parents.get(_node_key(current))
    return False


def check_java_window(snapshot: Snapshot, rule: TemporaryRule) -> WindowResult:
    if rule.lifecycle == "RESOLVED":
        return WindowResult(rule.id, "RESOLVED", (), (), True)

    files = {f.path: f for f in snapshot.files}
    trees: Dict[str, Tuple[Node, bytes]] = {}
    syntax_diagnostics: List[Diagnostic] = []
    for path in sorted(files):
        if not path.endswith(".java"):
            continue
        source = files[path].content.encode("utf-8")
        try:
            trees[path] = (_parse(source), source)
        except _JavaSyntax:
            syntax_diagnostics.append(Diagnostic(
                "JAVA_SYNTAX_ERROR", "file fails to parse", path))

    protected_path = rule.protected_symbol.path
    if protected_path not in trees:
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(
            syntax_diagnostics + [Diagnostic(
                "SYMBOL_UNVERIFIED",
                "protected file {} is missing or unparsable".format(
                    protected_path), protected_path)]), False)

    protected_tree, protected_source = trees[protected_path]
    type_name = _top_type_name(protected_tree, protected_source)
    diagnostics: List[Diagnostic] = list(syntax_diagnostics)
    if not type_name:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED", "no top-level type in protected file",
            protected_path))
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)
    package = _package_name(protected_tree, protected_source)
    if not package:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED", "missing package declaration", protected_path))
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)

    statics = _static_methods(protected_tree, protected_source)
    definition_nodes = statics.get(rule.protected_symbol.symbol, [])
    if len(definition_nodes) > 1:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED", "static method defined more than once",
            protected_path))
    elif not definition_nodes:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected static method {} not defined at top-level "
            "type".format(rule.protected_symbol.symbol), protected_path))

    fully_qualified = "{}.{}.{}".format(package, type_name,
                                        rule.protected_symbol.symbol)
    pair_path = "{}.{}".format(type_name, rule.protected_symbol.symbol)
    keyed: Dict[Tuple[str, str], Set[int]] = {}

    for path, (tree, source) in sorted(trees.items()):
        if path == protected_path:
            continue
        parents = _build_parents(tree)
        line_starts = _line_starts(source)
        imports = _collect_imports(tree, source)
        explicit_type_import = any(
            imported == "{}.{}".format(package, type_name) and not is_static
            for imported, is_static in imports)
        wildcard_static_import = any(
            is_static and imported == "{}.{}.*".format(package, type_name)
            for imported, is_static in imports)
        explicit_static_import = any(
            is_static and imported.endswith(
                ".{}".format(rule.protected_symbol.symbol))
            and imported.startswith("{}.".format(package, type_name))
            and not imported.endswith(".*")
            for imported, is_static in imports)

        for node in _walk(tree):
            if node.type != "method_invocation":
                continue
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            callee_name = _text(name_node, source)
            if callee_name != rule.protected_symbol.symbol:
                continue
            enclosing = _enclosing_function(node, parents, source)
            line = _line_of(line_starts, node.start_byte)
            identifiers = [c for c in node.named_children
                           if c.type == "identifier"]
            field_access = [c for c in node.named_children
                            if c.type == "field_access"]
            # bare invocation: exactly one identifier == method name.
            # field_access selector: nested selector expression.
            object_parts = field_access if field_access else \
                (identifiers[:-1] if len(identifiers) >= 2 else [])
            object_node = object_parts[0] if object_parts else None
            if object_node is None:
                # bare method() — only unambiguous with an exact static import
                if wildcard_static_import:
                    diagnostics.append(Diagnostic(
                        "UNSUPPORTED_REFERENCE",
                        "wildcard static import cannot identify {}"
                        .format(callee_name), path))
                    continue
                if explicit_static_import and not _shadows_name(
                        node, parents, callee_name, source):
                    keyed.setdefault(
                        (path, enclosing or MODULE_SCOPE), set()).add(line)
                continue
            owner_text = _text(object_node, source)
            if _shadows_name(object_node, parents, owner_text, source):
                continue  # parameter or local variable intercepts
            if owner_text == type_name and explicit_type_import:
                keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                    line)
                continue
            if owner_text == fully_qualified or owner_text == pair_path:
                keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                    line)
                continue
            if owner_text == "{}.{}".format(package, type_name):
                keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                    line)
                continue

    consumers: List[Consumer] = []
    for (path, symbol), line_set in sorted(keyed.items()):
        consumers.append(Consumer(path, symbol, tuple(sorted(line_set))))
    unknown_codes = {"JAVA_SYNTAX_ERROR", "SYMBOL_UNVERIFIED",
                     "UNSUPPORTED_REFERENCE"}
    complete = all(d.code not in unknown_codes for d in diagnostics)
    if consumers:
        status = "VIOLATED"
    elif diagnostics:
        status = "UNVERIFIED"
    else:
        status = "OPEN"
    return WindowResult(rule.id, status, tuple(consumers),
                        tuple(diagnostics), complete)
