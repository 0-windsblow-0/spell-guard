# Spellguard

> The grimoire remembers. The guard checks.

**别让临时补丁，变成永久架构。**

Alpha · Local-first · MIT

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

需要 Python 3.10+ 和 Git。在仓库根目录安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后运行：

```bash
uv tool install .
spellguard demo
```

Demo 展示当前完整闭环：

```text
OPEN → 新外部调用 → VIOLATED → 移除调用 → OPEN
```

这是一个在本地运行的合成 demo，不上传源码，也不调用 LLM。

## 工作方式

### 在 AI 会话中标记临时妥协

和 AI 编码 Agent 协作时，Agent 若明确引入临时兼容方案，可通过 `spellguard propose` 先草拟一份提案，而不是等你手写规则：

先运行一次 `spellguard instructions`，即可得到可加入 Agent 仓库指令的宿主无关说明。Spellguard 不会替你修改这些指令文件。

```bash
spellguard propose --path src/demo/adapt.py --symbol adapt --source-root src \
  --reason "迁移期间临时保留" --desired-state "迁移后移除"
```

提案只存在 Git metadata 中，`check` 不会采纳。`propose` 会打印摘要与你必须看到的摘要 digest。只有你明确确认后，Agent 才执行：

```bash
spellguard confirm --proposal-sha256 <你看到的 digest>
```

摘要不匹配、草案被改或已有正式 registry 时，`confirm` 退出 2 且不改任何状态。确认是人类决定，Agent 绝不能自我确认。若已安装持久 hook，完成后需用新 digest 重新配置。


1. 确认一个临时函数及其 `no_external_callers` 约束。
2. 在代码变化时运行 `spellguard check`。
3. 查看具体调用证据，决定保持隔离、接受依赖，还是移除依赖。

安装一次，平时保持安静；只有已确认的临时实现开始获得新的依赖时才提醒。

## Agent integration

接入 Agent hook / adapter 后，Spellguard 可以在原有开发流程里自动检查；正常情况下保持安静，只呈现有意义的变化。详见 [Agent 接入说明](docs/USAGE.md#agent-integration)。

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

如果只想体验当前产品，只需要关注这些接口。

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

如果手工配置过 Agent hook，请先移除其中的 Spellguard 条目，再执行卸载。

## 反馈

提醒是否改变了一次实现决定，或者根本不值得这次打断？欢迎通过 issue 提供最小合成示例、命令输出和预期结果。请勿包含私有源码或凭据。

## License

[MIT 许可证](LICENSE)
