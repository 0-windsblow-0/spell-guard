"""SG001: literal returns inside exception handlers."""

import hashlib
import json
from typing import Any, Sequence, Tuple

from ..models import Fact, Finding


RULE_ID = "SG001"
RULE_VERSION = "1"
FINGERPRINT_VERSION = "1"
_LITERAL_KINDS = {"constant", "list", "tuple", "set", "dict"}


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


def detect_exception_returns(facts: Sequence[Fact]) -> Tuple[Finding, ...]:
    findings = []
    for fact in facts:
        if fact.fact_type != "return":
            continue
        structure = fact.normalized_structure
        if not isinstance(structure, dict) or structure.get("in_exception_handler") is not True:
            continue
        value = structure.get("value")
        if not isinstance(value, dict) or value.get("kind") not in _LITERAL_KINDS:
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
                related_locations=(),
                fact_description="An exception handler contains a literal return.",
                investigation_prompt=(
                    "Check whether the return is reachable, whether finally overrides it, "
                    "and whether callers distinguish failure from a valid result."
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
