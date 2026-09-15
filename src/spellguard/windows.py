"""Pure-AST `no_external_callers` window check for one protected symbol.

Consumes only in-memory Snapshot; never reads files, never executes imports.
Direct parse
failure is never treated as "no dependency".

Status rules (ARCHITECTURE §5):
- RESOLVED rule -> always RESOLVED, no consumers.
- ACTIVE with a definite external caller -> VIOLATED (unknowns are kept).
- No definite caller and the static scope is incomplete -> UNVERIFIED.
- Otherwise OPEN.
"""

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from .models import Diagnostic, Snapshot, SourceFile
from .registry import TemporaryRule

MODULE_SCOPE = "<module>"


@dataclass(frozen=True)
class Consumer:
    path: str
    symbol: str
    lines: Tuple[int, ...]


@dataclass(frozen=True)
class WindowResult:
    rule_id: str
    status: str
    consumers: Tuple[Consumer, ...]
    diagnostics: Tuple[Diagnostic, ...]
    complete: bool


@dataclass(frozen=True)
class _Binding:
    module: str
    attr: Optional[str]  # None for module binding, str for `from m import attr`
    stmt: ast.stmt


@dataclass(frozen=True)
class _Symbol:
    path: str
    module: str
    name: str
    lineno: int


class _SyntaxFailure(Exception):
    pass


def _module_name(path: str, source_root: str) -> Optional[str]:
    candidate = PurePosixPath(path)
    if candidate.suffix != ".py":
        return None
    parts = candidate.parts
    if source_root == ".":
        if len(parts) != 1:
            return None
        return candidate.stem
    if not parts or parts[0] != source_root or len(parts) < 2:
        return None
    return ".".join(list(parts[1:-1]) + [candidate.stem])


def _package_of(path: str, source_root: str) -> str:
    parts = PurePosixPath(path).parts
    if source_root == ".":
        return ""
    if not parts or parts[0] != source_root:
        return ""
    return ".".join(parts[1:-1])


def _resolve_relative(module: str, level: int, current_package: str) -> Optional[str]:
    if level == 0:
        return module
    rest = module
    base = current_package
    for _ in range(level - 1):
        if not base:
            return None
        base = ".".join(base.split(".")[:-1])
    if not base and level > 1:
        return None
    return "{}.{}".format(base, rest) if rest else base


def _parse(path: str, content: str) -> ast.Module:
    try:
        return ast.parse(content, filename=path)
    except SyntaxError:
        raise _SyntaxFailure()


class _Scopes:
    """Parent map for one parsed module."""

    def __init__(self, tree: ast.Module) -> None:
        self.parents: Dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node


_CONTROL = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With,
             ast.AsyncWith, ast.ExceptHandler)


def _import_context(scopes: _Scopes, stmt: ast.stmt) -> str:
    """'top' | 'function' | 'conditional' for an import statement."""
    node: Optional[ast.AST] = stmt
    while node is not None:
        parent = scopes.parents.get(node)
        if parent is None:
            return "top"
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.Lambda, ast.ClassDef)):
            return "function"
        if isinstance(parent, _CONTROL):
            return "conditional"
        node = parent
    return "top"


def _enclosing_scopes(scopes: _Scopes, node: ast.AST) -> Tuple[ast.AST, ...]:
    enclosing: List[ast.AST] = []
    current: Optional[ast.AST] = node
    while current is not None:
        parent = scopes.parents.get(current)
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.Lambda, ast.ClassDef)):
            enclosing.append(parent)
        current = parent
    return tuple(enclosing)


def _local_def_shadows(scope: ast.AST, name: str) -> bool:
    """Control blocks share bindings; independent bodies have their own scope."""
    if isinstance(scope, ast.Lambda):
        return False

    class Bindings(ast.NodeVisitor):
        bound = False
        outer = False

        def visit_FunctionDef(self, node):
            self.bound |= node.name == name

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Lambda(self, node):
            pass

        visit_ListComp = visit_Lambda
        visit_SetComp = visit_Lambda
        visit_DictComp = visit_Lambda
        visit_GeneratorExp = visit_Lambda

        def visit_Name(self, node):
            self.bound |= isinstance(node.ctx, ast.Store) and node.id == name

        def visit_Import(self, node):
            self.bound |= any((a.asname or a.name.split(".")[0]) == name
                              for a in node.names)

        def visit_ImportFrom(self, node):
            self.bound |= any((a.asname or a.name) == name for a in node.names)

        def visit_Global(self, node):
            self.outer |= name in node.names

        visit_Nonlocal = visit_Global

        def visit_ExceptHandler(self, node):
            self.bound |= node.name == name
            self.generic_visit(node)

    bindings = Bindings()
    for statement in scope.body:
        bindings.visit(statement)
    return bindings.bound and not bindings.outer


