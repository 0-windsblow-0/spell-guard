"""SG002: repeated conditional chains in one source file."""

import hashlib
import json
from typing import Any, Dict, List, Sequence, Tuple

from ..models import Fact, Finding, SourceRange


RULE_ID = "SG002"
RULE_VERSION = "2"
FINGERPRINT_VERSION = "2"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprints(fact: Fact, members: Tuple[str, ...]) -> Tuple[str, str]:
    payload = {
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "syntax_model_version": fact.syntax_model_version,
        "structure": fact.normalized_structure,
    }
    comparison_key = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload["path"] = fact.path
    payload["group_members"] = members
    fingerprint = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return fingerprint, comparison_key


def _fact_order(fact: Fact) -> Tuple[int, int, int, int]:
    location = fact.source_range
    return (
        location.start_line,
        location.start_column,
        location.end_line,
        location.end_column,
    )


def detect_repeated_decisions(facts: Sequence[Fact]) -> Tuple[Finding, ...]:
    groups: Dict[Tuple[str, str, str], List[Fact]] = {}
    for fact in facts:
        if fact.fact_type != "if_chain" or fact.symbol == "<module>":
            continue
        structure = fact.normalized_structure
        groups.setdefault((fact.path, fact.syntax_model_version, _canonical_json(structure)), []).append(fact)

    findings = []
    for _, group in sorted(groups.items()):
        ordered = sorted(group, key=_fact_order)
        if len({fact.symbol for fact in ordered}) < 2:
            continue
        members = tuple(sorted(fact.symbol for fact in ordered))
        fingerprint, comparison_key = _fingerprints(ordered[0], members)
        findings.append(
            Finding(
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                fingerprint_version=FINGERPRINT_VERSION,
                fingerprint=fingerprint,
                severity="medium",
                confidence="high",
                primary_location=ordered[0].source_range,
                related_locations=tuple(fact.source_range for fact in ordered[1:]),
                fact_description=(
                    "The same if/else-if decision chain appears in multiple named "
                    "functions or methods in one Go file."
                    if ordered[0].syntax_model_version.startswith("go-") else
                    "The same if/elif decision chain appears in multiple functions "
                    "in one file."
                ),
                investigation_prompt=(
                    "Check whether the repeated chains express one business decision "
                    "and should share a maintained entry point."
                ),
                comparison_key=comparison_key,
                group_members=members,
            )
        )
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                item.primary_location.path,
                item.primary_location.start_line,
                item.primary_location.start_column,
                item.fingerprint,
            ),
        )
    )
