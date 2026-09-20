"""Command-line orchestration for the implemented scan path."""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .analysis import parse_snapshot
from .governance import GovernanceError, load_and_associate
from .models import AnalysisResult, DebtReport, Diagnostic, ReviewReport, ScanReport
from .reporting import (
    ANALYSIS_MODE,
    ENABLED_RULES,
    RULESET_VERSION,
    render_error_report,
    render_debt_report,
    render_review_report,
    render_scan_report,
)
from .repository import RepositoryError, collect_working_tree, repository_root
from .registry import RegistryError
from .review import analyze_review
from .window_review import render_window_report, review_windows, window_context
from .rules import detect_findings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spellguard")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    scan = commands.add_parser("scan")
    scan.add_argument("--format", choices=("text", "json"), default="text")

    review = commands.add_parser("review")
    review.add_argument("--base", default="HEAD", help="baseline commit (default: HEAD, compared with the working tree)")
    review.add_argument("--all", action="store_true", help="show all evidence, including existing and uncertain findings")
    review.add_argument("--format", choices=("text", "json"), default="text")
    review.add_argument("--fail-on", choices=("medium", "high"))

    debt = commands.add_parser("debt")
    debt.add_argument("--format", choices=("text", "json"), default="text")

    check = commands.add_parser("check")
    check.add_argument("--registry-sha256", required=True,
                       help="registry digest confirmed at install time")
    check.add_argument("--format", choices=("text", "json"), default="text")

    context = commands.add_parser("context")
    context.add_argument("--registry-sha256", required=True,
                         help="registry digest confirmed at install time")
    context.add_argument("--format", choices=("text", "json"), default="text")

    hook = commands.add_parser(
        "hook", help="single-host adapter (manual setup, reads stdin JSON)")
    hook.add_argument("--host", required=True, choices=("codex", "claude", "cursor"))
    hook.add_argument("--repo", required=True,
                      help="absolute repository root this installation protects")
    hook.add_argument("--state-dir", required=True,
                      help="absolute directory outside the repo for state")
    hook.add_argument("--registry-sha256", required=True,
                      help="registry digest confirmed at install time")

    commands.add_parser("demo", help="synthetic end-to-end demo, self-cleaning")

    assist = commands.add_parser(
        "assist", help="delegate review: prepare evidence, validate response")
    assist_sub = assist.add_subparsers(dest="assist_command")
    prepare_parser = assist_sub.add_parser("prepare",
                                           help="build the evidence packet")
    prepare_parser.add_argument(
        "--registry-sha256", help="optional confirmed registry digest")
    prepare_parser.add_argument("--format", choices=("json",), default="json")
    validate_parser = assist_sub.add_parser(
        "validate", help="validate review response anchors")
    validate_parser.add_argument(
        "--packet", default=".spellguard/assist/packet.json")
    validate_parser.add_argument(
        "--response", default=".spellguard/assist/response.json")
    validate_parser.add_argument(
        "--registry-sha256", help="optional confirmed registry digest")
    validate_parser.add_argument("--format", choices=("json",), default="json")

    propose = commands.add_parser(
        "propose", help="draft an unconfirmed TEMP proposal (AI mark)")
    propose.add_argument("--path", required=True)
    propose.add_argument("--symbol", required=True)
    propose.add_argument("--source-root", required=True)
    propose.add_argument("--reason", required=True)
    propose.add_argument("--desired-state", required=True)
    propose.add_argument("--format", choices=("text", "json"), default="text")

    confirm = commands.add_parser(
        "confirm", help="promote the shown proposal to the real registry")
    confirm.add_argument("--proposal-sha256", required=True)
    confirm.add_argument("--format", choices=("text", "json"), default="text")

    instructions_parser = commands.add_parser(
        "instructions", help="host-independent marking rules")
    instructions_parser.add_argument(
        "--format", choices=("text", "json"), default="text")
    return parser


def _emit_error(format_name: str, code: str, message: str) -> int:
    output = render_error_report(format_name, code, message)
    if format_name == "json":
        sys.stdout.write(output)
    else:
        sys.stderr.write(output)
    return 2


def _mark_no_supported_files(result: AnalysisResult) -> AnalysisResult:
    if result.coverage.eligible_files:
        return result
    diagnostic = Diagnostic(
        "NO_SUPPORTED_FILES",
        "no supported Python or Go files were available for analysis",
    )
    coverage = replace(
        result.coverage,
        diagnostics=tuple(sorted(result.coverage.diagnostics + (diagnostic,), key=lambda item: (item.path or "", item.code, item.message))),
        complete=False,
    )
    return AnalysisResult(result.facts, coverage)


