# Usage

[English](../README.md) · [简体中文](../README.zh-CN.md) · [日本語](../README.ja.md)

## Install with Python and pip

Requires Python 3.10+ and Git on macOS or Linux. From the downloaded source root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
spellguard demo
```

The README offers a direct GitHub install with uv. Both that route and `uv tool install .` install the CLI in its own environment; neither makes `spellguard` importable from an unrelated system Python. For the Python snippets and manual adapters below, use the Python interpreter in the environment where Spellguard was installed. If using uv tools, locate it under `uv tool dir` (the `spellguard/bin/python` interpreter on macOS/Linux). Do not guess a system Python path.

## Agent-managed Codex setup (experimental)

This Alpha adds a managed workflow for Codex in a main checkout on macOS/Linux. Its full live-host acceptance is pending. Configuration success and protocol tests do not prove that your host executed the hooks. Linked worktrees and nested repositories are rejected; there is no daemon, model call, or merge gate.

Ask your Agent to read `spellguard instructions`, then preview setup from the target Git repository:

```bash
spellguard setup --host codex --format json
```

The preview is read-only. Review the added `UserPromptSubmit` and `Stop` commands, executable path, and any existing registry that the plan will adopt. Approve only the contents you intend to trust. The Agent then runs:

```text
spellguard setup --apply <reviewed-plan-digest> --format json
spellguard status --format json
```

Setup changes only its entries in `.codex/hooks.json` and writes local control records under `~/.spellguard/installations/`. Other hooks are preserved. Existing legacy Spellguard hooks or conflicting configuration require review rather than automatic migration. Installation uses an absolute executable path; keep that environment available until you remove or reconfigure the hook. Keep local hook paths out of public commits.

Complete Codex's native project and Hook trust review separately (`/hooks` in the CLI). Never substitute a bypass flag or a manually edited trust hash. Then verify actual `UserPromptSubmit` and `Stop` events. `status` reports configuration and registry binding; this version does not automatically certify host activation (`host_verified` remains false and `last_check` is not populated). See the [official Codex Hooks documentation](https://developers.openai.com/zh-Hans/docs/hooks).

### Confirm a decision, not a digest-maintenance chore

The Agent gets `installation_id` from `status`, drafts a proposal, and shows the target, reason, and desired end state:

```text
spellguard propose --installation-id <id> --path src/demo/adapt.py --symbol adapt --source-root src --reason "temporary while migrating" --desired-state "remove after migration" --format json
```

Only after the maintainer explicitly approves the shown proposal may the Agent run:

```text
spellguard confirm --installation-id <id> --proposal-sha256 <reviewed-proposal-digest> --format json
```

Confirmation records the rule, updates the external adopted digest, and runs one real check. The hook command stays unchanged: there is no manual digest rebinding after each confirmation. A successful confirmation command means the decision was saved; inspect `check_exit_code`, `check_complete`, and `diagnostics` for the separate analysis result. A new function absent from HEAD can remain unverified after successful registration. Do not commit code just to hide that uncertainty.

To close a rule, the Agent uses `propose --installation-id <id> --resolve TEMP-001 --reason "migration complete"` and waits for the same explicit approval before `confirm`. Removing callers returns a check to OPEN; it does not end the ACTIVE decision. The eight-registration limit includes RESOLVED entries.

For interrupted confirmation, `spellguard recover --format json` replays only an existing authorized pending transaction. Interrupted setup/removal is resumed by repeating its reviewed `setup --apply` digest. Registry drift is not silently accepted. The Agent must not confirm on its own, regenerate trust to silence an error, or treat unknown as safe.

For removal, preview `spellguard setup --remove --format json`, approve it, then apply its plan digest with `setup --apply`. Only owned hooks are removed; confirmed rules and local records remain. Verify a new host turn no longer invokes Spellguard before uninstalling the CLI.

## Start with the demo

`spellguard demo` is an installed package command — no source checkout needed after install:

The demo creates a disposable Git repository and a synthetic rule. It runs the installed CLI and local adapter through isolated function → external caller → deduplication → recovery. It fails with a nonzero exit status if the expected behavior is not observed. Temporary files are cleaned up automatically, and no Agent hook is installed.

## A rule means a human decision

A rule records a maintainer's intent, not a conclusion inferred from a branch or a function name. Identify the function, explain why it is temporary, and state the condition for removing it. Confirm that it should remain isolated from other files.

The following is a **synthetic example**, not a rule to apply to an arbitrary project. A real rule belongs in `.spellguard/rules.json` inside your target Git repository:

```json
{
  "schema_version": 1,
  "rules": [{
    "id": "TEMP-001",
    "classification": "temporary",
    "lifecycle": "ACTIVE",
    "reason": "Synthetic example: keep the legacy adapter isolated during migration.",
    "desired_state": "Synthetic example: remove the adapter after migration.",
    "protected_symbol": {
      "path": "src/demo/legacy.py",
      "symbol": "adapt",
      "source_root": "src"
    },
    "window": "no_external_callers",
    "resolution_reason": null
  }]
}
```

Up to eight registrations are supported, including RESOLVED entries. For Python, `source_root` is `.` or a single repository-relative directory such as `src` or `tools`. A flat module such as `tools/legacy.py` can use `source_root="tools"`; nested package directories require the corresponding `__init__.py` files. With `.`, the protected file must be directly at the repository root. It must be a top-level named function, not a method or a re-export. Unknown registry fields, duplicate keys, invalid paths, and unsupported identities are rejected. This is static module identity, not execution of arbitrary import-path setup.

## Advanced: fixed-digest checks

The following is the manual route. Managed Codex confirmation above updates its own adopted digest after explicit approval.

From the target repository, with Spellguard's virtual environment active, compute the digest **once after confirming the rule contents**:

```bash
python - <<'PY'
from pathlib import Path
from spellguard.registry import parse_registry
print(parse_registry(Path('.spellguard/rules.json').read_bytes()).digest)
PY
```

Save that digest as the fixed value for this rule. The angle-bracket value below must be replaced with the confirmed digest:

```text
spellguard context --registry-sha256 <confirmed-digest> --format json
spellguard check --registry-sha256 <confirmed-digest> --format json
```

Do not recompute and adopt the digest before every check: that would silently accept changes to the rule itself. If you intentionally change the rule, review and confirm its contents again, then update the fixed digest.

`context` reads intent; it does not analyze source code. `check` evaluates the current working tree and uses HEAD to compare caller identities. A committed violation remains a violation even when the diff is empty. Removing callers restores OPEN but does not automatically resolve the rule.

| Check result | Meaning | Exit |
| --- | --- | --- |
| Complete, no violation | No supported external caller found | 0 |
| Complete, violated | At least one supported external caller found | 1 |
| Incomplete | Registry, source, identity, or baseline could not be fully checked | 2 |

Known callers are preserved alongside unknowns. An unavailable baseline makes the added-caller comparison unknown; it does not erase current evidence. Renames are different static identities, not proof of new business behavior. This is evidence for review, not a judgment that a caller is a defect.

### Supported direct-reference shapes

| Language | Definite external call |
| --- | --- |
| Python | Direct calls through an unambiguous absolute/relative import or module alias |
| Go | Direct same-package calls or selectors from an explicit module import |
| JavaScript / TypeScript | Direct calls through a relative ESM named, default, or namespace import |
| Java | A static method in a top-level type called through an explicit type import, static import, or fully qualified name |
| C | A direct free-function call after a literal include of the matching project header |
| C++ | A direct free-function call, or a namespace-qualified call, after a literal include of the matching project header |

These are intentionally narrow static identities. Related ambiguous imports, function-value passing, dynamic dispatch, reflection, macros, and runtime-only consumers are not treated as definite callers; depending on the language and syntax, Spellguard either reports an incomplete result or leaves the reference outside its declared coverage.

### Manual proposal and confirmation (without managed setup)

When an AI assistant deliberately introduces a temporary compatibility
compromise, it may draft a proposal instead of asking you to hand-write the
rule:

Print the host-neutral instruction block once and add it to the repository
instructions used by your Agent:

```bash
spellguard instructions
```

`instructions` only prints text; it does not edit Agent instruction files or host configuration. The separate managed `setup --apply` command does write its reviewed hook changes.

```bash
spellguard propose --path src/demo/adapt.py --symbol adapt \
  --source-root src --reason "temporary while migrating" \
  --desired-state "remove after migration"
