"""Strict TEMP registry parsing, digesting and safe loading.

Errors raise RegistryError with
machine codes; raw file content is never embedded in exception messages.
"""

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Tuple

REGISTRY_MAX_BYTES = 64 * 1024
MAX_RULES = 1

SUPPORTED_SOURCE_ROOTS = (".", "src")


@dataclass(frozen=True)
class ProtectedSymbol:
    path: str
    symbol: str
    source_root: str


@dataclass(frozen=True)
class TemporaryRule:
    id: str
    classification: str
    lifecycle: str
    reason: str
    desired_state: str
    protected_symbol: ProtectedSymbol
    window: str
    resolution_reason: "str | None"


@dataclass(frozen=True)
class Registry:
    rules: Tuple[TemporaryRule, ...]
    digest: str


class RegistryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


_IDENTIFIER_LABEL = re.compile(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
TEMP_ID_PATTERN = re.compile(pattern=r"^TEMP-\d{3,}$")

SUPPORTED_FIELDS = {
    "schema_version",
    "rules",
}
RULE_FIELDS = frozenset({
    "id", "classification", "lifecycle", "reason", "desired_state",
    "protected_symbol", "window", "resolution_reason",
})
SYMBOL_FIELDS = frozenset({"path", "symbol", "source_root"})


def _reject_duplicates(pairs: Any) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


class _DuplicateKey(Exception):
    pass


def _invalid(message: str) -> RegistryError:
    return RegistryError("REGISTRY_INVALID", message)


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_LABEL.match(value))


def _digest(value: dict) -> str:
    canonical = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_registry(raw: bytes) -> Registry:
    if len(raw) > REGISTRY_MAX_BYTES:
        raise _invalid("registry exceeds 64 KiB limit")
    try:
        schema = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except UnicodeDecodeError:
        raise _invalid("registry is not valid UTF-8")
    except json.JSONDecodeError as error:
        raise _invalid("registry is not valid JSON")
    except _DuplicateKey:
        raise _invalid("registry has a duplicate JSON key")
    if not isinstance(schema, dict):
        raise _invalid("registry root must be an object")
    unknown = set(schema) - SUPPORTED_FIELDS
    if unknown:
        raise _invalid("unsupported registry field: {}".format(sorted(unknown)[0]))
    version = schema.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise _invalid("unsupported schema_version")
    rules_raw = schema.get("rules")
    if not isinstance(rules_raw, list):
        raise _invalid("rules must be a list")
    if len(rules_raw) > MAX_RULES:
        raise _invalid("only one TEMP rule is supported")
    rules: Tuple[TemporaryRule, ...] = tuple(
        _rule(r, index) for index, r in enumerate(rules_raw))
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise _invalid("duplicate rule id")
    return Registry(rules=rules, digest=_digest(schema))