def _scan(format_name: str) -> int:
    try:
        root = repository_root(Path.cwd())
        collected = collect_working_tree(root)
        analyzed = parse_snapshot(collected)
        analyzed = _mark_no_supported_files(analyzed)
        findings = detect_findings(analyzed.facts)
        governance = load_and_associate(root, findings)
    except RepositoryError as error:
        return _emit_error(format_name, "REPOSITORY_ERROR", str(error))
    except GovernanceError as error:
        return _emit_error(format_name, "GOVERNANCE_ERROR", str(error))

    report = ScanReport(
        schema_version="1",
        tool_version=__version__,
        ruleset_version=RULESET_VERSION,
        snapshot=collected.snapshot,
        coverage=analyzed.coverage,
        findings=findings,
        diagnostics=analyzed.coverage.diagnostics,
        governance=governance,
        analysis_mode=ANALYSIS_MODE,
        enabled_rules=ENABLED_RULES,
    )
    sys.stdout.write(render_scan_report(report, format_name))
    return 0 if analyzed.coverage.complete else 2


def _review(format_name: str, base_ref: str, fail_on: Optional[str], show_all: bool = False) -> int:
    try:
        root = repository_root(Path.cwd())
        result = analyze_review(root, base_ref)
        governance = load_and_associate(root, result.current_findings)
    except RepositoryError as error:
        return _emit_error(format_name, "REPOSITORY_ERROR", str(error))
    except GovernanceError as error:
        return _emit_error(format_name, "GOVERNANCE_ERROR", str(error))

    report = ReviewReport(
        schema_version="1",
        tool_version=__version__,
        ruleset_version=RULESET_VERSION,
        base_snapshot=result.base_snapshot,
        current_snapshot=result.current_snapshot,
        base_coverage=result.base_analysis.coverage,
        current_coverage=result.current_analysis.coverage,
        change_set=result.change_set,
        base_findings=result.base_findings,
        current_findings=result.current_findings,
        delta=result.delta,
        diagnostics=result.diagnostics,
        governance=governance,
        analysis_mode=ANALYSIS_MODE,
        enabled_rules=ENABLED_RULES,
        complete=result.complete,
    )
    sys.stdout.write(render_review_report(report, format_name, show_all=show_all))
    if not result.complete:
        return 2
    if fail_on:
        threshold = {"medium": 1, "high": 2}[fail_on]
        statuses = {
            (fingerprint, rule_id): status
            for fingerprint, rule_id, status in governance.statuses
        }
        severity_rank = {"low": 0, "medium": 1, "high": 2}
        for entry in result.delta.introduced:
            finding = entry.after
            status = statuses.get((finding.fingerprint, finding.rule_id), "unregistered")
            if (
                status != "accepted"
                and severity_rank.get(finding.severity, 0) >= threshold
            ):
                return 1
    return 0


def _debt(format_name: str) -> int:
    try:
        root = repository_root(Path.cwd())
        collected = collect_working_tree(root)
        analyzed = _mark_no_supported_files(parse_snapshot(collected))
        findings = detect_findings(analyzed.facts)
        governance = load_and_associate(root, findings)
    except RepositoryError as error:
        return _emit_error(format_name, "REPOSITORY_ERROR", str(error))
    except GovernanceError as error:
        return _emit_error(format_name, "GOVERNANCE_ERROR", str(error))

    report = DebtReport(
        schema_version="1",
        tool_version=__version__,
        ruleset_version=RULESET_VERSION,
        snapshot=collected.snapshot,
        coverage=analyzed.coverage,
        findings=findings,
        diagnostics=analyzed.coverage.diagnostics,
        governance=governance,
        analysis_mode=ANALYSIS_MODE,
        enabled_rules=ENABLED_RULES,
        complete=analyzed.coverage.complete,
        candidate_closure=analyzed.coverage.complete,
    )
    sys.stdout.write(render_debt_report(report, format_name))
    return 0 if report.complete else 2


def _window_digest_argument(value: str) -> str:
    import re
    if not re.match(r"^[0-9a-f]{64}$", value or ""):
        sys.stderr.write(
            "spellguard: error: --registry-sha256 must be 64 lowercase "
            "hexadecimal characters\n")
        raise SystemExit(2)
    return value


