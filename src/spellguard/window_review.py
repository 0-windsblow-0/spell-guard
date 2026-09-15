"""Orchestration of check/context CLI around the window checker.

Reads the registry twice (before and after snapshot collection) and compares
digests so a self-modified registry cannot become a pass. Reuses the existing
repository collection; own analysis only. Reports preserve current evidence and explain incomplete comparisons.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Diagnostic, Snapshot
from .registry import Registry, RegistryError, TemporaryRule, load_registry, parse_registry
from .repository import (
    RepositoryError,
    collect_commit,
    collect_working_tree,
    repository_root,
)
from .windows import Consumer, WindowResult, check_window

UNKNOWN_CODES = {
    "PYTHON_SYNTAX_ERROR", "SYMBOL_UNVERIFIED", "UNSUPPORTED_REFERENCE"}


def _snapshot_is_complete(coverage) -> bool:
    return coverage.complete


def load_registry_twice(root: Path, expected_digest: str) -> Registry:
    """Read the registry and validate against the installed digest.

    The second read happens in review_windows after collecting the working
    tree; this helper exists so tests can wrap it.
    """
    return load_registry(root, expected_digest)


def _consumer_key(consumer: Consumer) -> Tuple[str, str]:
    return (consumer.path, consumer.symbol)


def _added_consumers(current: WindowResult, base: Optional[WindowResult]
                     ) -> Optional[List[Dict]]:
    if base is None:
        return None
    current_keys = {_consumer_key(c) for c in current.consumers}
    base_keys = {_consumer_key(c) for c in base.consumers}
    added = sorted(current_keys - base_keys)
    return [{"path": path, "symbol": symbol} for path, symbol in added]


def _consumers_payload(result: WindowResult) -> List[Dict]:
    return [{"path": consumer.path, "symbol": consumer.symbol,
             "lines": list(consumer.lines)}
            for consumer in result.consumers]


def _diagnostics_payload(result: WindowResult) -> List[Dict]:
    return [{"code": diagnostic.code, "message": diagnostic.message,
             "path": diagnostic.path}
            for diagnostic in result.diagnostics]


def _result_for_check(rule: TemporaryRule, result: WindowResult,
                      added: Optional[List[Dict]]) -> Dict:
    return {
        "rule_id": result.rule_id,
        "status": result.status,
        "complete": result.complete,
        "protected_symbol": {
            "path": rule.protected_symbol.path,
            "symbol": rule.protected_symbol.symbol,
            "source_root": rule.protected_symbol.source_root,
        },
        "reason": rule.reason,
        "desired_state": rule.desired_state,
        "consumers": _consumers_payload(result),
        "added_consumers": added,
        "diagnostics": _diagnostics_payload(result),
    }


def _registry_failure(report_command: str, error: RegistryError,
                      extra: List[Dict]) -> Tuple[Dict, int]:
    diagnostics = [{
        "code": error.code, "message": error.message, "path": ".spellguard/rules.json",
    } ] + extra
    return {
        "schema_version": 1,
        "command": report_command,
        "registry_digest": None,
        "base_commit": None,
        "snapshot_identity": None,
        "analysis_scope": "python-direct-calls-v1",
        "complete": False,
        "results": [],
        "rules": [],
        "diagnostics": diagnostics,
    }, 2


def _git_working_paths(root: Path) -> set:
    import subprocess
    raw = subprocess.run(
        ["git", "-C", os.fspath(root), "ls-files", "-z"],
        check=True, stdout=subprocess.PIPE).stdout
    return {os.fsdecode(part) for part in raw.split(b"\0") if part}


def review_windows(root: Path, expected_digest: str) -> Tuple[Dict, int]:
    """Run check for the whole working tree against HEAD."""
    registry: Optional[Registry] = None
    failures: List[Dict] = []
    try:
        actual_root = repository_root(root)
        registry = load_registry_twice(actual_root, expected_digest)
    except RegistryError as error:
        return _registry_failure("check", error, failures)
    except RepositoryError as error:
        failures.append({"code": "REPOSITORY_ERROR", "message": str(error),
                         "path": None})
        return _registry_failure("check", RegistryError(
            "REGISTRY_READ_FAILED", "registry could not be read"), failures)

    include_go_metadata = any(
        rule.protected_symbol.path.endswith(".go")
        for rule in registry.rules)
    head: Optional[str] = None
    base_collected = None
    base_paths = set()
    try:
        base_collected = collect_commit(
            actual_root, "HEAD",
            include_go_metadata=include_go_metadata)
        head = base_collected.snapshot.identity
        base_paths = {f.path for f in base_collected.snapshot.files}
    except RepositoryError:
        head = None

    deleted_paths: list = []
    if head is not None:
        try:
            from .repository import collect_change_set
            deleted_paths = list(collect_change_set(actual_root, "HEAD").deleted_paths)
        except RepositoryError:
            deleted_paths = []
    try:
        current = collect_working_tree(
            actual_root, ignore_missing_paths=deleted_paths,
            include_go_metadata=include_go_metadata)
    except RepositoryError as error:
        failures.append({"code": "REPOSITORY_ERROR", "message": str(error),
                         "path": None})
        return _registry_failure("check", RegistryError(
            "REGISTRY_READ_FAILED", "registry could not be read"), failures)

    complete_extra = True
    if head is None:
        complete_extra = False
        collection_diagnostics = [{
            "code": "HEAD_UNAVAILABLE",
            "message": "repository has no HEAD commit; added consumers unknown",
            "path": None}]
    else:
        collection_diagnostics = []
    collection_diagnostics.extend(
        {"code": d.code, "message": d.message, "path": d.path}
        for d in current.coverage.diagnostics)
    if not current.coverage.complete:
        complete_extra = False
    if base_collected is not None and not base_collected.coverage.complete:
        complete_extra = False
        collection_diagnostics.extend(
            {"code": d.code, "message": d.message, "path": d.path}
            for d in base_collected.coverage.diagnostics)

    try:
        verified = load_registry_twice(actual_root, expected_digest)
    except RegistryError as error:
        return _registry_failure("check", error, failures)
    digest_matches = verified.digest == registry.digest

    results: List[Dict] = []
    any_incomplete = not complete_extra or not digest_matches
    exit_code = 0
    for rule in verified.rules:
        current_result = check_window(current.snapshot, rule)
        base_result: Optional[WindowResult] = None
        base_status: Optional[str] = None
        base_diagnostics: List[Dict] = []
        if base_collected is not None:
            try:
                base_result = check_window(base_collected.snapshot, rule)
                base_status = base_result.status
                base_diagnostics = [
                    {"code": d.code, "message": "baseline: " + d.message,
                     "path": d.path}
                    for d in base_result.diagnostics]
            except Exception:
                base_status = None
        baseline_untrusted = (
            base_status is None or base_result is None or
            not base_result.complete or base_status == "UNVERIFIED")
        if baseline_untrusted:
            # The current side alone cannot establish trust when the baseline
            # could not be evaluated for the same rule. Keep the
            # real baseline diagnostics: only add the generic note when
            # the baseline produced no diagnostics of its own.
            real_base_diags = list(base_diagnostics)
            if not real_base_diags:
                base_diagnostics = [{
                    "code": "SYMBOL_UNVERIFIED",
                    "message": "baseline could not be evaluated for this "
                               "rule; added consumers unknown",
                    "path": rule.protected_symbol.path}]
            else:
                base_diagnostics = real_base_diags
            any_incomplete = True
            added = None
        else:
            added = _added_consumers(current_result, base_result)
        payload = _result_for_check(rule, current_result, added)
        payload["diagnostics"].extend(base_diagnostics)
        results.append(payload)
        if not current_result.complete:
            any_incomplete = True
        if current_result.status == "UNVERIFIED":
            any_incomplete = True
        elif current_result.status == "VIOLATED":
            exit_code = max(exit_code, 1)
    if any_incomplete:
        exit_code = 2
    report = {
        "schema_version": 1,
        "command": "check",
        "registry_digest": expected_digest if digest_matches else None,
        "base_commit": head,
        "snapshot_identity": current.snapshot.identity,
        "analysis_scope": "python-direct-calls-v1",
        "complete": not any_incomplete,
        "results": results,
        "diagnostics": collection_diagnostics,
    }
    for payload in results:
        for diagnostic in payload["diagnostics"]:
            report["diagnostics"].append(diagnostic)
    if not digest_matches:
        report["diagnostics"].append({
            "code": "REGISTRY_CHANGED",
            "message": "registry changed while collecting the snapshot",
            "path": ".spellguard/rules.json"})
    return report, exit_code


def window_context(root: Path, expected_digest: str) -> Tuple[Dict, int]:
    """Report ACTIVE rules without analyzing source code."""
    try:
        actual_root = repository_root(root)
        registry = load_registry(actual_root, expected_digest)
    except RegistryError as error:
        return _registry_failure("context", error, [])
    except RepositoryError:
        return _registry_failure("context", RegistryError(
            "REGISTRY_READ_FAILED", "registry could not be read"), [])
    rules = [{
        "id": rule.id,
        "reason": rule.reason,
        "desired_state": rule.desired_state,
        "protected_symbol": {
            "path": rule.protected_symbol.path,
            "symbol": rule.protected_symbol.symbol,
            "source_root": rule.protected_symbol.source_root,
        },
        "window": rule.window,
    } for rule in registry.rules if rule.lifecycle == "ACTIVE"]
    return {
        "schema_version": 1,
        "command": "context",
        "registry_digest": expected_digest,
        "complete": True,
        "rules": rules,
        "diagnostics": [],
    }, 0


def render_window_report(report: Dict, format_name: str) -> str:
    if format_name == "json":
        return json.dumps(report, ensure_ascii=False, sort_keys=True,
                          indent=2) + "\n"
    if report.get("command") == "context":
        rules = report.get("rules", [])
        if not rules:
            lines = ["[{}] {}: {}".format(
                d.get("code"), d.get("path") or "repository", d.get("message"))
                for d in report.get("diagnostics", [])]
            return "\n".join(lines) + ("\n" if lines else "")
        rule = rules[0]
        path = rule["protected_symbol"]["path"].rsplit(".", 1)[0]
        module = path.replace("/", ".")
        return (
            "1 temporary rule: {} protects {}.{}; run check after changes.\n"
            .format(rule["id"], module, rule["protected_symbol"]["symbol"]))
    lines: List[str] = []
    for result in report.get("results", []):
        if result["status"] == "VIOLATED":
            lines.append("{}: temporary rule {}".format(
                result["protected_symbol"]["path"], result["rule_id"]))
            lines.append("  because: {}".format(result["reason"]))
            for consumer in result["consumers"]:
                lines.append("  external call: {} ({})".format(
                    consumer["path"], consumer["symbol"]))
            if result["added_consumers"] is None:
                lines.append("  new consumers unknown: baseline unavailable")
            elif result["added_consumers"]:
                lines.append("  new consumers since HEAD: {}".format(
                    ", ".join("{}/{}".format(a["path"], a["symbol"])
                              for a in result["added_consumers"])))
            else:
                lines.append("  no new consumers since HEAD")
            lines.append(
                "  Should this temporary implementation become a "
                "cross-file dependency?")
        elif result["status"] == "UNVERIFIED":
            for diagnostic in result["diagnostics"]:
                lines.append("[{}] {}: {}".format(
                    diagnostic["code"], diagnostic["path"] or "repository",
                    diagnostic["message"]))
    for diagnostic in report.get("diagnostics", []):
        lines.append("[{}] {}: {}".format(
            diagnostic["code"], diagnostic["path"] or "repository",
            diagnostic["message"]))
    return "\n".join(lines) + ("\n" if lines else "")
