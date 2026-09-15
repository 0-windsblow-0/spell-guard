"""SG003: nested literal special cases in one function."""

import hashlib
import json
from typing import Any, Sequence, Tuple

from ..models import Fact, Finding


RULE_ID = "SG003"
RULE_VERSION = "1"
FINGERPRINT_VERSION = "1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprints(fact: Fact) -> Tuple[str, str]:
    payload = {
        "rule_id": RULE_ID,
        "rule_version": RULE_VERSION,
        "fingerprint_version": FINGERPRINT_VERSION,
        "syntax_model_version": fact.syntax_model_version,
        "symbol": fact.symbol,
        "structure": fact.normalized_structure,
    }
    comparison_key = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    payload["path"] = fact.path
    fingerprint = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return fingerprint, comparison_key


def detect_nested_literal_cases(facts: Sequence[Fact]) -> Tuple[Finding, ...]:
    findings = []
    for fact in facts:
        if fact.fact_type != "nested_literal_case":
            continue
        fingerprint, comparison_key = _fingerprints(fact)
        findings.append(
            Finding(
                rule_id=RULE_ID,
                rule_version=RULE_VERSION,
                fingerprint_version=FINGERPRINT_VERSION,
                fingerprint=fingerprint,
                severity="medium",
                confidence="high",
                primary_location=fact.source_range,
                related_locations=fact.related_ranges,
                fact_description=(
                    "A literal equality special case is nested directly inside "
                    "another literal equality special case."
                ),
                investigation_prompt=(
                    "Check whether the inner exception is part of the same decision "
                    "policy and whether its scope and exit condition are documented."
                ),
                comparison_key=comparison_key,
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