def _rule(raw: Any, index: int) -> TemporaryRule:
    where = "rules[{}]".format(index)
    if not isinstance(raw, dict):
        raise _invalid("{} must be an object".format(where))
    unknown = set(raw) - RULE_FIELDS
    if unknown:
        raise _invalid("{} has unsupported field: {}".format(
            where, sorted(unknown)[0]))
    missing = RULE_FIELDS - set(raw)
    if missing:
        raise _invalid("{} is missing field: {}".format(where, sorted(missing)[0]))
    rid = raw["id"]
    if not isinstance(rid, str) or not TEMP_ID_PATTERN.match(rid):
        raise _invalid("{} has invalid id (expected TEMP-NNN)".format(where))
    for field in ("classification", "lifecycle", "window"):
        if not isinstance(raw[field], str):
            raise _invalid("{} {} must be a string".format(where, field))
    if raw["classification"] != "temporary":
        raise _invalid("{} classification must be temporary".format(where))
    if raw["window"] != "no_external_callers":
        raise _invalid("{} window must be no_external_callers".format(where))
    if raw["lifecycle"] not in ("ACTIVE", "RESOLVED"):
        raise _invalid("{} lifecycle must be ACTIVE or RESOLVED".format(where))
    reason = raw["reason"]
    desired = raw["desired_state"]
    if not isinstance(reason, str) or not reason.strip():
        raise _invalid("{} reason must be a non-empty string".format(where))
    if not isinstance(desired, str) or not desired.strip():
        raise _invalid("{} desired_state must be a non-empty string".format(where))
    resolution = raw["resolution_reason"]
    if raw["lifecycle"] == "ACTIVE":
        if resolution is not None:
            raise _invalid("{} ACTIVE resolution_reason must be null".format(where))
    else:
        if not isinstance(resolution, str) or not resolution.strip():
            raise _invalid(
                "{} RESOLVED requires a non-empty resolution_reason".format(where))
    if not isinstance(raw["protected_symbol"], dict):
        raise _invalid("{} protected_symbol must be an object".format(where))
    symbol_raw = raw["protected_symbol"]
    unknown = set(symbol_raw) - SYMBOL_FIELDS
    if unknown:
        raise _invalid("{} protected_symbol has unsupported field: {}".format(
            where, sorted(unknown)[0]))
    missing = SYMBOL_FIELDS - set(symbol_raw)
    if missing:
        raise _invalid("{} protected_symbol is missing field: {}".format(
            where, sorted(missing)[0]))
    if not _is_identifier(symbol_raw["symbol"]):
        raise _invalid("{} protected_symbol symbol must be a single identifier".format(where))
    protected = _protected_symbol(symbol_raw, where)
    return TemporaryRule(
        id=rid,
        classification=raw["classification"],
        lifecycle=raw["lifecycle"],
        reason=reason,
        desired_state=desired,
        protected_symbol=protected,
        window=raw["window"],
        resolution_reason=resolution,
    )