def _bound_by_parameter(scope: ast.AST, name: str) -> bool:
    if isinstance(scope, ast.Lambda):
        args = scope.args
    elif isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = scope.args
    else:
        return False
    candidates = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        candidates.append(args.vararg)
    if args.kwarg:
        candidates.append(args.kwarg)
    return any(argument.arg == name for argument in candidates
               if argument is not None)


def _qualname(scopes: _Scopes, node: ast.AST) -> str:
    parts: List[str] = []
    current: Optional[ast.AST] = node
    while current is not None:
        parent = scopes.parents.get(current)
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(parent.name)
        elif isinstance(parent, ast.ClassDef):
            parts.append(parent.name)
        current = parent
    return ".".join(reversed(parts)) or MODULE_SCOPE


def _top_level_functions(tree: ast.Module, name: str) -> List[ast.FunctionDef]:
    found: List[ast.FunctionDef] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and stmt.name == name:
            found.append(stmt)
    return found


def _bound_names_in_statement(stmt: ast.stmt) -> Tuple[str, ...]:
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        names = []
        for alias in stmt.names:
            if alias.name == "*":
                continue
            names.append(alias.asname or alias.name.split(".")[0])
        return tuple(names)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (stmt.name,)
    if isinstance(stmt, ast.Assign):
        names: List[str] = []
        for target in stmt.targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    names.append(node.id)
        return tuple(names)
    if isinstance(stmt, ast.AnnAssign):
        return (stmt.target.id, ) if isinstance(stmt.target, ast.Name) else ()
    if isinstance(stmt, ast.AugAssign):
        return (stmt.target.id, ) if isinstance(stmt.target, ast.Name) else ()
    return ()


def _locate_symbol(snapshot: Snapshot, rule: TemporaryRule
                   ) -> Tuple[Optional[_Symbol], List[Diagnostic]]:
    protected = rule.protected_symbol
    files_by_path = {f.path: f for f in snapshot.files}
    path = protected.path
    if path not in files_by_path:
        return None, [Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected file {} is missing from the snapshot".format(path), path)]
    content = files_by_path[path].content
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return None, [Diagnostic(
            "PYTHON_SYNTAX_ERROR", "protected file fails to parse", path)]
    if protected.source_root != ".":
        parts = PurePosixPath(path).parts
        for depth in range(2, len(parts)):
            package_path = "/".join(list(parts[:depth]) + ["__init__.py"])
            if package_path not in files_by_path:
                return None, [Diagnostic(
                    "SYMBOL_UNVERIFIED",
                    "package {} has no __init__.py".format(
                        ".".join(parts[:depth])), path)]
    found = _top_level_functions(tree, protected.symbol)
    if not found:
        return None, [Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected function {} is not defined at module top level".format(
                protected.symbol), path)]
    if len(found) > 1:
        return None, [Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected function {} is defined more than once".format(
                protected.symbol), path)]
    if found[0].decorator_list:
        return None, [Diagnostic(
            "SYMBOL_UNVERIFIED",
            "protected function {} is wrapped by a decorator".format(
                protected.symbol), path)]
    module = _module_name(path, protected.source_root)
    if module is None:
        return None, [Diagnostic(
            "SYMBOL_UNVERIFIED",
            "cannot derive module path from {}".format(path), path)]
    return (_Symbol(path, module, protected.symbol, found[0].lineno), [])


class _FileAnalysis:
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.tree = tree
        self.scopes = _Scopes(tree)
        self.bindings: Dict[str, _Binding] = {}
        self.unsupported: List[str] = []


def _collect_bindings(analysis: _FileAnalysis, target_module: str,
                      target_symbol: str, source_root: str) -> None:
    """Track only bindings that could resolve to the protected module.

    Unrelated imports (json, os, other modules) are out of scope entirely:
    their value-references, rebinding or getattr usage cannot mask a
    protected call, and reporting them as UNSUPPORTED_REFERENCE would make
    every repository permanently incomplete."""
    scopes = analysis.scopes
    package = _package_of(analysis.path, source_root)

    for stmt in ast.walk(analysis.tree):
        if isinstance(stmt, ast.Import):
            context = _import_context(scopes, stmt)
            for alias in stmt.names:
                binding_name = alias.asname or alias.name.split(".")[0]
                if alias.name == target_module and context != "top":
                    analysis.unsupported.append(
                        "{} import of protected module".format(context))
                    continue
                if alias.name != target_module:
                    continue
                analysis.bindings[binding_name] = _Binding(
                    alias.name, None, stmt)
        elif isinstance(stmt, ast.ImportFrom):
            context = _import_context(scopes, stmt)
            module = _resolve_relative(stmt.module or "", stmt.level, package)
            if module is None:
                analysis.unsupported.append(
                    "relative import cannot be resolved to a package")
                continue
            is_star = any(alias.name == "*" for alias in stmt.names)
            if is_star:
                if module == target_module:
                    analysis.unsupported.append(
                        "star import from protected module")
                continue
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                if (module == target_module and alias.name == target_symbol
                        and context != "top"):
                    analysis.unsupported.append(
                        "{} import of protected module".format(context))
                    continue
                if alias.name == module.split(".")[-1]:
                    # `from demo.workaround import workaround` binds module
                    binding_name = alias.asname or alias.name
                    analysis.bindings[binding_name] = _Binding(
                        module, None, stmt)
                elif "{}.{}".format(module, alias.name) == target_module:
                    # `from demo import workaround` binds the child module
                    binding_name = alias.asname or alias.name
                    analysis.bindings[binding_name] = _Binding(
                        "{}.{}".format(module, alias.name), None, stmt)
                elif module == target_module and alias.name == target_symbol:
                    binding_name = alias.asname or alias.name
                    analysis.bindings[binding_name] = _Binding(
                        module, alias.name, stmt)
                else:
                    # imported attribute from an unrelated module (e.g.
                    # `from json import dumps`): only relevant when the name
                    # is later called AND that call still binds to the
                    # protected module — it never does. Out of scope.
                    continue


