"""Deterministic matching of findings from two complete analyses."""

from pathlib import Path
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from .analysis import parse_snapshot
from .models import (
    CollectedSnapshot,
    Delta,
    DeltaEntry,
    Diagnostic,
    Finding,
    AnalysisResult,
    ReviewResult,
)
from .repository import collect_change_set, collect_commit, collect_working_tree
from .rules import detect_findings


def _finding_key(finding: Finding) -> Tuple[str, str, str, str]:
    return (
        finding.rule_id,
        finding.rule_version,
        finding.fingerprint_version,
        finding.fingerprint,
    )


def _comparison_key(
    finding: Finding, mapped_path: Optional[str] = None
) -> Tuple[str, str, str, str, str]:
    path = mapped_path or finding.primary_location.path
    return (
        finding.rule_id,
        finding.rule_version,
        finding.fingerprint_version,
        finding.comparison_key or finding.fingerprint,
        path,
    )


def _finding_order(finding: Finding) -> Tuple[str, int, int, int, int, str]:
    location = finding.primary_location
    return (
        location.path,
        location.start_line,
        location.start_column,
        location.end_line,
        location.end_column,
        finding.fingerprint,
    )


def _group_findings(findings: Sequence[Finding]) -> Dict[Tuple[str, str, str, str], List[Finding]]:
    groups: Dict[Tuple[str, str, str, str], List[Finding]] = {}
    for finding in findings:
        groups.setdefault(_finding_key(finding), []).append(finding)
    for group in groups.values():
        group.sort(key=_finding_order)
    return groups


def compare_findings(
    baseline: Sequence[Finding],
    current: Sequence[Finding],
    renames: Sequence[Tuple[str, str]] = (),
) -> Delta:
    """Match findings while preserving exact and explicit-rename semantics."""

    old_groups = _group_findings(baseline)
    new_groups = _group_findings(current)
    keys = sorted(set(old_groups) | set(new_groups))
    introduced: List[DeltaEntry] = []
    persisting: List[DeltaEntry] = []
    resolved: List[DeltaEntry] = []
    unverified: List[DeltaEntry] = []
    matched_old = set()
    matched_new = set()

    for key in keys:
        old_items = old_groups.get(key, [])
        new_items = new_groups.get(key, [])
        if old_items and new_items:
            if len(old_items) == 1 and len(new_items) == 1:
                matched_old.add(id(old_items[0]))
                matched_new.add(id(new_items[0]))
                persisting.append(
                    DeltaEntry(
                        before=old_items[0],
                        after=new_items[0],
                        reason="exact-fingerprint",
                    )
                )
            else:
                # There is no evidence that any one old instance is the
                # counterpart of any one new instance. Preserve every
                # location as unknown instead of inventing a positional pair.
                for finding in old_items:
                    matched_old.add(id(finding))
                    unverified.append(
                        DeltaEntry(
                            before=finding,
                            after=None,
                            reason="ambiguous-fingerprint",
                        )
                    )
                for finding in new_items:
                    matched_new.add(id(finding))
                    unverified.append(
                        DeltaEntry(
                            before=None,
                            after=finding,
                            reason="ambiguous-fingerprint",
                        )
                    )
    rename_map = dict(renames)
    old_rename_groups: Dict[Tuple[str, str, str, str, str], List[Finding]] = {}
    new_rename_groups: Dict[Tuple[str, str, str, str, str], List[Finding]] = {}
    for finding in baseline:
        new_path = rename_map.get(finding.primary_location.path)
        if new_path is None and finding.group_members:
            new_path = finding.primary_location.path
        if id(finding) not in matched_old and new_path is not None:
            old_rename_groups.setdefault(_comparison_key(finding, new_path), []).append(finding)
    for finding in current:
        if id(finding) not in matched_new:
            new_rename_groups.setdefault(_comparison_key(finding), []).append(finding)

    rename_keys = sorted(set(old_rename_groups) & set(new_rename_groups))
    for key in rename_keys:
        old_items = sorted(old_rename_groups[key], key=_finding_order)
        new_items = sorted(new_rename_groups[key], key=_finding_order)
        if len(old_items) == 1 and len(new_items) == 1:
            old_finding = old_items[0]
            new_finding = new_items[0]
            matched_old.add(id(old_finding))
            matched_new.add(id(new_finding))
            reason = "explicit-rename"
            destination = persisting
            if old_finding.group_members and new_finding.group_members:
                added = Counter(new_finding.group_members) - Counter(old_finding.group_members)
                removed = Counter(old_finding.group_members) - Counter(new_finding.group_members)
                if added:
                    reason = "group-expanded"
                    destination = introduced
                elif removed:
                    reason = "group-reduced"
            destination.append(DeltaEntry(old_finding, new_finding, reason))
        else:
            for finding in old_items:
                matched_old.add(id(finding))
                unverified.append(
                    DeltaEntry(
                        before=finding,
                        after=None,
                        reason="ambiguous-rename",
                    )
                )
            for finding in new_items:
                matched_new.add(id(finding))
                unverified.append(
                    DeltaEntry(
                        before=None,
                        after=finding,
                        reason="ambiguous-rename",
                    )
                )

    for finding in baseline:
        if id(finding) not in matched_old:
            resolved.append(
                DeltaEntry(before=finding, after=None, reason="no-current-match")
            )
    for finding in current:
        if id(finding) not in matched_new:
            introduced.append(
                DeltaEntry(before=None, after=finding, reason="no-baseline-match")
            )

    def entry_order(entry: DeltaEntry) -> Tuple[str, int, int, str]:
        finding = entry.after or entry.before
        location = finding.primary_location
        return (location.path, location.start_line, location.start_column, finding.fingerprint)

    return Delta(
        introduced=tuple(sorted(introduced, key=entry_order)),
        persisting=tuple(sorted(persisting, key=entry_order)),
        resolved=tuple(sorted(resolved, key=entry_order)),
        unverified=tuple(sorted(unverified, key=entry_order)),
    )


