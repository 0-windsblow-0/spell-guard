"""Shared immutable values for the repository and analysis boundaries."""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: Optional[str] = None


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: str
    digest: str


@dataclass(frozen=True)
class Snapshot:
    identity: str
    source: str
    files: Tuple[SourceFile, ...]


@dataclass(frozen=True)
class Coverage:
    eligible_files: int
    analyzed_files: int
    excluded_files: int
    unsupported_files: int
    failed_files: int
    diagnostics: Tuple[Diagnostic, ...]
    complete: bool


@dataclass(frozen=True)
class CollectedSnapshot:
    snapshot: Snapshot
    coverage: Coverage


@dataclass(frozen=True)
class ChangeSet:
    base_commit: str
    target_snapshot_identity: str
    added_paths: Tuple[str, ...]
    modified_paths: Tuple[str, ...]
    deleted_paths: Tuple[str, ...]
    renames: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class SourceRange:
    path: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True)
class Fact:
    path: str
    symbol: str
    source_range: SourceRange
    fact_type: str
    syntax_model_version: str
    normalized_structure: object
    evidence: str
    related_ranges: Tuple[SourceRange, ...] = ()


@dataclass(frozen=True)
class AnalysisResult:
    facts: Tuple[Fact, ...]
    coverage: Coverage


@dataclass(frozen=True)
class Finding:
    rule_id: str
    rule_version: str
    fingerprint_version: str
    fingerprint: str
    severity: str
    confidence: str
    primary_location: SourceRange
    related_locations: Tuple[SourceRange, ...]
    fact_description: str
    investigation_prompt: str
    comparison_key: str = ""
    group_members: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DeltaEntry:
    before: Optional[Finding]
    after: Optional[Finding]
    reason: str


@dataclass(frozen=True)
class Delta:
    introduced: Tuple[DeltaEntry, ...]
    persisting: Tuple[DeltaEntry, ...]
    resolved: Tuple[DeltaEntry, ...]
    unverified: Tuple[DeltaEntry, ...]


@dataclass(frozen=True)
class ReviewResult:
    base_snapshot: Snapshot
    current_snapshot: Snapshot
    base_analysis: AnalysisResult
    current_analysis: AnalysisResult
    change_set: ChangeSet
    base_findings: Tuple[Finding, ...]
    current_findings: Tuple[Finding, ...]
    delta: Delta
    diagnostics: Tuple[Diagnostic, ...]
    complete: bool


@dataclass(frozen=True)
class ExceptionEntry:
    fingerprint: str
    rule_id: str
    reason: str
    owner: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class GovernanceResult:
    source: str
    evaluated_at: str
    statuses: Tuple[Tuple[str, str, str], ...]
    unmatched: Tuple[ExceptionEntry, ...]


@dataclass(frozen=True)
class ReviewReport:
    schema_version: str
    tool_version: str
    ruleset_version: str
    base_snapshot: Snapshot
    current_snapshot: Snapshot
    base_coverage: Coverage
    current_coverage: Coverage
    change_set: ChangeSet
    base_findings: Tuple[Finding, ...]
    current_findings: Tuple[Finding, ...]
    delta: Delta
    diagnostics: Tuple[Diagnostic, ...]
    governance: GovernanceResult
    analysis_mode: str
    enabled_rules: Tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class ScanReport:
    schema_version: str
    tool_version: str
    ruleset_version: str
    snapshot: Snapshot
    coverage: Coverage
    findings: Tuple[Finding, ...]
    diagnostics: Tuple[Diagnostic, ...]
    governance: GovernanceResult
    analysis_mode: str
    enabled_rules: Tuple[str, ...]


@dataclass(frozen=True)
class DebtReport:
    schema_version: str
    tool_version: str
    ruleset_version: str
    snapshot: Snapshot
    coverage: Coverage
    findings: Tuple[Finding, ...]
    diagnostics: Tuple[Diagnostic, ...]
    governance: GovernanceResult
    analysis_mode: str
    enabled_rules: Tuple[str, ...]
    complete: bool
    candidate_closure: bool