def _value_reference_name(analysis: _FileAnalysis, name: str) -> bool:
    """True when the binding is referenced as a value somewhere (not a direct
    call, not an import statement, not part of an attribute-chain call)."""
    for node in ast.walk(analysis.tree):
        if not isinstance(node, ast.Name) or node.id != name:
            continue
        parent = analysis.scopes.parents.get(node)
        if isinstance(parent, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(parent, ast.Attribute):
            continue  # attribute chain: w.fallback() / demo.workaround.fallback()
        if isinstance(parent, ast.Call) and parent.func is node:
            continue  # direct call expression
        return True
    return False


def _match_call(analysis: _FileAnalysis, call: ast.Call, symbol: _Symbol
                ) -> Optional[Tuple[str, str, int]]:
    node = call.func
    if isinstance(node, ast.Name):
        binding = analysis.bindings.get(node.id)
        if binding is None or binding.attr != symbol.name:
            return None
        if binding.module != symbol.module:
            return None
        for scope in _enclosing_scopes(analysis.scopes, call):
            if _bound_by_parameter(scope, node.id):
                return None
            if _local_def_shadows(scope, node.id):
                return None
        return (analysis.path, _qualname(analysis.scopes, call), call.lineno)
    if isinstance(node, ast.Attribute) and node.attr == symbol.name:
        value = node.value
        if isinstance(value, ast.Name):
            binding = analysis.bindings.get(value.id)
            if binding is None or binding.module != symbol.module:
                return None
            if binding.attr is not None:
                return None
            for scope in _enclosing_scopes(analysis.scopes, call):
                if _bound_by_parameter(scope, value.id):
                    return None
                if _local_def_shadows(scope, value.id):
                    return None
            return (analysis.path, _qualname(analysis.scopes, call), call.lineno)
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            binding = analysis.bindings.get(value.value.id)
            if binding is None or binding.module != symbol.module:
                return None
            if binding.attr is not None:
                return None
            if value.attr != symbol.module.rsplit(".", 1)[-1]:
                return None
            for scope in _enclosing_scopes(analysis.scopes, call):
                if _bound_by_parameter(scope, value.value.id):
                    return None
            return (analysis.path, _qualname(analysis.scopes, call), call.lineno)
    return None


def _call_name(call: ast.Call) -> Optional[str]:
    node = call.func
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _getattr_on_module(analysis: _FileAnalysis, module_bindings: Set[str]) -> bool:
    for node in ast.walk(analysis.tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
            continue
        if node.args and isinstance(node.args[0], ast.Name) \
                and node.args[0].id in module_bindings:
            return True
    return False


def _duplicate_qualnames(tree: ast.Module) -> Set[str]:
    counts: Dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                full = "{}.{}".format(prefix, child.name) if prefix else child.name
                counts[full] = counts.get(full, 0) + 1
                visit(child, full)
            else:
                visit(child, prefix)

    visit(tree, "")
    return {name for name, count in counts.items() if count > 1}


def check_window(snapshot: Snapshot, rule: TemporaryRule) -> WindowResult:
    if rule.lifecycle == "RESOLVED":
        return WindowResult(rule.id, "RESOLVED", (), (), True)

    protected_path = rule.protected_symbol.path
    if protected_path.endswith(".go"):
        # G02: Go evaluation lives in go_windows; imported here to avoid a
        # module import cycle at load time.
        from .go_windows import check_go_window
        return check_go_window(snapshot, rule)
    if protected_path.rsplit(".", 1)[-1] in {"js", "jsx", "mjs", "ts",
                                             "tsx", "mts"}:
        # L10: JS/TS evaluation lives in javascript_windows (same reason).
        from .javascript_windows import check_javascript_window
        return check_javascript_window(snapshot, rule)
    if protected_path.endswith(".java"):
        # L20: Java evaluation lives in java_windows; no host branch.
        from .java_windows import check_java_window
        return check_java_window(snapshot, rule)
    if protected_path.endswith(".c"):
        # L30: C evaluation lives in c_windows; no host branch.
        from .c_windows import check_c_window
        return check_c_window(snapshot, rule)
    if protected_path.rsplit(".", 1)[-1] in {"cc", "cpp", "cxx"}:
        # L40: C++ evaluation lives in cpp_windows; no host branch.
        from .cpp_windows import check_cpp_window
        return check_cpp_window(snapshot, rule)

    files_by_path = {f.path: f for f in snapshot.files}
    syntax_diagnostics: List[Diagnostic] = []
    analyses: Dict[str, _FileAnalysis] = {}
    for path in sorted(files_by_path):
        if not path.endswith(".py"):
            continue
        try:
            analyses[path] = _FileAnalysis(
                path, _parse(path, files_by_path[path].content))
        except _SyntaxFailure:
            syntax_diagnostics.append(Diagnostic(
                "PYTHON_SYNTAX_ERROR", "file fails to parse", path))

    symbol, symbol_diagnostics = _locate_symbol(snapshot, rule)
    diagnostics: List[Diagnostic] = list(syntax_diagnostics) + list(symbol_diagnostics)
    if symbol is None:
        return WindowResult(
            rule.id, "UNVERIFIED", (), tuple(diagnostics), False)

    keyed: Dict[Tuple[str, str], List[int]] = {}
    rebound_by_file: Dict[str, Set[str]] = {}
    for path in sorted(analyses):
        if path == rule.protected_symbol.path:
            continue
        analysis = analyses[path]
        _collect_bindings(analysis, symbol.module, symbol.name,
                          rule.protected_symbol.source_root)
        rebound = set()
        for stmt in ast.walk(analysis.tree):
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            if _import_context(analysis.scopes, stmt) != "top":
                continue
            for name in _bound_names_in_statement(stmt):
                if name in analysis.bindings:
                    rebound.add(name)
        rebound_by_file[path] = rebound
        diagnostics.extend(
            Diagnostic("UNSUPPORTED_REFERENCE", message, path)
            for message in analysis.unsupported)
        for stmt in ast.walk(analysis.tree):
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            if _import_context(analysis.scopes, stmt) != "top":
                continue
            for name in _bound_names_in_statement(stmt):
                if name in analysis.bindings:
                    diagnostics.append(Diagnostic(
                        "UNSUPPORTED_REFERENCE",
                        "binding {} is re-assigned; reference uncertain".format(name),
                        path))
        module_bindings = {name for name, binding in analysis.bindings.items()
                           if binding.attr is None}
        if _getattr_on_module(analysis, module_bindings):
            diagnostics.append(Diagnostic(
                "UNSUPPORTED_REFERENCE",
                "getattr on protected module cannot be resolved", path))
        value_refs = [name for name in analysis.bindings
                      if _value_reference_name(analysis, name)]
        for name in value_refs:
            diagnostics.append(Diagnostic(
                "UNSUPPORTED_REFERENCE",
                "binding {} is used as a value, not a direct call".format(name),
                path))
        for call in ast.walk(analysis.tree):
            if not isinstance(call, ast.Call):
                continue
            rebound = rebound_by_file.get(analysis.path, set())
            if _call_name(call) in rebound:
                continue
            match = _match_call(analysis, call, symbol)
            if match is None:
                continue
            match_path, qualname, lineno = match
            keyed.setdefault((match_path, qualname), []).append(lineno)

    duplicate_defs: Dict[str, Set[str]] = {}
    for path, analysis in analyses.items():
        duplicate_defs[path] = _duplicate_qualnames(analysis.tree)

    consumers: List[Consumer] = []
    for (path, qualname), lines in sorted(keyed.items()):
        if qualname != MODULE_SCOPE and qualname in duplicate_defs.get(path, set()):
            diagnostics.append(Diagnostic(
                "SYMBOL_UNVERIFIED",
                "consumer {} is defined more than once in file {}".format(
                    qualname, path), path))
            continue
        consumers.append(Consumer(path, qualname, tuple(sorted(set(lines)))))

    if consumers:
        status = "VIOLATED"
    elif diagnostics:
        status = "UNVERIFIED"
    else:
        status = "OPEN"
    unknown_codes = {
        "PYTHON_SYNTAX_ERROR", "SYMBOL_UNVERIFIED", "UNSUPPORTED_REFERENCE"}
    complete = all(d.code not in unknown_codes for d in diagnostics)
    return WindowResult(
        rule.id, status, tuple(consumers), tuple(diagnostics), complete)
