"""Pure in-memory Go `no_external_callers` evaluation using Tree-sitter.

Consumes only the in-memory Snapshot (its SourceFile contents plus go.mod
metadata collected by the repository layer) and one rule; no disk, network,
subprocess, and no Tree-sitter Point/column access. Status, diagnostic and
consumer ordering follow the shared window contract (ARCHITECTURE §7, G02).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_go

from .models import Diagnostic, Snapshot
from .registry import TemporaryRule
from .windows import Consumer, MODULE_SCOPE, WindowResult

LANGUAGE = Language(tree_sitter_go.language())


class _GoSyntax(Exception):
    pass


def _line_starts(source: bytes) -> List[int]:
    return [0] + [index + 1 for index, byte in enumerate(source) if byte == 0x0A]


def _line_of(line_starts: List[int], byte_offset: int) -> int:
    return bisect.bisect_right(line_starts, byte_offset)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _parse(source: bytes) -> Optional[Node]:
    parser = Parser(LANGUAGE)
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise _GoSyntax()
    return tree.root_node


def _package_name(tree: Node, source: bytes) -> str:
    for child in tree.named_children:
        if child.type == "package_clause":
            for item in child.named_children:
                if item.type == "package_identifier":
                    return _text(item, source)
    return ""


def _go_mod_path(snapshot: Snapshot, source_root: str) -> Optional[str]:
    """Declared Go module metadata file near the protected source root."""
    root_module = str(PurePosixPath(source_root) / "go.mod")
    for path in (root_module, "go.mod", "go.work"):
        if any(f.path == path for f in snapshot.files):
            return path
    return None


def _module_name(go_mod_path: Optional[str],
                 files: Dict[str, bytes]) -> Optional[str]:
    if not go_mod_path or go_mod_path not in files:
        return None
    text = files[go_mod_path].decode("utf-8", "replace")
    for line in text.splitlines():
        cleaned = line.split("//")[0].strip()
        if cleaned.startswith("module "):
            module = cleaned[len("module "):].strip().strip('"').strip("`")
            if module:
                return module
    return None


def _top_level_functions(tree: Node, source: bytes) -> Dict[str, List[Node]]:
    found: Dict[str, List[Node]] = {}
    for child in tree.named_children:
        if child.type != "function_declaration":
            continue
        name = None
        for item in child.named_children:
            if item.type == "identifier":
                name = _text(item, source)
                break
        if name:
            found.setdefault(name, []).append(child)
    return found


def _top_level_methods(tree: Node) -> List[Node]:
    return [child for child in tree.named_children
            if child.type == "method_declaration"]


def _has_type_params(node: Node) -> bool:
    for item in node.named_children:
        if item.type == "type_parameter_list":
            return True
    return False


def _parse_imports(tree: Node, source: bytes) -> List[Tuple[str, Optional[str]]]:
    collected: List[Tuple[str, Optional[str]]] = []
    for child in tree.named_children:
        if child.type != "import_declaration":
            continue
        for item in child.named_children:
            if item.type == "import_spec":
                collected.append(_import_spec(item, source))
            elif item.type == "import_spec_list":
                for spec in item.named_children:
                    if spec.type == "import_spec":
                        collected.append(_import_spec(spec, source))
    return collected


def _import_spec(spec: Node, source: bytes) -> Tuple[str, Optional[str]]:
    path: Optional[str] = None
    alias: Optional[str] = None
    for item in spec.named_children:
        if item.type == "interpreted_string_literal":
            path = _text(item, source).strip('"').strip("`")
        elif item.type == "package_identifier":
            alias = _text(item, source)
        elif item.type == "dot":
            alias = "."
    if path is None:
        return ("", None)
    return (path, alias)


@dataclass(frozen=True)
class _Call:
    kind: str          # "plain" | "selector" | "unsupported"
    callee: Tuple[Optional[str], ...]   # (binding, attr) or (plain_name,)
    line: int
    enclosing: str


def _enclosing_function(node: Node, parents: Dict[Node, Node],
                        source: bytes) -> Optional[str]:
    current = parents.get(node)
    while current is not None:
        if current.type == "function_declaration":
            for item in current.named_children:
                if item.type == "identifier":
                    return _text(item, source)
            return None
        current = parents.get(current)
    return None  # module scope


def _build_parents(tree: Node) -> Dict[Node, Node]:
    parents: Dict[Node, Node] = {}
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in node.children:
            parents[child] = node
            stack.append(child)
    return parents


def _function_binds_name(node: Node, name: str, source: bytes) -> bool:
    """A local binding of `name` within the enclosing function, without
    crossing into nested function declarations."""
    for item in node.named_children:
        if item.type == "parameters":
            for param in item.named_children:
                if _binds_identifier(param, name, source):
                    return True
        if item.type == "block":
            if _block_binds(item, name, source):
                return True
    return False


def _binds_identifier(node: Node, name: str, source: bytes) -> bool:
    if node.type == "identifier" and _text(node, source) == name:
        return True
    if node.type == "expression_list":
        for sub in node.named_children:
            if sub.type == "identifier" and _text(sub, source) == name:
                return True
    return False


def _block_binds(block: Node, name: str, source: bytes) -> bool:
    for statement in block.named_children:
        if statement.type in ("function_declaration", "method_declaration"):
            continue  # nested scope
        for item in statement.named_children:
            if item.type in ("var_declaration", "type_declaration",
                             "const_declaration"):
                for spec in item.named_children:
                    if spec.type in ("var_spec", "const_spec", "type_spec"):
                        for child in spec.named_children:
                            if child.type == "identifier" and _text(
                                    child, source) == name:
                                return True
            if item.type == "expression_list":
                for sub in item.named_children:
                    if sub.type == "identifier" and _text(sub, source) == name:
                        return True
            if item.type == "assignment_statement":
                # left side expression_list comes first in the statement
                left = item.named_children[0] if item.named_children else None
                if left is not None and _binds_identifier(left, name, source):
                    return True
            if item.type == "short_var_declaration":
                left = item.named_children[0] if item.named_children else None
                if left is not None and _binds_identifier(left, name, source):
                    return True
            if item.type in ("for_statement", "for_clause"):
                # range and for clauses bind identifiers too
                for sub in item.named_children:
                    if _binds_identifier(sub, name, source):
                        return True
                    if sub.type in ("range_clause", "for_clause"):
                        if _binds_identifier(sub, name, source):
                            return True
                        for spec in sub.named_children:
                            if _binds_identifier(spec, name, source):
                                return True
            if item.type == "if_statement" and name in _text(statement,
                                                            source).split(":=")[0]:
                for spec in item.named_children:
                    if spec.type == "init":
                        for sub in spec.named_children:
                            if _binds_identifier(sub, name, source):
                                return True
            if item.type in ("import_declaration",):
                specs = _parse_imports(item, source)
                for import_path, alias in specs:
                    if (alias or import_path.rsplit("/", 1)[-1]) == name:
                        return True
    return False


def _calls(tree: Node, parents: Dict[Node, Node], source: bytes
           ) -> List[Tuple[Node, str, int, Optional[str]]]:
    out: List[Tuple[Node, str, int, Optional[str]]] = []
    line_starts = _line_starts(source)
    for node in parents:
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None:
            continue
        kind: str
        parts: Tuple[Optional[str], ...]
        if callee.type == "identifier":
            kind = "plain"
            parts = (_text(callee, source),)
        elif callee.type == "selector_expression":
            value = callee.child_by_field_name("field")
            owner = callee.child_by_field_name("operand")
            if value is None or owner is None:
                continue
            if owner.type == "identifier":
                kind = "selector"
                parts = (_text(owner, source), _text(value, source))
            else:
                kind = "unsupported"
                parts = (None, None)
        else:
            # composite literal call, method value, parenthesized etc.
            kind = "unsupported"
            parts = (None, None)
        line = _line_of(line_starts, node.start_byte)
        enclosing = _enclosing_function(node, parents, source)
        out.append((node, kind, parts, line, enclosing))
    return [(node, kind, parts, line, enclosing)
            for node, kind, parts, line, enclosing in
            [(n, "plain" if False else k, p, l, e)
             for n, k, p, l, e in out]]


def check_go_window(snapshot: Snapshot, rule: TemporaryRule) -> WindowResult:
    if rule.lifecycle == "RESOLVED":
        return WindowResult(rule.id, "RESOLVED", (), (), True)

    files = {f.path: f for f in snapshot.files}
    raw_bytes = {path: f.content.encode("utf-8") for path, f in files.items()
                 if path.endswith(".go")
                 or PurePosixPath(path).name in ("go.mod", "go.work")}
    go_mod_path = _go_mod_path(snapshot, rule.protected_symbol.source_root)
    module = _module_name(go_mod_path, raw_bytes) if go_mod_path else None

    trees: Dict[str, Tuple[Node, bytes]] = {}
    syntax_diagnostics: List[Diagnostic] = []
    for path in sorted(files):
        if not path.endswith(".go"):
            continue
        source = files[path].content.encode("utf-8")
        try:
            trees[path] = (_parse(source), source)
        except _GoSyntax:
            syntax_diagnostics.append(Diagnostic(
                "GO_SYNTAX_ERROR", "file fails to parse", path))

    protected_path = rule.protected_symbol.path
    if protected_path not in trees:
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(
            syntax_diagnostics + [Diagnostic(
                "SYMBOL_UNVERIFIED",
                "protected file {} is missing or unparsable".format(
                    protected_path), protected_path)]), False)

    protected_tree, protected_source = trees[protected_path]
    package = _package_name(protected_tree, protected_source)
    if not package:
        diagnostics = list(syntax_diagnostics) + [Diagnostic(
            "GO_SYNTAX_ERROR", "missing package clause", protected_path)]
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)
    if module is None:
        diagnostics = list(syntax_diagnostics) + [Diagnostic(
            "GO_MODULE_UNVERIFIED",
            "Go module metadata (go.mod) is absent or unreadable",
            protected_path)]
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)
    if package == "main":
        diagnostics = list(syntax_diagnostics) + [Diagnostic(
            "GO_MODULE_UNVERIFIED",
            "package main import targets are not addressable", protected_path)]
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)

    # duplicate protected definition?
    definitions = _top_level_functions(protected_tree, protected_source)
    definition_nodes = definitions.get(rule.protected_symbol.symbol, [])
    diagnostics: List[Diagnostic] = list(syntax_diagnostics)
    if len(definition_nodes) > 1:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected function is defined more than once", protected_path))
    elif not definition_nodes:
        diagnostics.append(Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected function {} is not defined at module top "
            "level".format(rule.protected_symbol.symbol), protected_path))
    keyed: Dict[Tuple[str, str], List[int]] = {}
    unsupported_marks = set()

    target_path = _protected_module_import_path(module, package)

    for path, (tree, source) in sorted(trees.items()):
        if path == protected_path:
            continue  # same-file calls are not external
        parents = _build_parents(tree)
        imports = _parse_imports(tree, source)
        bindings: Dict[str, str] = {}
        alias_dot = False
        for import_path, alias in imports:
            default_binding = import_path.rsplit("/", 1)[-1]
            binding = alias or default_binding
            if import_path == module:
                bindings[binding] = module
            elif import_path == "{}/{}".format(module, package):
                bindings[binding] = module
            elif alias == "." and import_path.rsplit("/", 1)[-1] == package:
                alias_dot = True
                diagnostics.append(Diagnostic(
                    "UNSUPPORTED_REFERENCE",
                    "dot import of the protected package".format(), path))
            else:
                if alias is not None and alias != ".":
                    bindings[alias] = import_path
                elif alias is None:
                    bindings[default_binding] = import_path
            if alias is None and import_path == module:
                bindings[package] = module
        per_file_starts = _line_starts(source)
        for call, kind, parts, line, enclosing in _call_nodes(tree, parents,
                                                              source):
            if kind == "unsupported":
                continue
            shadowed = _call_is_shadowed(call, parents, parts[0],
                                         per_file_starts, source)
            if kind == "selector":
                if bindings.get(parts[0]) != module:
                    continue
                if parts[1] != rule.protected_symbol.symbol:
                    continue
                if shadowed:
                    continue
                keyed.setdefault((path, enclosing or MODULE_SCOPE), []).append(
                    line)
            else:  # plain
                if parts[0] != rule.protected_symbol.symbol:
                    continue
                if alias_dot and package == parts[0] and False:
                    pass
                if bindings.get(parts[0]):
                    continue  # plain name is an unrelated import
                if shadowed:
                    continue
                if package_by_file(trees, path) != package and \
                        package_by_file(trees, path) != package:
                    continue
                keyed.setdefault((path, enclosing or MODULE_SCOPE), []).append(
                    line)

    for path, (tree, source) in sorted(trees.items()):
        if path == protected_path:
            continue
        if package_by_file(trees, path) == package and not _imports(
                tree, source, module):
            # same package clause without import: the name resolves here,
            # so a non-callback value reference is a real risk.
            pass
        elif not _imports(tree, source, module):
            continue  # unrelated package, no import: symbol invisible here
        if not _has_protected_value_reference(tree, source,
                                              rule.protected_symbol.symbol):
            continue
        diagnostics.append(Diagnostic(
            "UNSUPPORTED_REFERENCE",
            "reference to {} as a value".format(
                rule.protected_symbol.symbol), path))

    consumers: List[Consumer] = []
    for (path, symbol), lines in sorted(keyed.items()):
        consumers.append(Consumer(path, symbol, tuple(sorted(set(lines)))))
    unknown_codes = {"GO_SYNTAX_ERROR", "SYMBOL_UNVERIFIED",
                     "UNSUPPORTED_REFERENCE", "GO_MODULE_UNVERIFIED",
                     "GO_BUILD_UNVERIFIED"}
    complete = all(d.code not in unknown_codes for d in diagnostics)
    status = None
    if definition_nodes and consumers and complete is False:
        status = "VIOLATED"
    elif not definition_nodes and consumers:
        status = "VIOLATED"
    elif consumers:
        status = "VIOLATED"
    elif diagnostics:
        status = "UNVERIFIED"
    else:
        status = "OPEN"
    return WindowResult(rule.id, status, tuple(consumers),
                        tuple(diagnostics), complete)


def _value_nodes(tree: Node) -> List[Node]:
    del tree
    return []


def _has_protected_value_reference(tree: Node, source: bytes,
                                   name: str) -> bool:
    """True when the protected symbol's name is referenced outside the
    callee position of a call (function value, assignment RHS, argument...),
    except when that reference belongs to a shadowing declaration or is
    under a shadowed scope."""
    parents = _build_parents(tree)
    hits = 0
    stack = [tree]
    while stack:
        current = stack.pop()
        for child in current.named_children:
            stack.append(child)
        if current.type == "identifier" and _text(current, source) == name:
            parent = parents.get(current)
            if parent is None:
                continue
            if parent.type == "call_expression":
                callee = parent.child_by_field_name("function")
                if callee is not None and (
                        callee.start_byte == current.start_byte
                        and callee.end_byte == current.end_byte):
                    continue  # callee position = a call
            # the identifier sits in a binding position of the same name
            if current.parent is not None and current.parent.type in (
                    "parameter_declaration", "var_spec", "const_spec"):
                # a binding declaration, not a value reference
                continue
            if _line_starts and _call_is_shadowed(
                    parent, parents, name, _line_starts(source), source):
                # shadowed: not a reference to the protected symbol
                continue
            hits += 1
    return hits > 0


def _imports(tree: Node, source: bytes, module: Optional[str]) -> bool:
    if not module:
        return False
    for test_path, _alias in _parse_imports(tree, source):
        if test_path == module or test_path.startswith(module + "/"):
            return True
    return False


def package_by_file(trees, path):
    tree, source = trees[path]
    return _package_name(tree, source)


def _protected_module_import_path(module: Optional[str],
                                  package: str) -> Tuple[str, ...]:
    return (module or "", package)


def _call_nodes(tree: Node, parents: Dict[Node, Node], source: bytes):
    out = []
    line_starts = _line_starts(source)
    for node in parents:
        if node.type != "call_expression":
            continue
        callee = node.child_by_field_name("function")
        if callee is None:
            continue
        line = _line_of(line_starts, callee.start_byte)
        kwargs = _enclosing_function(node, parents, source)
        if callee.type == "identifier":
            out.append((node, "plain", (_text(callee, source),), line, kwargs))
        elif callee.type == "selector_expression":
            owner = callee.child_by_field_name("operand")
            field = callee.child_by_field_name("field")
            if owner is None or field is None:
                continue
            if owner.type == "identifier":
                out.append((node, "selector",
                            (_text(owner, source), _text(field, source)),
                            line, kwargs))
            else:
                out.append((node, "unsupported", (None, None), line, kwargs))
        else:
            out.append((node, "unsupported", (None, None), line, kwargs))
    return out


def _call_is_shadowed(call: Node, parents: Dict[Node, Node], name,
                      line_starts, source: bytes) -> bool:
    """Walk enclosing scopes of the call; any parameter or in-function
    binding of the identifier shadows the package/import."""
    if not name or name == ".":
        return False
    current = parents.get(call)
    while current is not None:
        if current.type in ("function_declaration", "method_declaration",
                            "func_literal"):
            if _function_scope_binds(current, name, source):
                return True
        current = parents.get(current)
    return False


def _node_chain_binds(node: Node, name: str, source: bytes) -> bool:
    """Recursive binding check for parameter lists: parameter identifiers."""
    if node is None:
        return False
    if node.type == "identifier" and _text(node, source) == name:
        return True
    for child in node.named_children:
        if _node_chain_binds(child, name, source):
            return True
    return False


def _function_scope_binds(node: Node, name: str, source: bytes) -> bool:
    for item in node.named_children:
        if item.type in ("parameter_list", "parameters"):
            for param in item.named_children:
                if _node_chain_binds(param, name, source):
                    return True
        if item.type == "block":
            if _block_scope_binds(item, name, source):
                return True
    return False


def _block_scope_binds(block: Node, name: str, source: bytes) -> bool:
    """A local binding of name in this block (not crossing into nested
    function bodies)."""
    for statement in block.named_children:
        if statement.type in ("function_declaration", "method_declaration",
                              "func_literal"):
            continue  # nested scope
        if _binds_identifier(statement, name, source):
            return True
        if statement.type not in ("assignment_statement",
                                  "short_var_declaration"):
            parts = []
            for child in statement.named_children:
                parts.append(child)
            if statement.type in ("assignment_statement",
                                  "short_var_declaration"):
                pass  # re-binding handled directly below
        parts = list(statement.named_children)
        if statement.type in ("assignment_statement",
                              "short_var_declaration") and parts and parts[0].type in ("expression_list", "identifier"):
            if _binds_identifier(parts[0], name, source):
                return True
        if statement.type in ("var_declaration", "const_declaration",
                              "type_declaration"):
            for spec in statement.named_children:
                if spec.type in ("var_spec", "const_spec", "type_spec"):
                    for child in spec.named_children:
                        if child.type == "identifier" and _text(
                                child, source) == name:
                            return True
            continue
        if statement.type in ("if_statement", "for_statement",
                              "range_clause"):
            for sub in statement.named_children:
                if sub.type in ("init", "range_clause", "for_clause",
                                "expression_list", "range_expression"):
                    for child in sub.named_children:
                        if _binds_identifier(child, name, source):
                            return True
            continue
        if statement.type == "statement_list":
            if _block_scope_binds(statement, name, source):
                return True
        if statement.type == "block":
            if _block_scope_binds(statement, name, source):
                return True
        if statement.type == "for_statement":  # repeats handled above
            pass
        del parts
    return False


def _enclosing_function(node: Node, parents: Dict[Node, Node],
                        source: bytes) -> Optional[str]:
    current = parents.get(node)
    while current is not None:
        if current.type == "function_declaration":
            for item in current.named_children:
                if item.type == "identifier":
                    return _text(item, source)
            return MODULE_SCOPE
        if current.type == "func_literal":
            return MODULE_SCOPE  # module-executed literal
        current = parents.get(current)
    return MODULE_SCOPE