def _sort_diagnostics(diagnostics: Sequence[Diagnostic]) -> Tuple[Diagnostic, ...]:
    return tuple(sorted(diagnostics, key=lambda item: (item.path or "", item.code, item.message)))


def _review_diagnostics(
    base_analysis: AnalysisResult, current_analysis: AnalysisResult
) -> Tuple[Diagnostic, ...]:
    diagnostics = list(base_analysis.coverage.diagnostics)
    diagnostics.extend(current_analysis.coverage.diagnostics)
    if not base_analysis.coverage.eligible_files and not current_analysis.coverage.eligible_files:
        diagnostics.append(
            Diagnostic(
                "NO_SUPPORTED_FILES",
                "no supported Python or Go files were available in either review side",
            )
        )
    return _sort_diagnostics(diagnostics)


def _incomplete_delta(delta: Delta) -> Delta:
    uncertain = list(delta.unverified)
    uncertain.extend(
        DeltaEntry(
            before=entry.before,
            after=None,
            reason="incomplete-analysis",
        )
        for entry in delta.resolved
    )

    def entry_order(entry: DeltaEntry) -> Tuple[str, int, int, str]:
        finding = entry.after or entry.before
        location = finding.primary_location
        return (location.path, location.start_line, location.start_column, finding.fingerprint)

    return Delta(
        introduced=delta.introduced,
        persisting=delta.persisting,
        resolved=(),
        unverified=tuple(sorted(uncertain, key=entry_order)),
    )


def analyze_review(root: Path, base_ref: str) -> ReviewResult:
    """Analyze a fixed base and current work tree without mutating either side."""

    base_collected = collect_commit(root, base_ref)
    path_changes = collect_change_set(root, base_ref)
    current_collected = collect_working_tree(
        root,
        ignore_missing_paths=path_changes.deleted_paths,
    )
    base_analysis = parse_snapshot(base_collected)
    current_analysis = parse_snapshot(current_collected)
    change_set = collect_change_set(root, base_ref, current_collected)
    base_findings = detect_findings(base_analysis.facts)
    current_findings = detect_findings(current_analysis.facts)
    delta = compare_findings(
        base_findings,
        current_findings,
        renames=change_set.renames,
    )
    diagnostics = _review_diagnostics(base_analysis, current_analysis)
    complete = (
        base_analysis.coverage.complete
        and current_analysis.coverage.complete
        and bool(
            base_analysis.coverage.eligible_files
            or current_analysis.coverage.eligible_files
        )
    )
    if not complete:
        delta = _incomplete_delta(delta)
    return ReviewResult(
        base_snapshot=base_collected.snapshot,
        current_snapshot=current_collected.snapshot,
        base_analysis=base_analysis,
        current_analysis=current_analysis,
        change_set=change_set,
        base_findings=base_findings,
        current_findings=current_findings,
        delta=delta,
        diagnostics=diagnostics,
        complete=complete,
    )
