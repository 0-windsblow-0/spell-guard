# Spellguard

> The grimoire remembers. The guard checks.

**Keep temporary workarounds from becoming permanent architecture.**

Alpha · Local-first · MIT

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

Requires Python 3.10+ and Git. From the repository root, with [uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```bash
uv tool install .
spellguard demo
```

The demo shows the current loop:

```text
OPEN → new external caller → VIOLATED → remove caller → OPEN
```

The synthetic demo runs locally and does not upload source code or call an LLM.

## How it works

### Marking a temporary compromise in an AI session

Working with an AI coding agent? When the agent deliberately introduces a
temporary compatibility compromise, it can draft a proposal instead of
waiting for you to hand-write a rule:

Run `spellguard instructions` once to print the host-neutral instruction block
you can add to your Agent's repository instructions. Spellguard does not edit
those files for you.

```bash
spellguard propose --path src/demo/adapt.py --symbol adapt --source-root src \
  --reason "temporary while migrating" --desired-state "remove after migration"
```

The proposal lives in Git metadata only and does not affect `check`. Only
after you explicitly confirm does `spellguard confirm` promote it to the
official registry:

```bash
spellguard confirm --proposal-sha256 <digest shown to you>
```

The agent asks; you confirm. Nothing is auto-confirmed.

## Existing checks

1. Confirm a temporary function and its `no_external_callers` constraint.
2. Run `spellguard check` as the code changes.
3. Review concrete caller evidence and decide whether to keep, accept, or remove the dependency.

Install once. Spellguard stays quiet until a confirmed temporary decision starts gaining new dependencies.

## Agent integration

Spellguard can run through Agent hooks and adapters so checks happen automatically while you keep using your normal coding workflow. In normal operation it stays quiet and surfaces only relevant changes. See [Agent integration](docs/USAGE.md#agent-integration).

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

To experience the current product, these are the only interfaces you need.

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

If you configured an Agent hook manually, remove its Spellguard entry before uninstalling.

## Feedback

Did a reminder change an implementation decision—or was it not worth the interruption? Open an issue with a minimal synthetic example, command output, and expected result. Please do not include private source code or credentials.

## License

[MIT License](LICENSE)
