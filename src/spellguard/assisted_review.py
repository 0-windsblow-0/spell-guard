"""Assisted review orchestration: prepare/validate strict A-A02 rules.

Contract source: docs/ARCHITECTURE.md §10 (assisted-review-contract) and the
A02/A03 task cards. validate rebuilds the trusted packet from the current
repository (never trusts a submitted packet JSON as-is) and rejects any anchor
(side/path/line/quote) not matching exactly. evidence_status="anchored" +
semantic_status="unverified" is the only positive outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Diagnostic
from .review_packet import build_review_packet

MAX_QUESTIONS = 3
MAX_TEXT = 1000
MAX_ITEMS = 10
MAX_ITEM_TEXT = 500
MAX_ANCHORS = 6
MAX_ANCHOR_LINES = 30
PACKET_INPUT_BYTES = 64 * 1024
RESPONSE_INPUT_BYTES = 256 * 1024
KNOWN_TOP_FIELDS = {"schema_version", "packet_id", "questions", "limitations"}
KNOWN_QUESTION_FIELDS = {"hypothesis", "question", "anchors", "unknowns"}
KNOWN_ANCHOR_FIELDS = {"side", "path", "start_line", "end_line", "quote"}


class _AssistError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


def _diag(code: str, message: str, path: Optional[str] = None) -> dict:
    return {"code": code, "message": message, "path": path}


def _bounded_error(code: str, message: str) -> Dict:
    return {"schema_version": 1, "kind": "state_write_review",
            "complete": False, "questions": [], "limitations": [],
            "diagnostics": [_diag(code, message)]}


def _parse_object(text: str, limit: int, what: str) -> Dict:
    if len(text.encode("utf-8")) > limit:
        raise _AssistError("INVALID_RESPONSE",
                           "{} exceeds size limit".format(what))
    from spellguard.registry import _reject_duplicates
    try:
        schema = json.loads(text, object_pairs_hook=_reject_duplicates)
    except (ValueError, TypeError):
        raise _AssistError("INVALID_RESPONSE", "{} is not valid JSON".format(what))
    if not isinstance(schema, dict):
        raise _AssistError("INVALID_RESPONSE", "{} must be an object".format(what))
    return schema


def prepare_assisted_review(root: Path, expected_digest: Optional[str] = None) -> tuple:
    """Build the trusted packet. Exit 0 on complete, 2 otherwise."""
    from .repository import (
        RepositoryError,
        collect_change_set,
        collect_commit,
        collect_working_tree,
        repository_root,
    )
    from .registry import RegistryError, load_registry
    try:
        actual_root = repository_root(root)
    except RepositoryError as error:
        return _bounded_error("REPOSITORY_ERROR", str(error)), 2
    try:
        base_collected = collect_commit(actual_root, "HEAD")
    except RepositoryError as error:
        return _bounded_error("HEAD_UNAVAILABLE", str(error)), 2
    try:
        current_collected = collect_working_tree(actual_root)
        changes = collect_change_set(actual_root, "HEAD")
    except RepositoryError as error:
        return _bounded_error("REPOSITORY_ERROR", str(error)), 2
    registry = None
    if expected_digest is not None:
        try:
            registry = load_registry(actual_root, expected_digest)
        except RegistryError as error:
            return _bounded_error(error.code, error.message), 2
    packet = build_review_packet(
        base_snap=base_collected.snapshot,
        current_snap=current_collected.snapshot,
        changes=changes, registry=registry)
    complete = current_collected.coverage.complete
    complete = complete and packet["coverage"]["context_complete"]
    packet["limitations"] = list(packet.get("limitations", []))
    if not complete:
        packet["coverage"]["context_complete"] = False
        packet["limitations"].append("working tree collection incomplete")
        packet["complete"] = False
        return packet, 2
    packet["complete"] = True
    packet["diagnostics"] = []
    return packet, 0


def validate_assisted_review(root: Path, packet_path: str, response_path: str,
                             expected_digest: Optional[str] = None) -> tuple:
    """Read both packet/response files safely; rebuild and validate."""
    from .repository import (
        RepositoryError, read_repository_file, repository_root)
    from .registry import RegistryError
    try:
        actual_root = repository_root(root)
        packet_text = read_repository_file(actual_root, packet_path,
                                           PACKET_INPUT_BYTES).decode("utf-8")
        response_text = read_repository_file(actual_root, response_path,
                                             RESPONSE_INPUT_BYTES).decode("utf-8")
    except (RepositoryError, RegistryError, ValueError) as error:
        return _bounded_error("REPOSITORY_ERROR", str(error)), 2
    return validate_review_response(actual_root, packet_text, response_text,
                                    expected_digest)


def validate_review_response(root: Path, packet_text: str, response_text: str,
                             expected_digest: Optional[str] = None) -> tuple:
    """Rebuild the packet fresh from the repo; validate the response."""
    diagnostics: List[dict] = []
    try:
        submitted = _parse_object(packet_text, PACKET_INPUT_BYTES, "packet")
        response = _parse_object(response_text, RESPONSE_INPUT_BYTES, "response")
    except _AssistError as error:
        return _bounded_error(error.code, error.message), 2
    rebuilt, code = prepare_assisted_review(root, expected_digest)
    if code != 0:
        return _bounded_error("STALE_PACKET",
                              "current snapshot could not be prepared"), 2
    submitted_id = submitted.get("packet_id")
    if not isinstance(submitted_id, str) or submitted_id == "":
        diagnostics.append(_diag("INVALID_RESPONSE",
                                 "packet_id missing or not text", "packet"))
    elif submitted_id != rebuilt["packet_id"]:
        return _bounded_error(
            "STALE_PACKET",
            "submitted packet_id does not match the current repository"), 2

    unknown = set(response) - KNOWN_TOP_FIELDS
    if unknown:
        diagnostics.append(_diag("INVALID_RESPONSE",
                                 "unknown response fields: {}".format(
                                     sorted(unknown)), "response"))
    questions = response.get("questions", [])
    if not isinstance(questions, list):
        diagnostics.append(_diag("INVALID_RESPONSE", "questions must be a list",
                                 "response"))
        questions = []
    if len(questions) > MAX_QUESTIONS:
        diagnostics.append(_diag(
            "INVALID_RESPONSE", "at most {} questions".format(MAX_QUESTIONS),
            "response"))
    limitations = response.get("limitations", [])
    if not isinstance(limitations, list) or any(
            not isinstance(item, str) or not item
            for item in limitations):
        diagnostics.append(_diag("INVALID_RESPONSE",
                                 "limitations must be text entries",
                                 "response"))
        limitations = []
    if len(limitations) > MAX_ITEMS or any(
            len(item) > MAX_ITEM_TEXT for item in limitations):
        diagnostics.append(_diag("INVALID_RESPONSE", "limitations out of bounds",
                                 "response"))
        limitations = limitations[:MAX_ITEMS]

    changes_side = {"after": {}, "before": {}}
    for file_entry in rebuilt["files"]:
        path = file_entry["path"]
        if file_entry["before"] is not None:
            changes_side["before"][path] = file_entry["before"]["content"].splitlines()
        if file_entry["after"] is not None:
            changes_side["after"][path] = file_entry["after"]["content"].splitlines()

    valid_questions: List[Dict] = []
    invalid_count = False
    for raw_question in questions:
        if not isinstance(raw_question, dict):
            diagnostics.append(_diag("INVALID_RESPONSE",
                                     "question must be an object", "response"))
            invalid_count = True
            continue
        if set(raw_question) - KNOWN_QUESTION_FIELDS:
            diagnostics.append(_diag("INVALID_RESPONSE",
                                     "unknown question fields", "response"))
            invalid_count = True
            continue
        question_ok = True
        for key in ("hypothesis", "question"):
            value = raw_question.get(key)
            if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
                diagnostics.append(_diag(
                    "INVALID_RESPONSE",
                    "{} must be non-empty text of at most {} "
                    "chars".format(key, MAX_TEXT), "response"))
                question_ok = False
        anchors = raw_question.get("anchors", [])
        if not isinstance(anchors, list) or not (
                isinstance(raw_question.get("unknowns"), list)
                and all(isinstance(u, str) and len(u) <= MAX_ITEM_TEXT
                        and u for u in raw_question["unknowns"])):
            diagnostics.append(_diag("INVALID_RESPONSE", "unknowns must be "
                                     "bounded strings", "response"))
            question_ok = False
        if not isinstance(anchors, list):
            diagnostics.append(_diag("INVALID_RESPONSE", "anchors must be a list",
                                     "response"))
            question_ok = False
            anchors = []
        if len(anchors) > MAX_ANCHORS:
            diagnostics.append(_diag("INVALID_RESPONSE",
                                     "at most {} anchors".format(MAX_ANCHORS),
                                     "response"))
            question_ok = False
        normalized_anchors = []
        for anchor in anchors:
            if not isinstance(anchor, dict) or set(anchor) - KNOWN_ANCHOR_FIELDS:
                diagnostics.append(_diag("INVALID_RESPONSE",
                                         "unsupported anchor fields",
                                         "response"))
                question_ok = False
                continue
            side = anchor.get("side")
            path = anchor.get("path")
            start_line = anchor.get("start_line")
            end_line = anchor.get("end_line")
            quote = anchor.get("quote")
            if side not in ("before", "after"):
                diagnostics.append(_diag("INVALID_RESPONSE",
                                         "side must be before/after",
                                         "response"))
                question_ok = False
                continue
            if path not in changes_side[side]:
                diagnostics.append(_diag("ANCHOR_MISMATCH",
                                         "anchor path {} not in {}: side".format(
                                             path, side), "response"))
                question_ok = False
                continue
            for number in (start_line, end_line):
                if (not isinstance(number, int) or isinstance(number, bool)
                        or number < 1):
                    diagnostics.append(_diag("INVALID_RESPONSE",
                                             "anchor lines must be positive "
                                             "integers", "response"))
                    question_ok = False
            if not (isinstance(end_line, int) and isinstance(start_line, int)
                    and end_line >= start_line):
                diagnostics.append(_diag("INVALID_RESPONSE",
                                         "anchor end_line must be >= start_line",
                                         "response"))
                question_ok = False
            if end_line - start_line + 1 > MAX_ANCHOR_LINES:
                diagnostics.append(_diag("INVALID_RESPONSE",
                                         "anchor range over "
                                         "{} lines".format(MAX_ANCHOR_LINES),
                                         "response"))
                question_ok = False
            if not isinstance(quote, str) or not quote:
                diagnostics.append(_diag("INVALID_RESPONSE",
                                         "anchor quote must be a non-empty "
                                         "string", "response"))
                question_ok = False
                continue
            text_lines = changes_side[side].get(path)
            if text_lines is None or end_line > len(text_lines) or start_line >= 1:
                if text_lines is None or end_line > len(text_lines):
                    diagnostics.append(_diag(
                        "ANCHOR_MISMATCH",
                        "anchor range exceeds file length", "response"))
                    question_ok = False
                    continue
            actual = "\n".join(text_lines[start_line - 1: end_line])
            if actual != quote:
                diagnostics.append(_diag(
                    "ANCHOR_MISMATCH",
                    "quote must be an exact line-range match; got {!r}".format(
                        actual[:80]), "response"))
                question_ok = False
                continue
            normalized_anchors.append({
                "side": side, "path": path,
                "start_line": start_line, "end_line": end_line,
                "quote": quote})
        has_after = any(a.get("side") == "after" for a in normalized_anchors)
        has_before = any(a.get("side") == "before" for a in normalized_anchors)
        if not has_after:
            diagnostics.append(_diag(
                "ANCHOR_MISMATCH",
                "at least one after-change anchor required", "response"))
            question_ok = False
        if not has_before:
            diagnostics.append(_diag(
                "ANCHOR_MISMATCH",
                "a baseline comparison anchor is required to be able to "
                "justify \"new\"", "response"))
            question_ok = False
        after_anchors = [a for a in normalized_anchors
                         if a["side"] == "after"]
        changed = set()
        import_file = None
        for entry in rebuilt["files"]:
            if any(a["path"] == entry["path"] for a in after_anchors):
                import_file = entry
                break
        if import_file is not None:
            changed = set(import_file["changed_after_lines"] or [])
        if import_file is not None and changed and not (
                {a.get("start_line") for a in after_anchors} & set(changed)):
            diagnostics.append(_diag(
                "ANCHOR_MISMATCH",
                "no after anchor touches a modification line", "response"))
            question_ok = False
        if not question_ok:
            invalid_count = True
            continue
        valid_questions.append({
            "hypothesis": raw_question["hypothesis"],
            "question": raw_question["question"],
            "anchors": normalized_anchors,
            "unknowns": list(raw_question["unknowns"]),
        })
    report = {
        "schema_version": 1,
        "command": "validate",
        "packet_id": rebuilt["packet_id"],
        "complete": not diagnostics,
        "questions": valid_questions,
        "limitations": limitations,
        "diagnostics": diagnostics,
        "evidence_status": "anchored" if not diagnostics else "unverified",
        "semantic_status": "unverified",
    }
    if diagnostics or invalid_count:
        return report, 2
    return report, 0
