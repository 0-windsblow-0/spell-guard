"""Stable text and JSON reports for scan, review, and debt commands."""

import json
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from . import __version__
from .go_analysis import SYNTAX_MODEL_VERSION as GO_SYNTAX_MODEL_VERSION
from .models import (
    ChangeSet,
    Coverage,
    Diagnostic,
    DebtReport,
    DeltaEntry,
    ExceptionEntry,
    Finding,
    GovernanceResult,
    ReviewReport,
    ScanReport,
    Snapshot,
)


SCHEMA_VERSION = "1"
RULESET_VERSION = "candidate-5"
ANALYSIS_MODE = "experimental"
ENABLED_RULES = ("SG001", "SG002", "SG003")


def _analysis_scope() -> Dict[str, Any]:
    return {
        "python": {
            "parser": "python-ast-{}.{}".format(*sys.version_info[:2]),
            "rules": ["SG001", "SG002", "SG003"],
            "source_selection": "Git-visible .py files",
            "limitations": ["Structural candidates, not business correctness or runtime analysis"],
        },
        "go": {
            "parser": GO_SYNTAX_MODEL_VERSION,
            "rules": ["SG002", "SG003"],
            "source_selection": "All Git-visible .go files, including tests, generated and vendor files; build tags and GOOS/GOARCH are not evaluated",
            "limitations": [
                "Only repeated if/else-if chains and directly nested literal equality cases in named functions/methods",
                "No switch/select, closure-body candidates, error-flow, type checking or cross-file analysis",
            ],
        },
    }


def _scope_text() -> list:
    lines = ["analysis scope (syntax coverage is not semantic coverage):"]
    for language, scope in _analysis_scope().items():
        lines.append("- {}: {}; rules={}".format(language, scope["parser"], ", ".join(scope["rules"])))
        lines.append("  sources: {}".format(scope["source_selection"]))
        lines.extend("  limit: {}".format(limit) for limit in scope["limitations"])
    return lines


def _location(location) -> Dict[str, Any]:
    return {
        "path": location.path,
        "start_line": location.start_line,
        "start_column": location.start_column,
        "end_line": location.end_line,
        "end_column": location.end_column,
    }


def _diagnostic(diagnostic: Diagnostic) -> Dict[str, Any]:
    result = {"code": diagnostic.code, "message": diagnostic.message}
    if diagnostic.path is not None:
        result["path"] = diagnostic.path
    return result


def _entry(entry: ExceptionEntry) -> Dict[str, str]:
    return {
        "fingerprint": entry.fingerprint,
        "rule_id": entry.rule_id,
        "reason": entry.reason,
        "owner": entry.owner,
        "created_at": entry.created_at,
        "expires_at": entry.expires_at,
    }


def _governance(result: GovernanceResult) -> Dict[str, Any]:
    return {
        "source": result.source,
        "evaluated_at": result.evaluated_at,
        "statuses": [
            {"fingerprint": fingerprint, "rule_id": rule_id, "status": status}
            for fingerprint, rule_id, status in result.statuses
        ],
        "unmatched": [_entry(entry) for entry in result.unmatched],
    }


def _finding(
    finding: Finding,
    statuses: Dict[Tuple[str, str], str],
    include_governance_status: bool = True,
) -> Dict[str, Any]:
    result = {
        "rule_id": finding.rule_id,
        "rule_version": finding.rule_version,
        "fingerprint_version": finding.fingerprint_version,
        "fingerprint": finding.fingerprint,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "primary_location": _location(finding.primary_location),
        "related_locations": [_location(item) for item in finding.related_locations],
        "fact_description": finding.fact_description,
        "investigation_prompt": finding.investigation_prompt,
    }
    if include_governance_status:
        result["governance_status"] = statuses.get(
            (finding.fingerprint, finding.rule_id), "unregistered"
        )
    if finding.group_members:
        result["group_members"] = list(finding.group_members)
    return result


def _snapshot(snapshot: Snapshot) -> Dict[str, Any]:
    return {
        "identity": snapshot.identity,
        "source": snapshot.source,
        "files": [
            {"path": item.path, "digest": item.digest} for item in snapshot.files
        ],
    }


def _coverage(coverage: Coverage) -> Dict[str, Any]:
    return {
        "eligible_files": coverage.eligible_files,
        "analyzed_files": coverage.analyzed_files,
        "excluded_files": coverage.excluded_files,
        "unsupported_files": coverage.unsupported_files,
        "failed_files": coverage.failed_files,
        "complete": coverage.complete,
    }


