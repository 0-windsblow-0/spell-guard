"""Go syntax facts from in-memory source; never build or execute a project."""

from bisect import bisect_right
from importlib.metadata import version
from typing import List, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_go

from .models import Fact, SourceRange


SYNTAX_MODEL_VERSION = "go-tree-sitter-{}-grammar-{}-model-1".format(
    version("tree-sitter"), version("tree-sitter-go")
)
_LANGUAGE = Language(tree_sitter_go.language())
_LITERALS = {
    "int_literal", "float_literal", "imaginary_literal", "rune_literal",
    "interpreted_string_literal", "raw_string_literal", "true", "false",
}


def _unparen(node: Node) -> Node:
    while node.type == "parenthesized_expression":
        node = next(child for child in node.named_children if child.type != "comment")
    return node


def _normalize(node: Node, source: bytes):
    if node is None:
        return None
    node = _unparen(node)
    if node.type in _LITERALS or not node.children:
        return [node.type, source[node.start_byte:node.end_byte].decode("utf-8")]
    return [node.type, [
        _normalize(child, source) for child in node.children
        if child.type not in ("comment", ";")
    ]]


def _line_starts(source: bytes) -> List[int]:
    return [0] + [index + 1 for index, byte in enumerate(source) if byte == 0x0A]


def _position(line_starts: List[int], byte_offset: int) -> Tuple[int, int]:
    line_index = bisect_right(line_starts, byte_offset) - 1
    return line_index + 1, byte_offset - line_starts[line_index]


def _location(path: str, node: Node, line_starts: List[int]) -> SourceRange:
    start_line, start_column = _position(line_starts, node.start_byte)
    end_line, end_column = _position(line_starts, node.end_byte)
    return SourceRange(path, start_line, start_column, end_line, end_column)


def _statements(block: Node) -> List[Node]:
    if block is None:
        return []
    return [statement for child in block.named_children if child.type == "statement_list"
            for statement in child.named_children if statement.type != "comment"]


def _subject(node: Node) -> bool:
    node = _unparen(node)
    if node.type == "identifier":
        return True
    return (node.type == "selector_expression"
            and _subject(node.child_by_field_name("operand")))


def _literal_condition(node: Node, source: bytes):
    node = _unparen(node)
    if node.type != "binary_expression":
        return None
    op = node.child_by_field_name("operator").type
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if op == "&&":
        lhs, rhs = _literal_condition(left, source), _literal_condition(right, source)
        if lhs is not None and rhs is not None:
            return ["and", lhs, rhs]
    if op == "==" and _subject(left) and _unparen(right).type in _LITERALS:
        return ["comparison", _normalize(left, source), _normalize(right, source)]
    return None


class _GoFacts:
    def __init__(self, path: str, source: bytes, line_starts: List[int]):
        self.path = path
        self.source = source
        self.line_starts = line_starts
        self.facts: List[Fact] = []

    def _text(self, node: Node) -> str:
        return self.source[node.start_byte:node.end_byte].decode("utf-8")

    def _fact(self, node: Node, symbol: str, kind: str, structure, related=()) -> None:
        self.facts.append(Fact(
            path=self.path, symbol=symbol,
            source_range=_location(self.path, node, self.line_starts),
            fact_type=kind, syntax_model_version=SYNTAX_MODEL_VERSION,
            normalized_structure=structure,
            evidence=self._text(node).splitlines()[0].strip(), related_ranges=related,
        ))

    def _symbol(self, node: Node) -> str:
        name = self._text(node.child_by_field_name("name"))
        if node.type == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            parameter = next(c for c in receiver.named_children if c.type == "parameter_declaration")
            receiver_type = parameter.child_by_field_name("type")
            if receiver_type.type == "pointer_type":
                receiver_type = next(c for c in receiver_type.named_children if c.type != "comment")
            if receiver_type.type == "generic_type":
                receiver_type = receiver_type.child_by_field_name("type")
            return self._text(receiver_type) + "." + name
        return name

    def walk(self, node: Node, symbol: str = "<module>") -> None:
        if node.type == "func_literal":
            return
        if node.type in ("function_declaration", "method_declaration"):
            body = node.child_by_field_name("body")
            if body is not None:
                self.walk(body, self._symbol(node))
            return
        if node.type == "if_statement" and symbol != "<module>":
            self._if_chain(node, symbol)
            return
        for child in node.named_children:
            self.walk(child, symbol)

    def _nested(self, node: Node, symbol: str) -> None:
        if node.child_by_field_name("initializer") is not None:
            return
        outer = _literal_condition(node.child_by_field_name("condition"), self.source)
        if outer is None:
            return
        blocks = [node.child_by_field_name("consequence")]
        alternative = node.child_by_field_name("alternative")
        if alternative is not None and alternative.type == "block":
            blocks.append(alternative)
        for block in blocks:
            for child in _statements(block):
                if child.type != "if_statement" or child.child_by_field_name("initializer") is not None:
                    continue
                inner = _literal_condition(child.child_by_field_name("condition"), self.source)
                if inner is not None:
                    self._fact(node, symbol, "nested_literal_case", {"outer": outer, "inner": inner},
                               (_location(self.path, child, self.line_starts),))

    def _if_chain(self, node: Node, symbol: str) -> None:
        chain = [node]
        alternative = node.child_by_field_name("alternative")
        while alternative is not None and alternative.type == "if_statement":
            chain.append(alternative)
            alternative = alternative.child_by_field_name("alternative")
        if len(chain) >= 2:
            self._fact(node, symbol, "if_chain", {
                "arms": [{
                    "initializer": _normalize(item.child_by_field_name("initializer"), self.source),
                    "test": _normalize(item.child_by_field_name("condition"), self.source),
                    "body": _normalize(item.child_by_field_name("consequence"), self.source),
                } for item in chain],
                "else": _normalize(alternative, self.source),
            })
        for item in chain:
            self._nested(item, symbol)
            self.walk(item.child_by_field_name("consequence"), symbol)
        if alternative is not None:
            self.walk(alternative, symbol)


def parse_go(path: str, content: str) -> Tuple[Fact, ...]:
    source = content.encode("utf-8")
    line_starts = _line_starts(source)
    tree = Parser(_LANGUAGE).parse(source)
    root = tree.root_node
    if root.has_error:
        pending = [root]
        while pending:
            node = pending.pop()
            if node.is_error or node.is_missing:
                line, column = _position(line_starts, node.start_byte)
                raise SyntaxError("Go syntax could not be parsed at {}:{}".format(line, column))
            pending.extend(reversed(node.children))
        raise SyntaxError("Go syntax could not be parsed")
    if not any(child.type == "package_clause" for child in root.named_children):
        raise SyntaxError("Go source is missing a package clause")
    visitor = _GoFacts(path, source, line_starts)
    visitor.walk(root)
    return tuple(visitor.facts)
