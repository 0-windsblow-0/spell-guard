"""Deterministic rule implementations and the shared candidate entry point."""

from typing import Sequence, Tuple

from ..models import Fact, Finding
from .exception_return import detect_exception_returns
from .nested_literal_case import detect_nested_literal_cases
from .repeated_decision import detect_repeated_decisions


def detect_findings(facts: Sequence[Fact]) -> Tuple[Finding, ...]:
    findings = list(detect_exception_returns(facts))
    findings.extend(detect_repeated_decisions(facts))
    findings.extend(detect_nested_literal_cases(facts))
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                item.primary_location.path,
                item.primary_location.start_line,
                item.primary_location.start_column,
                item.rule_id,
                item.fingerprint,
            ),
        )
    )


__all__ = [
    "detect_findings",
    "detect_exception_returns",
    "detect_repeated_decisions",
    "detect_nested_literal_cases",
]
