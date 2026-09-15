"""Validate and associate the optional current-worktree exception registry."""

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Sequence, Tuple

from .models import ExceptionEntry, Finding, GovernanceResult


class GovernanceError(ValueError):
    """Raised when the exception registry is present but invalid."""


REGISTRY_FILENAME = ".spellguard-exceptions.json"
SCHEMA_VERSION = 1
_REQUIRED_FIELDS = (
    "fingerprint",
    "rule_id",
    "reason",
    "owner",
    "created_at",
    "expires_at",
)


def _non_empty_string(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError("{} must be a non-empty string".format(field_name))
    return value


def _parse_date(value, field_name: str) -> date:
    value = _non_empty_string(value, field_name)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise GovernanceError("{} must use YYYY-MM-DD".format(field_name)) from error


def _load_entries(path: Path) -> Tuple[ExceptionEntry, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GovernanceError("could not read {}: {}".format(path, error)) from error

    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise GovernanceError("schema_version must be {}".format(SCHEMA_VERSION))
    raw_entries = data.get("exceptions")
    if not isinstance(raw_entries, list):
        raise GovernanceError("exceptions must be an array")

    entries = []
    fingerprints = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise GovernanceError("exceptions[{}] must be an object".format(index))
        missing = [field for field in _REQUIRED_FIELDS if field not in raw_entry]
        if missing:
            raise GovernanceError(
                "exceptions[{}] missing {}".format(index, ", ".join(missing))
            )
        fingerprint = _non_empty_string(raw_entry["fingerprint"], "fingerprint")
        if fingerprint in fingerprints:
            raise GovernanceError("duplicate fingerprint: {}".format(fingerprint))
        fingerprints.add(fingerprint)
        created_at = _parse_date(raw_entry["created_at"], "created_at")
        expires_at = _parse_date(raw_entry["expires_at"], "expires_at")
        if expires_at <= created_at:
            raise GovernanceError("expires_at must be later than created_at")
        entries.append(
            ExceptionEntry(
                fingerprint=fingerprint,
                rule_id=_non_empty_string(raw_entry["rule_id"], "rule_id"),
                reason=_non_empty_string(raw_entry["reason"], "reason"),
                owner=_non_empty_string(raw_entry["owner"], "owner"),
                created_at=created_at.isoformat(),
                expires_at=expires_at.isoformat(),
            )
        )
    return tuple(sorted(entries, key=lambda item: (item.fingerprint, item.rule_id)))


def load_and_associate(
    root: Path,
    findings: Sequence[Finding],
    evaluated_at: str = "",
) -> GovernanceResult:
    """Load the one current registry and associate it without hiding findings."""

    registry = Path(root) / REGISTRY_FILENAME
    if evaluated_at:
        evaluated_date = _parse_date(evaluated_at, "evaluated_at")
    else:
        evaluated_date = datetime.now(timezone.utc).date()

    if registry.exists():
        entries = _load_entries(registry)
        source = REGISTRY_FILENAME
    else:
        entries = ()
        source = "missing"

    by_key: Dict[Tuple[str, str], ExceptionEntry] = {
        (entry.fingerprint, entry.rule_id): entry for entry in entries
    }
    finding_counts = Counter((finding.fingerprint, finding.rule_id) for finding in findings)
    statuses = []
    matched = set()
    for finding in findings:
        key = (finding.fingerprint, finding.rule_id)
        entry = by_key.get(key)
        if entry is None:
            status = "unregistered"
        elif finding_counts[key] > 1:
            status = "ambiguous"
            matched.add(key)
        elif evaluated_date >= date.fromisoformat(entry.expires_at):
            status = "expired"
            matched.add(key)
        else:
            status = "accepted"
            matched.add(key)
        statuses.append((finding.fingerprint, finding.rule_id, status))

    unmatched = tuple(
        entry for entry in entries if (entry.fingerprint, entry.rule_id) not in matched
    )
    return GovernanceResult(
        source=source,
        evaluated_at=evaluated_date.isoformat(),
        statuses=tuple(sorted(statuses)),
        unmatched=unmatched,
    )
