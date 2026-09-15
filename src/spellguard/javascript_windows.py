"""Pure in-memory JS/TS `no_external_callers` evaluation using Tree-sitter.

Contract source: the L10 multi-language design and shared identity contract.
Scope: relative ESM named/default/namespace imports and unique extensionless
relative imports; direct `name()` or `ns.name()` calls only. CommonJS
`require`, dynamic `import()`, bare package specifiers, tsconfig paths,
re-export chains, decorator flows and function values become unknowns; no
guessing and no empty success. Test files are not auto-excluded.

Consumes only the in-memory Snapshot; no disk, network, subprocess; no
Tree-sitter Point/column access.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript
import tree_sitter_typescript

from .models import Diagnostic, Snapshot
from .registry import TemporaryRule
from .windows import Consumer, MODULE_SCOPE, WindowResult

JS_SUFFIXES = {".js", ".jsx", ".mjs"}
TS_SUFFIXES = {".ts", ".tsx", ".mts"}
ALL_SUFFIXES = sorted(JS_SUFFIXES | TS_SUFFIXES)

LANGUAGE_JS = Language(tree_sitter_javascript.language())
LANGUAGE_TS = Language(tree_sitter_typescript.language_typescript())
LANGUAGE_TSX = Language(tree_sitter_typescript.language_tsx())


class _SyntaxError_(Exception):
    pass


def _line_starts(source: bytes) -> List[int]:
    return [0] + [i + 1 for i, b in enumerate(source) if b == 0x0A]


def _line_of(line_starts: List[int], offset: int) -> int:
    return bisect.bisect_right(line_starts, offset)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _parse(source: bytes, language: Language) -> Node:
    parser = Parser(language)
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise _SyntaxError_()
    return tree.root_node


def _language_for(path: str) -> Language:
    suffix = PurePosixPath(path).suffix
    if suffix in TS_SUFFIXES:
        if suffix == ".tsx":
            return LANGUAGE_TSX
        return LANGUAGE_TS
    return LANGUAGE_JS


def _resolve_relative(import_path: str, importer: str) -> Optional[str]:
    """Only relative imports; a bare specifier is not resolvable."""
    if not import_path.startswith("."):
        return None
    folder = "/".join(PurePosixPath(importer).parts[:-1])
    segments: List[str] = []
    merged = (folder + "/" if folder else "") + import_path
    for part in merged.split("/"):
        if part == ".":
            continue
        if part == "..":
            if segments:
                segments.pop()
            continue
        segments.append(part)
    return "/".join(segments)


def _candidate_files(current_files: Dict[str, object], resolved: str) -> List[str]:
    matches = []
    for suffix in ("", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".mts",
                   "/index.js", "/index.ts", "/index.tsx", "/index.mts"):
        candidate = resolved + suffix
        if candidate in current_files and candidate not in matches:
            matches.append(candidate)
    if len(matches) == 1:
        return matches
    return []  # ambiguous → unknown


@dataclass(frozen=True)
class _ImportBinding:
    path: str
    local: str
    imported: Optional[str]
    namespace: bool = False


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


def _enclosing_function(node: Node, parents, source: bytes) -> Optional[str]:
    current = parents.get(_node_key(node))
    while current is not None:
        if current.type == "function_declaration":
            name = current.child_by_field_name("name")
            if name is not None:
                return _text(name, source)
            return MODULE_SCOPE
        if current.type in ("function_expression", "arrow_function"):
            return MODULE_SCOPE
        current = parents.get(_node_key(current))
    return MODULE_SCOPE



def _parse_imports(tree: Node, source: bytes) -> List[_ImportBinding]:
    """Return source-aware bindings for relative ESM imports."""
    collected: List[_ImportBinding] = []
    for child in tree.named_children:
        if child.type != "import_statement":
            continue
        clause = None
        source_node = None
        for item in child.named_children:
            if item.type == "import_clause":
                clause = item
            elif item.type == "string":
                source_node = item
        import_path = None
        if source_node is not None:
            import_path = _text(source_node, source).strip("\"'")
        if import_path is None:
            for item in child.named_children:
                if item.type == "string":
                    import_path = _text(item, source).strip("'\"")
                    break
        if import_path is None:
            continue

        if clause is None:
            # side-effect only: `import "./mod.js"`
            # No binding introduced; protected-name identity unclear.
            continue
        for item in clause.named_children:
            if item.type == "identifier":
                # default import
                collected.append(_ImportBinding(
                    import_path, _text(item, source), "default"))
            elif item.type == "namespace_import":
                for alias in item.named_children:
                    if alias.type == "identifier":
                        collected.append(_ImportBinding(
                            import_path, _text(alias, source), None, True))
            elif item.type == "named_imports":
                for spec in item.named_children:
                    if spec.type == "import_specifier":
                        imported = spec.child_by_field_name("name")
                        alias = spec.child_by_field_name("alias")
                        if imported is not None:
                            imported_name = _text(imported, source)
                            collected.append(_ImportBinding(
                                import_path,
                                _text(alias, source) if alias is not None
                                else imported_name,
                                imported_name))
    return collected



def _top_level_functions(tree: Node, source: bytes) -> Dict[str, List[Node]]:
    """Top-level named (optionally exported) function declarations. Class
    methods, arrow variables and anonymous default are not protected."""
    found: Dict[str, List[Node]] = {}

    for child in tree.named_children:
        node = child
        if node.type in ("export_statement",):
            inner = None
            for item in node.named_children:
                if item.type in ("function_declaration", "function_signature"):
                    inner = item
                    break
            if inner is None:
                continue
            node = inner
        if node.type not in ("function_declaration", "function_signature"):
            continue
        name = None
        for item in node.named_children:
            if item.type == "identifier":
                name = _text(item, source)
                break
        if name:
            found.setdefault(name, []).append(node)
    return found


def _is_default_export(tree: Node, source: bytes, name: str) -> bool:
    for child in tree.named_children:
        if child.type != "export_statement" or not _text(
                child, source).lstrip().startswith("export default"):
            continue
        for item in child.named_children:
            if item.type == "function_declaration":
                node = item.child_by_field_name("name")
                return node is not None and _text(node, source) == name
    return False


def _local_function_declarations_in_scope(node: Node, name: str,
                                          source: bytes) -> bool:
    """Any nested (non-top-level) declaration of `name` shadows."""
    for statement in node.named_children:
        if statement.type in ("function_declaration", "lexical_declaration",
                              "variable_declaration"):
            for item in statement.named_children:
                if item.type == "identifier" and _text(item, source) == name:
                    return True
                if item.type == "variable_declarator":
                    for piece in item.named_children:
                        if piece.type == "identifier" and _text(
                                piece, source) == name:
                            return True
        if statement.type in ("function_declaration", "function_expression"):
            for piece in node.named_children:
                if piece.type == "identifier" and _text(piece, source) == name:
                    return True
    return False


def _enclosed_shadows(node: Node, call_node: Node, parents, name: str,
                      source: bytes) -> bool:
    """Walk enclosing scopes; any local binding of name shadows the import."""
    current = parents.get(_node_key(call_node))
    while current is not None:
        if current.type in ("function_declaration", "function_expression",
                            "arrow_function"):
            # Peek its enclosing named function; parameters shadow
            for param in current.named_children:
                if param.type == "formal_parameters":
                    for ident in param.named_children:
                        if ident.type == "identifier" and _text(
                                ident, source) == name:
                            return True
            if current.type in ("function_declaration",):
                # only if nested, not the top-level one
                if (parents.get(_node_key(current)) is not tree_root(current)
                        and _function_names_itself(current, name, source)):
                    return True
            block = current.child_by_field_name("body")
            if block is not None and _block_binds(block, name, source):
                return True
        current = parents.get(_node_key(current))
    return False


def tree_root(node: Node) -> Node:
    while node.parent is not None:
        try:
            node = node.parent
        except AttributeError:
            break
    return node


def _function_names_itself(node: Node, name: str, source: bytes) -> bool:
    for item in node.named_children:
        if item.type == "identifier" and _text(item, source) == name:
            return True
    return False


def _block_binds(block: Node, name: str, source: bytes) -> bool:
    for statement in block.named_children:
        if statement.type in ("lexical_declaration",
                              "expression_statement",
                              "variable_declaration"):
            for item in statement.named_children:
                if item.type in ("variable_declarator", "assignment_expression",
                                 "augmented_assignment_expression"):
                    left = (item.child_by_field_name("left")
                            or item.child_by_field_name("name"))
                    if (left is not None and left.type == "identifier"
                            and _text(left, source) == name):
                        return True
                    if left is not None:
                        for sub in left.named_children:
                            if sub.type == "identifier" and _text(
                                    sub, source) == name:
                                return True
                if item.type == "call_expression":
                    continue
                if item.type == "binary_expression":
                    continue
        if statement.type == "function_declaration":
            for item in statement.named_children:
                if item.type == "identifier" and _text(item, source) == name:
                    return True
        if statement.type == "import_statement":
            continue
    return False


def _walk(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in node.children:
            stack.append(child)


def check_javascript_window(snapshot: Snapshot, rule: TemporaryRule) -> (
        WindowResult):
    if rule.lifecycle == "RESOLVED":
        return WindowResult(rule.id, "RESOLVED", (), (), True)

    files = {f.path: f for f in snapshot.files}
    trees: Dict[str, Tuple[Node, bytes]] = {}
    syntax_diagnostics: List[Diagnostic] = []
    for path in sorted(files):
        suffix = PurePosixPath(path).suffix
        if suffix not in ALL_SUFFIXES:
            continue
        source = files[path].content.encode("utf-8")
        language = _language_for(path)
        try:
            trees[path] = (_parse(source, language), source)
        except _SyntaxError_:
            syntax_diagnostics.append(Diagnostic(
                "JS_SYNTAX_ERROR" if suffix in JS_SUFFIXES else
                "TS_SYNTAX_ERROR", "file fails to parse", path))

    protected_path = rule.protected_symbol.path
    if protected_path not in trees:
        diagnostics = list(syntax_diagnostics) + [Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected file {} is missing or unparsable".format(protected_path),
            protected_path)]
        return WindowResult(rule.id, "UNVERIFIED", (), tuple(diagnostics), False)

    protected_tree, protected_source = trees[protected_path]
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

    protected_name = rule.protected_symbol.symbol
    protected_is_default = _is_default_export(
        protected_tree, protected_source, protected_name)
    keyed: Dict[Tuple[str, str], Set[int]] = {}
    for path, (tree, source) in sorted(trees.items()):
        if path == protected_path:
            continue
        parents = _build_parents(tree)
        line_starts = _line_starts(source)
        imports = _parse_imports(tree, source)
        bindings: Dict[str, _ImportBinding] = {}

        for binding in imports:
            if binding.path.startswith("."):
                resolved = _resolve_relative(binding.path, path)
                matches = _candidate_files(files, resolved) if resolved else []
                if not matches:
                    continue  # unresolved relative import: unknown
                unique_target = matches[0]
                if unique_target != protected_path:
                    continue
                bindings[binding.local] = binding

        # A definite call requires the identifier was imported from the
        # protected path via a relative ESM import. Bare/dynamic/shadowed
        # calls are NOT definites (no guessing).
        for node in _walk(tree):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            enclosing = _enclosing_function(node, parents, source)
            if callee.type == "identifier":
                name = _text(callee, source)
                binding = bindings.get(name)
                if binding is None or binding.namespace:
                    continue
                if not (binding.imported == protected_name or
                        (binding.imported == "default" and
                         protected_is_default)):
                    continue
                if _enclosed_shadows(node, node, parents, name, source):
                    continue
                keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                    _line_of(line_starts, node.start_byte))
            elif callee.type == "member_expression":
                owner = callee.child_by_field_name("object")
                field = callee.child_by_field_name("property")
                if owner is None or field is None:
                    continue
                owner_name = _text(owner, source)
                prop_name = _text(field, source)
                if prop_name != protected_name:
                    continue
                binding = bindings.get(owner_name)
                if binding is None or not binding.namespace:
                    continue
                if _enclosed_shadows(node, node, parents, owner_name, source):
                    continue
                keyed.setdefault((path, enclosing or MODULE_SCOPE), set()).add(
                    _line_of(line_starts, node.start_byte))

    consumers: List[Consumer] = []
    for (path, symbol), line_set in sorted(keyed.items()):
        consumers.append(Consumer(path, symbol, tuple(sorted(line_set))))
    complete = not syntax_diagnostics and not [
        d for d in diagnostics if d.code == "SYMBOL_UNVERIFIED"]
    if consumers:
        status = "VIOLATED"
    else:
        status = "UNVERIFIED" if diagnostics else "OPEN"
    return WindowResult(rule.id, status, tuple(consumers),
                        tuple(diagnostics), complete)