```

A proposal is stored only in Git metadata, so `check` and `context` still
ignore it. The command prints a human-readable summary and the digest you
must see. Only after you explicitly approve does the assistant run:

```bash
spellguard confirm --proposal-sha256 <the digest you saw>
```

`confirm` fails (exit 2) if the digest is wrong, the draft changed, or a
registry already exists. To confirm is a human decision; the assistant must
never self-confirm. If you use a persistent Agent hook, enable it with the
new digest printed by `confirm`.

## Agent integration

Use the managed Codex workflow above for the new experimental setup path. The source distribution also includes three manual adapters. Keep the scripts together: Claude Code and Cursor reuse the core in the Codex script.

| Host | Script | Input events | Verification |
| --- | --- | --- | --- |
| Codex | `scripts/codex_repair_window.py` | `UserPromptSubmit`, `Stop` | Live evidence as described below |
| Claude Code | `scripts/claude_repair_window.py` | `UserPromptSubmit`, `Stop` | Protocol tests only |
| Cursor | `scripts/cursor_repair_window.py` | `beforeSubmitPrompt`, `stop` | Protocol tests only |

For the selected adapter, inspect its arguments; for example:

```bash
python scripts/codex_repair_window.py --help
```

The adapter takes fixed absolute `--repo`, `--state-dir`, and `--registry-sha256` arguments. The state directory must be outside the repository and use a real path without symbolic links. Configure the host to pass the corresponding events from the table to its script. Use the absolute path to the Python interpreter where Spellguard is installed. Setup is manual; the demo does not configure your host.

Project hook loading requires host trust. Verify actual events in your host before relying on reminders. Older fixed-digest adapter evidence covers Codex CLI 0.153.4 in a primary checkout on macOS; it does not establish live-host acceptance for the new managed workflow. Linked worktrees are unsupported; interrupted turns, crashes, and sub-agents are not covered by normal turn-end triggering. See the [Codex hook documentation](https://developers.openai.com/zh-Hans/docs/hooks) for host configuration.

Normal Stop checks are quiet. Changed violations or failures surface a message, and unchanged evidence is deduplicated. Prompt context preserves the distinction between the last check and a fresh result. The Codex and Claude adapters do not request automatic continuation. The current Cursor adapter maps Stop notices to `followup_message`, which can request another Agent turn; it is experimental and should not be treated as a verified quiet notification. None of these adapters edits your source or rules.

Local state/logs contain check and notification metadata, not prompts or transcripts. If storage fails, the adapter reports the failure and cannot guarantee deduplication. A log entry records the output prepared by the adapter; it is not confirmation that a human saw the message.

## Stop and uninstall

The demo ends without leaving a hook installed. For managed setup, preview `spellguard setup --remove` and apply the approved plan before uninstalling. If you configured hooks manually, remove only the Spellguard entries from the two events. Verify that a new turn no longer invokes the adapter. Keep your other hooks intact.

For a uv tool installation, run `uv tool uninstall spellguard`. For pip, from the active virtual environment:

```bash
python -m pip uninstall spellguard
```

Uninstallation does not remove manually added host configuration, rule files, or logs. You may delete the dedicated state directory after disabling the hook if you no longer need its records.

## Older structural commands

With the installation environment active, these commands can run in a Git repository:

```bash
spellguard review --base HEAD
spellguard review --base HEAD --all
spellguard scan
spellguard debt --format json
```

They provide experimental Python/Go structural candidates and their existing records. They do not automatically create or confirm TEMP rules. A candidate is not a confirmed defect, and these commands are separate from the Go Repair Window.

## Contributing

Tests use synthetic source and temporary Git repositories. After installing from this source tree, run:

```bash
python - <<'PY'
import unittest
suite = unittest.defaultTestLoader.discover('tests', pattern='test_*.py')
assert suite.countTestCases() > 0, 'no tests discovered'
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
```

Reinstall with `python -m pip install .` after source changes, or use `python -m pip install -e .` while contributing. Run `python scripts/check_docs.py` for documentation links and `spellguard demo` for the demo.

Please submit minimal synthetic reproductions rather than private application source. Include versions, expected/actual output, and whether the reminder affected your decision. Reports of confusing or unnecessary reminders are useful too.

[MIT License](../LICENSE)