def _protected_symbol(raw: dict, where: str) -> ProtectedSymbol:
    path = raw["path"]
    if not isinstance(path, str):
        raise _invalid("{} protected_symbol path must be a string".format(where))
    candidate = PurePosixPath(path)
    if path in ("",) or "\\" in path:
        raise _invalid("{} protected_symbol path must be a POSIX path".format(where))
    if PurePosixPath(path).is_absolute() or path.startswith("/"):
        raise _invalid("{} protected_symbol path must be repository relative".format(where))
    if ".." in candidate.parts:
        raise _invalid("{} protected_symbol path must not traverse up".format(where))
    if candidate.parts and candidate.parts[0] == ".":
        raise _invalid("{} protected_symbol path must not start with '.'".format(where))
    source_root = raw["source_root"]
    if not isinstance(source_root, str) or (
            not source_root or (source_root != "." and (
                source_root.startswith(".") or source_root.startswith("/")
                or ".." in source_root or "\\" in source_root))):
        raise _invalid(
            "{} protected_symbol source_root must be '.' or a plain "
            "repo-relative package root (e.g. src or backend)".format(where))
    if source_root == ".":
        normalized = PurePosixPath(path)
        if len(normalized.parts) != 1:
            raise _invalid(
                "{} with source_root '.', path must be a single repo-relative file".format(where))
        normalized = normalized
    else:
        # Generic repo-relative package roots (src, backend, ...): the path
        # must lie inside source_root. A single file directly inside
        # source_root is allowed for Go (module root = source root).
        if candidate.parts[0] != source_root:
            raise _invalid("{} path must lie inside source_root".format(where))
        relative = candidate.parts[1:]
        if len(relative) < 2 and (
                candidate.suffix not in (".go",) + (".js", ".jsx", ".mjs", ".ts", ".tsx", ".mts")):
            raise _invalid("{} definition must live in a package under source_root".format(where))
        normalized = PurePosixPath(*relative)
    is_go = normalized.suffix == ".go"
    js_suffixes = (".js", ".jsx", ".mjs", ".ts", ".tsx", ".mts")
    java_suffixes = (".java",)
    c_suffixes = (".c", ".cpp", ".cxx", ".cc")
    if normalized.suffix not in (".py", ".go") and normalized.suffix not in js_suffixes \
            and normalized.suffix not in java_suffixes \
            and normalized.suffix not in c_suffixes:
        raise _invalid(
            "{} protected_symbol path must end in .py, .go, .js, .jsx, "
            ".mjs, .ts, .tsx, .mts, .java, or .c/.cc/.cpp/.cxx".format(where))
    is_js = normalized.suffix in js_suffixes
    is_java = normalized.suffix in java_suffixes
    is_c = normalized.suffix in c_suffixes
    if normalized.name == "__init__.py":
        raise _invalid("{} protecting __init__.py exports is not supported".format(where))
    if is_c:
        identifier = raw.get("symbol")
        if not isinstance(identifier, str) or not _IDENTIFIER_LABEL.match(
                identifier):
            raise _invalid("{} C symbol must be a plain identifier".format(where))
        if identifier.startswith("main"):
            pass
    if is_java:
        identifier = raw.get("symbol")
        if not isinstance(identifier, str) or not _IDENTIFIER_LABEL.match(
                identifier):
            raise _invalid(
                "{} Java symbol must be a plain identifier (Type.method "
                "unsupported)".format(where))
    if is_js:
        identifier = raw.get("symbol")
        if not isinstance(identifier, str) or not _IDENTIFIER_LABEL.match(
                identifier):
            raise _invalid(
                "{} JS symbol must be a plain identifier".format(where))
        if identifier in ("default",):
            raise _invalid("{} default/anonymous JS exports are not "
                           "protectable".format(where))
    if is_go:
        # Phase 0 Go window: identifier rules per the G01 card. Nested
        # package paths stay fine (source_root-scoped); the Go-specific
        # restrictions apply to the symbol itself.
        identifier = raw.get("symbol")
        if not isinstance(identifier, str) or not _IDENTIFIER_LABEL.match(identifier):
            raise _invalid(
                "{} Go symbol must be a plain identifier".format(where))
        if identifier in ("init", "_", "iota"):
            raise _invalid("{} Go symbol names {} are not protectable".format(
                where, identifier))
        if "." in identifier:
            raise _invalid(
                "{} method-qualified Go symbols are not supported".format(where))
        if candidate.parts and candidate.parts[0] == "cmd":
            pass  # cmd/ identity comes from module name, checked at evaluation
        # Go: either "src" (tracking Python repo layout) or a package-root dir
        # like backend/internal/legacy: the Go card registers file under the
        # module root; any normal repo-relative subdirectory works because the
        # module name comes from go.mod metadata, and the "package" boundary is
        # the file's own `package` clause. Keep source_root contract as-is.
        parent = normalized.parent
        if str(parent) in (".", ""):
            raise _invalid(
                "{} Go definition must live inside source_root's package "
                "directory".format(where))
    return ProtectedSymbol(
        path=path,
        symbol=raw["symbol"],
        source_root=source_root,
    )


def load_registry(root: Path, expected_digest: str) -> Registry:
    path = PurePosixPath(".spellguard/rules.json")
    if not re.match(r"^[0-9a-f]{64}$", expected_digest or ""):
        raise RegistryError("REGISTRY_CHANGED", "registry digest shape is invalid")
    from . import repository as _repository
    try:
        raw = _repository.read_repository_file(root, str(path), REGISTRY_MAX_BYTES)
    except RegistryError:
        raise
    except Exception:
        raise RegistryError("REGISTRY_READ_FAILED", "registry file could not be read")
    registry = parse_registry(raw)
    if registry.digest != expected_digest:
        raise RegistryError(
            "REGISTRY_CHANGED",
            "registry content does not match the digest confirmed at install time")
    return registry