def _change_set(change_set: ChangeSet) -> Dict[str, Any]:
    return {
        "base_commit": change_set.base_commit,
        "target_snapshot_identity": change_set.target_snapshot_identity,
        "added_paths": list(change_set.added_paths),
        "modified_paths": list(change_set.modified_paths),
        "deleted_paths": list(change_set.deleted_paths),
        "renames": [
            {"before": before, "after": after}
            for before, after in change_set.renames
        ],
    }


def _delta_entry(
    entry: DeltaEntry, statuses: Dict[Tuple[str, str], str]
) -> Dict[str, Any]:
    result = {"reason": entry.reason}
    if entry.before is not None:
        result["before"] = _finding(entry.before, {}, False)
    else:
        result["before"] = None
    if entry.after is not None:
        result["after"] = _finding(entry.after, statuses, True)
    else:
        result["after"] = None
    return result


def _delta(report: ReviewReport) -> Dict[str, Any]:
    statuses = {
        (fingerprint, rule_id): status
        for fingerprint, rule_id, status in report.governance.statuses
    }
    return {
        "introduced": [
            _delta_entry(item, statuses) for item in report.delta.introduced
        ],
        "persisting": [
            _delta_entry(item, statuses) for item in report.delta.persisting
        ],
        "resolved": [
            _delta_entry(item, statuses) for item in report.delta.resolved
        ],
        "unverified": [
            _delta_entry(item, statuses) for item in report.delta.unverified
        ],
    }


def report_as_dict(report: ScanReport) -> Dict[str, Any]:
    statuses = {
        (fingerprint, rule_id): status
        for fingerprint, rule_id, status in report.governance.statuses
    }
    return {
        "analysis_scope": _analysis_scope(),
        "schema_version": report.schema_version,
        "tool_version": report.tool_version,
        "ruleset_version": report.ruleset_version,
        "snapshot": _snapshot(report.snapshot),
        "coverage": _coverage(report.coverage),
        "findings": [_finding(item, statuses) for item in report.findings],
        "diagnostics": [_diagnostic(item) for item in report.diagnostics],
        "governance": _governance(report.governance),
        "analysis_mode": report.analysis_mode,
        "enabled_rules": list(report.enabled_rules),
    }


def review_report_as_dict(report: ReviewReport) -> Dict[str, Any]:
    statuses = {
        (fingerprint, rule_id): status
        for fingerprint, rule_id, status in report.governance.statuses
    }
    return {
        "analysis_scope": _analysis_scope(),
        "schema_version": report.schema_version,
        "tool_version": report.tool_version,
        "ruleset_version": report.ruleset_version,
        "base_snapshot": _snapshot(report.base_snapshot),
        "current_snapshot": _snapshot(report.current_snapshot),
        "base_coverage": _coverage(report.base_coverage),
        "current_coverage": _coverage(report.current_coverage),
        "change_set": _change_set(report.change_set),
        "base_findings": [
            _finding(item, {}, False) for item in report.base_findings
        ],
        "current_findings": [
            _finding(item, statuses, True) for item in report.current_findings
        ],
        "delta": _delta(report),
        "diagnostics": [_diagnostic(item) for item in report.diagnostics],
        "governance": _governance(report.governance),
        "analysis_mode": report.analysis_mode,
        "enabled_rules": list(report.enabled_rules),
        "complete": report.complete,
    }


def _text(report: ScanReport) -> str:
    lines = [
        "Spellguard scan",
        "analysis_mode: {}".format(report.analysis_mode),
        "snapshot: {}".format(report.snapshot.identity or "unstable"),
        "coverage: eligible={} analyzed={} failed={} unsupported={} complete={}".format(
            report.coverage.eligible_files,
            report.coverage.analyzed_files,
            report.coverage.failed_files,
            report.coverage.unsupported_files,
            str(report.coverage.complete).lower(),
        ),
        "findings: {}".format(len(report.findings)),
    ]
    statuses = {
        (fingerprint, rule_id): status
        for fingerprint, rule_id, status in report.governance.statuses
    }
    for finding in report.findings:
        location = finding.primary_location
        lines.extend(
            [
                "- [{}] {} {}:{}:{} ({})".format(
                    finding.rule_id,
                    finding.severity,
                    location.path,
                    location.start_line,
                    location.start_column,
                    statuses.get((finding.fingerprint, finding.rule_id), "unregistered"),
                ),
                "  fact: {}".format(finding.fact_description),
                "  check: {}".format(finding.investigation_prompt),
            ]
        )
    if report.diagnostics:
        lines.append("diagnostics:")
        for diagnostic in report.diagnostics:
            suffix = " [{}]".format(diagnostic.path) if diagnostic.path else ""
            lines.append("- {}{}: {}".format(diagnostic.code, suffix, diagnostic.message))
    lines.extend(_scope_text())
    lines.append("governance: {}".format(report.governance.source))
    return "\n".join(lines) + "\n"