def _check(format_name: str, digest: str) -> int:
    try:
        root = repository_root(Path.cwd())
        report, code = review_windows(root, digest)
    except RepositoryError as error:
        return _emit_error(format_name, "REPOSITORY_ERROR", str(error))
    except RegistryError as error:
        return _emit_error(format_name, error.code, error.message)
    if format_name == "text" and code in (0, 1, 2):
        output = render_window_report(report, format_name)
        if code == 0:
            sys.stdout.write(output)
        else:
            sys.stderr.write(output)
    else:
        output = render_window_report(report, "json")
        sys.stdout.write(output)
    return code


def _context(format_name: str, digest: str) -> int:
    try:
        root = repository_root(Path.cwd())
        report, code = window_context(root, digest)
    except RegistryError as error:
        return _emit_error(format_name, error.code, error.message)
    except RepositoryError as error:
        return _emit_error(format_name, "REPOSITORY_ERROR", str(error))
    sys.stdout.write(render_window_report(report, format_name))
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _scan(args.format)
    if args.command == "review":
        return _review(args.format, args.base, args.fail_on, args.all)
    if args.command == "debt":
        return _debt(args.format)
    if args.command == "check":
        return _check(args.format, _window_digest_argument(args.registry_sha256))
    if args.command == "context":
        return _context(args.format, _window_digest_argument(args.registry_sha256))
    if args.command == "hook":
        if args.host == "codex":
            from .hook_core import main as hook_main
        elif args.host == "claude":
            from .claude_hook import main as hook_main
        else:
            from .cursor_hook import main as hook_main
        return hook_main(["--repo", args.repo, "--state-dir", args.state_dir,
                          "--registry-sha256", args.registry_sha256])
    if args.command == "demo":
        from .demo import main as demo_main
        return demo_main()
    if args.command == "assist":
        return _assist(args)
    if args.command == "propose":
        return _propose(args)
    if args.command == "confirm":
        return _confirm(args)
    if args.command == "instructions":
        return _instructions(args.format if hasattr(args, "format") else "text")
    parser.print_usage(sys.stderr)
    return 2


def _propose(args) -> int:
    from .marking import propose_temporary
    from .registry import RegistryError
    from .repository import RepositoryError
    try:
        report = propose_temporary(
            Path.cwd(), path=args.path, symbol=args.symbol,
            source_root=args.source_root, reason=args.reason,
            desired_state=args.desired_state)
    except RegistryError as error:
        return _emit_error(args.format, error.code, error.message)
    except RepositoryError as error:
        return _emit_error(args.format, "REPOSITORY_ERROR", str(error))
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(report["summary"] + "\n")
        sys.stdout.write("digest: {} (confirmed: no)\n".format(
            report["proposal_digest"]))
        sys.stdout.write("next: spellguard confirm --proposal-sha256 {}\n".format(
            report["proposal_digest"]))
    return 0


def _confirm(args) -> int:
    from .marking import confirm_proposal
    from .registry import RegistryError
    from .repository import RepositoryError
    try:
        report = confirm_proposal(Path.cwd(), args.proposal_sha256)
    except RegistryError as error:
        return _emit_error(args.format, error.code, error.message)
    except RepositoryError as error:
        return _emit_error(args.format, "REPOSITORY_ERROR", str(error))
    if args.format == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write("TEMP-001 confirmed with digest {}\n".format(
            report["registry_digest"]))
        if not report["proposal_cleaned"]:
            sys.stdout.write(
                "note: formal registry is confirmed; proposal cleanup is pending\n")
        sys.stdout.write("next:\n  {}\n  {}\n".format(
            report["next"][0], report["next"][1]))
    return 0


def _instructions(format_name: str) -> int:
    from .marking import agent_instructions
    if format_name == "json":
        sys.stdout.write(json.dumps(
            {"command": "instructions", "flag": False,
             "instructions": agent_instructions()}, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(agent_instructions())
    return 0


def _assist(args) -> int:
    from .assisted_review import prepare_assisted_review, validate_assisted_review
    root = Path.cwd()
    format_name = args.format if hasattr(args, "format") else "json"
    if getattr(args, "assist_command", None) == "prepare":
        packet, code = prepare_assisted_review(root, args.registry_sha256)
        if format_name == "json":
            sys.stdout.write(json.dumps(
                packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return code
    if getattr(args, "assist_command", None) == "validate":
        report, code = validate_assisted_review(
            root, args.packet, args.response, args.registry_sha256)
        sys.stdout.write(json.dumps(
            report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return code
    return 2
