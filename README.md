# Spellguard

> The grimoire remembers. The guard checks.

**Keep temporary workarounds from becoming permanent architecture.**

0.2.0a3 Alpha · Local-first · MIT

English · [简体中文](README.zh-CN.md) · [日本語](README.ja.md)

Spellguard is a local guard for Coding Agents: mark an implementation as temporary, and Spellguard alerts you when later code starts depending on it.

```text
legacy.adapt() is a temporary compatibility function kept during a migration.

Weeks later:

feature.py → legacy.adapt()

Spellguard:
⚠ TEMP-017 gained a new external caller.
```

An implementation meant to last a little while is gaining new dependencies. Spellguard brings it back to your attention before it becomes harder to remove.

**Current Alpha:** Spellguard focuses on direct static references and the `no_external_callers` Repair Window constraint.

## Why Spellguard

Coding Agents move quickly, but a temporary compatibility path can quietly become part of the architecture. Spellguard remembers the temporary decision and checks later changes for new dependencies.

*Every shortcut leaves a trace. Every curse has an exit.*

## Intent → Evidence → Decision

- **Intent:** you confirm what is temporary.
- **Evidence:** Spellguard finds new supported dependencies.
- **Decision:** you or the Agent decide whether to keep it isolated.

## Quick start

Requires Python 3.10+ and Git on macOS/Linux. With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```bash
uv tool install "git+https://github.com/0-windsblow-0/spell-guard.git"
spellguard demo
```

The demo shows the current loop:

```text
OPEN → new external caller → VIOLATED → remove caller → OPEN
```

The synthetic demo runs locally and does not upload source code or call an LLM.

## Let your Agent handle the setup

After installing, open your project's main checkout in Codex and ask:

> Set up Spellguard in this repository. Read `spellguard instructions`, preview the changes, and show me which hooks will be added. Apply the reviewed plan after my approval. Never confirm a temporary rule without asking me.

The Agent handles paths, installation IDs, and digests. You review the setup and complete Codex's native hook trust step; installing the CLI alone does not activate checks. The managed setup is an **experimental Codex-only workflow**. Its full live-host acceptance is still pending; verify real events before relying on reminders. There is no background daemon or extra LLM call.

## What you get

- **Preview and remove setup:** manage Spellguard's own hooks while preserving other hooks.
- **Confirm intent once:** the Agent proposes a temporary function, its reason, and its exit condition. After your approval, it records the decision, updates the adopted digest, and runs a check.
- **Keep a small set of constraints:** up to eight registrations, including resolved ones, all using `no_external_callers`.
- **See actionable evidence:** caller file, line, and symbol; incomplete analysis stays visible. Unchanged notices are deduplicated.
- **Recover interrupted confirmation:** replay the authorized transaction without silently accepting registry drift.

A reminder identifies a dependency to review, not a business defect. Existing callers—including tests—also violate `no_external_callers`; it is not a “new production callers only” policy. `OPEN` means no supported external caller was found, while `ACTIVE` means the temporary decision still applies.

## Agent integration

Managed setup currently targets **Codex in a main checkout on macOS/Linux**. Linked worktrees are unsupported. Claude Code and Cursor retain manual adapters with protocol-test evidence only. The managed workflow does not inherit the older adapter's live-host validation.

See the [usage guide](docs/USAGE.md#agent-integration) for setup, confirmation, native trust, recovery, and removal. Advanced users can keep using fixed-digest `check` and `context` directly.

## Supported languages

| Language | Repair Window |
| --- | --- |
| Python | ✓ |
| Go | ✓ |
| JavaScript / TypeScript (ESM subset) | ✓ |
| Java (static methods in a top-level type) | ✓ |
| C (free functions with a matched header prototype) | ✓ |
| C++ (free or namespace-scoped free functions) | ✓ |

Alpha currently focuses on direct static references. Exact syntax coverage is documented in the [usage guide](docs/USAGE.md).

## Core commands

| Interface | Purpose |
| --- | --- |
| `spellguard check` | Check a confirmed temporary constraint |
| `spellguard context` | Provide confirmed intent to an Agent |
| Agent hooks / adapters | Check changes inside the existing Agent workflow |

The Agent also uses `setup`, `status`, `propose`, `confirm`, and `recover`; you do not need to memorize their arguments.

## Experimental analysis tools

Spellguard also includes the earlier `scan`, `review`, and `debt` structure-analysis commands. They are not required to use the Repair Window workflow.

## Spellguard and Agent instructions

**AGENTS.md tells agents what to remember. Spellguard checks whether the code is still honoring it.**

AGENTS.md and CLAUDE.md provide static instructions. Spellguard checks whether current changes conflict with a confirmed temporary constraint and points to concrete caller evidence.

## Limitations

Spellguard is currently Alpha and advisory.

- It focuses on supported direct static references.
- It does not infer whether a temporary dependency is correct for the business.
- It does not build a complete runtime call graph.
- Use it as an additional guard, not as the only merge gate.

More precise boundaries are documented in the [usage guide](docs/USAGE.md).

## Uninstall

```bash
uv tool uninstall spellguard
# or, inside the relevant virtual environment:
python -m pip uninstall spellguard
```

First ask your Agent to preview `spellguard setup --remove` and apply the approved removal. Then uninstall the CLI. For manual adapters, remove only their Spellguard entries. Confirmed rules and local records are retained.

## Feedback

Did a reminder change an implementation decision—or was it not worth the interruption? Open an issue with a minimal synthetic example, command output, and expected result. Please do not include private source code or credentials.

## License

[MIT License](LICENSE)
