# Spellguard

> The grimoire remembers. The guard checks.

**别让临时补丁，变成永久架构。**

0.2.0a3 Alpha · Local-first · MIT

[English](README.md) · 简体中文 · [日本語](README.ja.md)

Spellguard 是一个面向 Coding Agent 的本地守卫：把一个实现标记为 temporary，当后续代码开始依赖它时，Spellguard 会提醒你。

```text
legacy.adapt() 是迁移期间保留的临时兼容函数。

几周后：

feature.py → legacy.adapt()

Spellguard:
⚠ TEMP-017 gained a new external caller.
```

原本只想保留一阵子的实现，正在获得新的依赖。Spellguard 会在它变得更难移除之前提醒你。

**当前 Alpha：** 主要检查直接静态引用，并支持 `no_external_callers` 这一类 Repair Window 约束。

## 为什么需要 Spellguard

Coding Agent 可以快速推进代码，但临时兼容路径也可能悄悄变成架构的一部分。Spellguard 记住这项临时决定，并检查后续修改是否增加了新的依赖。

*Every shortcut leaves a trace. Every curse has an exit.*

## Intent → Evidence → Decision

- **Intent：** 你确认什么只是 temporary。
- **Evidence：** Spellguard 找出新的、支持范围内的依赖。
- **Decision：** 是否继续保持隔离，由你或 Agent 决定。

## Quick start

需要 macOS/Linux、Python 3.10+ 和 Git。安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后可直接运行：

```bash
uv tool install "git+https://github.com/0-windsblow-0/spell-guard.git"
spellguard demo
```

Demo 展示当前完整闭环：

```text
OPEN → 新外部调用 → VIOLATED → 移除调用 → OPEN
```

这是一个在本地运行的合成 demo，不上传源码，也不调用 LLM。

## 让 Agent 完成接入

安装后，在 Codex 中打开项目主 checkout，告诉它：

> 请在这个仓库接入 Spellguard。先读 `spellguard instructions`，预览变更并告诉我将添加哪些 Hook。我批准方案后再应用。每条临时约定都必须先问我，不能自行确认。

路径、安装 ID 和 digest 由 Agent 处理。你审查接入方案，并完成 Codex 原生 Hook 信任步骤；仅安装 CLI 不会激活检查。受管接入目前是 **Codex 专属的实验流程**，完整实机验收尚未完成，依赖提醒前请验证真实事件。不需要常驻服务，也不额外调用 LLM。

## 能帮你做什么

- **预览和移除接入：** 只管理 Spellguard 自己的 Hook，保留其他 Hook。
- **确认一次意图：** Agent 展示临时函数、保留理由和退出条件；你批准后，它完成登记、已采纳摘要更新和一次检查。
- **保留少量约束：** 最多八条登记，包含已结束的约定，统一使用 `no_external_callers`。
- **提供可追查证据：** 给出调用文件、行号和符号；分析不完整时明确提示，相同提醒去重。
- **恢复中断的确认：** 重放已授权事务，不静默接受登记漂移。

提醒指出的是值得审查的依赖，不是已确认业务缺陷。已有调用者（包括测试）同样违反 `no_external_callers`；它不是“只管新增生产调用”的策略。`OPEN` 表示未发现支持范围内的外部调用，`ACTIVE` 表示临时约定仍然有效。

## Agent integration

受管接入当前面向 **macOS/Linux 上 Codex 的主 checkout**，不支持 linked worktree。Claude Code 和 Cursor 保留手工适配器，目前只有协议测试证据。新版受管流程不沿用旧适配器的实机通过结论。

接入、确认、原生信任、恢复与移除步骤见[使用说明](docs/USAGE.md#agent-integration)。高级用户仍可直接使用固定摘要的 `check` 和 `context`。

## 支持的语言

| 语言 | Repair Window |
| --- | --- |
| Python | ✓ |
| Go | ✓ |
| JavaScript / TypeScript（ESM 子集） | ✓ |
| Java（顶层类型中的 static 方法） | ✓ |
| C（free function + 匹配的 header prototype） | ✓ |
| C++（free 或 namespace 内 free function） | ✓ |

当前 Alpha 聚焦直接静态引用。准确语法范围见[使用说明](docs/USAGE.md)。

## 核心命令

| 接口 | 用途 |
| --- | --- |
| `spellguard check` | 检查已确认的临时约束 |
| `spellguard context` | 向 Agent 提供已确认的意图 |
| Agent hook / adapter | 在现有 Agent 工作流中自动检查变化 |

Agent 还会使用 `setup`、`status`、`propose`、`confirm` 和 `recover`；你无需记住参数。

## 实验性分析工具

Spellguard 还保留了较早的 `scan`、`review` 和 `debt` 结构分析命令。使用 Repair Window 工作流并不需要它们。

## Spellguard 与 Agent 指令文件

**AGENTS.md tells agents what to remember. Spellguard checks whether the code is still honoring it.**

AGENTS.md / CLAUDE.md 提供静态指令；Spellguard 检查当前改动是否正在违背已确认的临时约束，并给出具体调用证据。

## Limitations

Spellguard 当前处于 Alpha，默认只提供 advisory 提醒。

- 主要分析支持范围内的直接静态引用。
- 不自动判断临时依赖在业务上是否正确。
- 不建立完整的运行时调用图。
- 应作为额外守卫，而不是唯一的 merge gate。

更准确的边界见[使用说明](docs/USAGE.md)。

## 停止与卸载

```bash
uv tool uninstall spellguard
# 或在对应虚拟环境中运行：
python -m pip uninstall spellguard
```

先让 Agent 预览 `spellguard setup --remove`，批准后应用移除，再卸载 CLI。手工适配器只移除其中的 Spellguard 条目。已确认规则和本地记录会保留。

## 反馈

提醒是否改变了一次实现决定，或者根本不值得这次打断？欢迎通过 issue 提供最小合成示例、命令输出和预期结果。请勿包含私有源码或凭据。

## License

[MIT 许可证](LICENSE)