def render_scan_report(report: ScanReport, format_name: str) -> str:
    if format_name == "json":
        return json.dumps(report_as_dict(report), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if format_name == "text":
        return _text(report)
    raise ValueError("unsupported report format: {}".format(format_name))


def _debt_groups(report: DebtReport) -> Dict[str, list]:
    statuses = {
        (fingerprint, rule_id): status
        for fingerprint, rule_id, status in report.governance.statuses
    }
    groups = {"unregistered": [], "accepted": [], "expired": [], "ambiguous": []}
    for finding in report.findings:
        status = statuses.get((finding.fingerprint, finding.rule_id), "unregistered")
        groups.setdefault(status, []).append(_finding(finding, statuses, True))
    return groups


def debt_report_as_dict(report: DebtReport) -> Dict[str, Any]:
    return {
        "analysis_scope": _analysis_scope(),
        "schema_version": report.schema_version,
        "tool_version": report.tool_version,
        "ruleset_version": report.ruleset_version,
        "snapshot": _snapshot(report.snapshot),
        "coverage": _coverage(report.coverage),
        "findings": [
            _finding(
                item,
                {
                    (fingerprint, rule_id): status
                    for fingerprint, rule_id, status in report.governance.statuses
                },
                True,
            )
            for item in report.findings
        ],
        "debt": _debt_groups(report),
        "diagnostics": [_diagnostic(item) for item in report.diagnostics],
        "governance": _governance(report.governance),
        "analysis_mode": report.analysis_mode,
        "enabled_rules": list(report.enabled_rules),
        "complete": report.complete,
        "candidate_closure": report.candidate_closure,
    }


def _debt_text(report: DebtReport) -> str:
    groups = _debt_groups(report)
    lines = [
        "Spellguard debt",
        "analysis_mode: {}".format(report.analysis_mode),
        "snapshot: {}".format(report.snapshot.identity or "unstable"),
        "complete: {}".format(str(report.complete).lower()),
        "candidate_closure: {}".format(str(report.candidate_closure).lower()),
        "evaluated_at: {}".format(report.governance.evaluated_at),
    ]
    for name in ("unregistered", "accepted", "expired", "ambiguous"):
        lines.append("{}: {}".format(name, len(groups[name])))
        for finding in groups[name]:
            location = finding["primary_location"]
            lines.extend(
                [
                    "- [{}] {} {}:{}:{} ({})".format(
                        finding["rule_id"],
                        finding["severity"],
                        location["path"],
                        location["start_line"],
                        location["start_column"],
                        name,
                    ),
                    "  fact: {}".format(finding["fact_description"]),
                    "  check: {}".format(finding["investigation_prompt"]),
                ]
            )
    if report.governance.unmatched:
        lines.append("unmatched_registry: {}".format(len(report.governance.unmatched)))
        for entry in report.governance.unmatched:
            lines.append(
                "- {} {} ({})".format(
                    entry.rule_id,
                    entry.fingerprint,
                    "candidate-closure" if report.candidate_closure else "unverified",
                )
            )
    if report.diagnostics:
        lines.append("diagnostics:")
        for diagnostic in report.diagnostics:
            suffix = " [{}]".format(diagnostic.path) if diagnostic.path else ""
            lines.append("- {}{}: {}".format(diagnostic.code, suffix, diagnostic.message))
    lines.extend(_scope_text())
    lines.append("governance: {}".format(report.governance.source))
    return "\n".join(lines) + "\n"


def render_debt_report(report: DebtReport, format_name: str) -> str:
    if format_name == "json":
        return json.dumps(
            debt_report_as_dict(report), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
    if format_name == "text":
        return _debt_text(report)
    raise ValueError("unsupported report format: {}".format(format_name))


def _review_text(report: ReviewReport) -> str:
    statuses = {
        (fingerprint, rule_id): status
        for fingerprint, rule_id, status in report.governance.statuses
    }
    lines = [
        "Spellguard review",
        "analysis_mode: {}".format(report.analysis_mode),
        "base_snapshot: {}".format(report.base_snapshot.identity or "unavailable"),
        "current_snapshot: {}".format(
            report.current_snapshot.identity or "unstable"
        ),
        "complete: {}".format(str(report.complete).lower()),
    ]
    for name, entries in (
        ("introduced", report.delta.introduced),
        ("persisting", report.delta.persisting),
        ("resolved", report.delta.resolved),
        ("unverified", report.delta.unverified),
    ):
        lines.append("{}: {}".format(name, len(entries)))
        if name == "unverified":
            ambiguous, other = _split_ambiguous_fingerprint(entries)
            for group in _sort_ambiguous_groups(ambiguous):
                lines.extend(_ambiguous_group_text(group, statuses))
            entries = other
        for entry in entries:
            finding = entry.after or entry.before
            location = finding.primary_location
            status = (
                statuses.get((entry.after.fingerprint, entry.after.rule_id), "unregistered")
                if entry.after is not None
                else "historical"
            )
            lines.append(
                "- [{}] {} {}:{}:{} ({})".format(
                    finding.rule_id,
                    finding.severity,
                    location.path,
                    location.start_line,
                    location.start_column,
                    status,
                )
            )
            lines.append("  reason: {}".format(entry.reason))
            if entry.before is not None and entry.after is not None and entry.after.group_members:
                before_members = entry.before.group_members
                after_members = entry.after.group_members
                lines.append("  group size: {} -> {}".format(len(before_members), len(after_members)))
                added = Counter(after_members) - Counter(before_members)
                if added:
                    lines.append("  new members: {}".format(", ".join(sorted(added.elements()))))
            lines.append("  fact: {}".format(finding.fact_description))
            lines.append("  check: {}".format(finding.investigation_prompt))
    if report.diagnostics:
        lines.append("diagnostics:")
        for diagnostic in report.diagnostics:
            suffix = " [{}]".format(diagnostic.path) if diagnostic.path else ""
            lines.append("- {}{}: {}".format(diagnostic.code, suffix, diagnostic.message))
    lines.extend(_scope_text())
    lines.append("governance: {}".format(report.governance.source))
    return "\n".join(lines) + "\n"


def _finding_identity_key(finding: Finding) -> Tuple[str, str, str, str]:
    return (
        finding.rule_id,
        finding.rule_version,
        finding.fingerprint_version,
        finding.fingerprint,
    )


def _finding_sort_key(finding: Finding) -> Tuple[str, int, int, int, int, str]:
    location = finding.primary_location
    return (
        location.path,
        location.start_line,
        location.start_column,
        location.end_line,
        location.end_column,
        finding.fingerprint,
    )


def _split_ambiguous_fingerprint(
    entries: Sequence[DeltaEntry],
) -> Tuple[Dict[Tuple[str, str, str, str], Dict[str, List[Finding]]], List[DeltaEntry]]:
    groups: Dict[Tuple[str, str, str, str], Dict[str, List[Finding]]] = {}
    other: List[DeltaEntry] = []
    for entry in entries:
        if entry.reason != "ambiguous-fingerprint":
            other.append(entry)
            continue
        finding = entry.after or entry.before
        key = _finding_identity_key(finding)
        group = groups.setdefault(key, {"before": [], "after": []})
        group["after" if entry.after is not None else "before"].append(finding)
    for group in groups.values():
        group["before"].sort(key=_finding_sort_key)
        group["after"].sort(key=_finding_sort_key)
    return groups, other


def _ambiguous_group_sort_key(
    item: Tuple[Tuple[str, str, str, str], Dict[str, List[Finding]]]
) -> Tuple[int, int, str, int, int, Tuple[str, str, str, str]]:
    key, group = item
    before = group["before"]
    after = group["after"]
    change = len(after) - len(before)
    if change > 0:
        priority = 0
    elif change < 0:
        priority = 1
    else:
        priority = 2
    first = (after or before)[0].primary_location
    return (priority, -abs(change), first.path, first.start_line, first.start_column, key)


def _sort_ambiguous_groups(
    groups: Dict[Tuple[str, str, str, str], Dict[str, List[Finding]]]
) -> Iterable[Dict[str, List[Finding]]]:
    return (
        group
        for _, group in sorted(groups.items(), key=_ambiguous_group_sort_key)
    )


def _line_summary(findings: Sequence[Finding]) -> str:
    by_path: Dict[str, List[int]] = {}
    for finding in findings:
        by_path.setdefault(finding.primary_location.path, []).append(
            finding.primary_location.start_line
        )
    return "; ".join(
        "{}: {}".format(path, ", ".join(str(line) for line in lines))
        for path, lines in sorted(by_path.items())
    )


def _ambiguous_group_text(
    group: Dict[str, List[Finding]], statuses: Dict[Tuple[str, str], str]
) -> List[str]:
    before = group["before"]
    after = group["after"]
    representative = (after or before)[0]
    location = representative.primary_location
    status = statuses.get(
        (representative.fingerprint, representative.rule_id), "unregistered"
    )
    before_count = len(before)
    after_count = len(after)
    change = after_count - before_count
    if change > 0:
        change_text = (
            "  count increased by {}; exact new location is unknown.".format(change)
        )
    elif change < 0:
        change_text = (
            "  count decreased by {}; exact removed location is unknown.".format(-change)
        )
    else:
        change_text = "  count unchanged; exact correspondence remains unknown."
    return [
        "- [{} v{}] {} instance correspondence uncertain".format(
            representative.rule_id, representative.rule_version, representative.severity
        ),
        "  path: {}".format(location.path),
        "  baseline: {} location{} — {}".format(
            before_count,
            "" if before_count == 1 else "s",
            _line_summary(before),
        ),
        "  current: {} location{} — {}".format(
            after_count,
            "" if after_count == 1 else "s",
            _line_summary(after),
        ),
        change_text,
        "  governance: {}".format(status),
        "  fact: {}".format(representative.fact_description),
        "  check: {}".format(representative.investigation_prompt),
    ]


def _review_summary(report: ReviewReport) -> str:
    delta = report.delta
    lines = [
        "Spellguard review (experimental)",
        "Baseline: {} -> working tree".format(report.base_snapshot.identity or "unavailable"),
        "introduced: {} | persisting: {} | resolved: {} | unverified: {}".format(
            len(delta.introduced), len(delta.persisting), len(delta.resolved), len(delta.unverified)),
    ]
    changes = report.change_set
    unchanged = not (changes.added_paths or changes.deleted_paths or changes.modified_paths or changes.renames)
    if not report.complete:
        lines.append("Incomplete analysis. Findings below are partial evidence, not a pass.")
    elif unchanged:
        lines.append("No working-tree changes relative to this baseline. Committed changes require an earlier --base.")
    elif not delta.introduced:
        lines.append("No new structural candidates in the supported checks.")
    statuses = {(fingerprint, rule): status for fingerprint, rule, status in report.governance.statuses}
    rank = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(delta.introduced, key=lambda entry: (
        rank.get(entry.after.severity, 3), _finding_sort_key(entry.after), entry.after.rule_id))
    for entry in ordered[:3]:
        finding = entry.after
        location = finding.primary_location
        status = statuses.get((finding.fingerprint, finding.rule_id), "unregistered")
        lines.extend([
            "",
            "{}:{} ({})".format(location.path, location.start_line, status),
            "  fact: {}".format(finding.fact_description),
            "  check: {}".format(finding.investigation_prompt),
        ])
        if entry.before is not None and finding.group_members:
            lines.append("  group size: {} -> {}".format(len(entry.before.group_members), len(finding.group_members)))
            added = Counter(finding.group_members) - Counter(entry.before.group_members)
            if added:
                lines.append("  new members: {}".format(", ".join(sorted(added.elements()))))
    if len(ordered) > 3:
        lines.append("{} more new candidates not expanded here.".format(len(ordered) - 3))
    if delta.unverified:
        lines.append("Some finding identities remain uncertain; inspect --all before drawing conclusions.")
    for diagnostic in report.diagnostics:
        lines.append("{} [{}]: {}".format(diagnostic.code, diagnostic.path or "repository", diagnostic.message))
    lines.extend([
        "",
        "Coverage (base/current): {}/{} files analyzed; unsupported: {}/{}.".format(
            report.base_coverage.analyzed_files, report.current_coverage.analyzed_files,
            report.base_coverage.unsupported_files, report.current_coverage.unsupported_files),
        "Checks: Python structural candidates; Go repeated chains/nested comparisons. No business-correctness guarantee.",
        "Full evidence: spellguard review --all (repeat your --base if specified), or --format json.",
    ])
    return "\n".join(lines) + "\n"


def render_review_report(report: ReviewReport, format_name: str, show_all: bool = False) -> str:
    if format_name == "json":
        return json.dumps(
            review_report_as_dict(report),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
    if format_name == "text":
        return _review_text(report) if show_all else _review_summary(report)
    raise ValueError("unsupported report format: {}".format(format_name))


def render_error_report(format_name: str, code: str, message: str) -> str:
    if format_name == "json":
        return (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "tool_version": __version__,
                    "diagnostics": [{"code": code, "message": message}],
                    "error": True,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    return "error: {}\n".format(message)
