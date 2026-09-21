# Spellguard

> The grimoire remembers. The guard checks.

**一時的な回避策を、恒久的なアーキテクチャにしない。**

0.2.0a3 Alpha · Local-first · MIT

[English](README.md) · [简体中文](README.zh-CN.md) · 日本語

Spellguard は Coding Agent 向けのローカルガードです。実装を temporary として登録すると、後のコードがその実装に依存し始めたときに通知します。

```text
legacy.adapt() は移行中だけ残す一時的な互換関数です。

数週間後：

feature.py → legacy.adapt()

Spellguard:
⚠ TEMP-017 gained a new external caller.
```

短期間だけ残すはずの実装に、新しい依存が増えています。Spellguard は、削除が難しくなる前に注意を戻します。

**現在の Alpha：** 直接的な静的参照と、`no_external_callers` Repair Window 制約に焦点を当てています。

## Why Spellguard

Coding Agent は開発を速めますが、一時的な互換経路が気付かないうちにアーキテクチャの一部になることがあります。Spellguard はその一時的な判断を記憶し、後の変更で新しい依存が増えていないかを確認します。

*Every shortcut leaves a trace. Every curse has an exit.*

## Intent → Evidence → Decision

- **Intent：** 何が temporary なのかをあなたが確認します。
- **Evidence：** Spellguard が対応範囲内の新しい依存を見つけます。
- **Decision：** 独立した状態を保つかは、あなたまたは Agent が決めます。

## Quick start

macOS/Linux、Python 3.10+、Git が必要です。[uv](https://docs.astral.sh/uv/getting-started/installation/)をインストールしてから直接実行できます。

```bash
uv tool install "git+https://github.com/0-windsblow-0/spell-guard.git"
spellguard demo
```

Demo は現在の一連の動作を示します。

```text
OPEN → 新しい外部呼び出し → VIOLATED → 呼び出しを削除 → OPEN
```

この合成 demo はローカルで動作し、ソースコードをアップロードせず、LLM も呼び出しません。

## セットアップは Agent に任せる

インストール後、Codex でプロジェクトのメイン checkout を開き、次のように依頼します。

> このリポジトリに Spellguard を設定してください。まず `spellguard instructions` を読み、変更をプレビューして追加する Hook を示してください。私が承認した後に適用してください。一時的な制約は必ず私に確認し、自己承認しないでください。

パス、インストール ID、digest は Agent が扱います。あなたは設定案を確認し、Codex のネイティブな Hook 信頼操作を行います。CLI のインストールだけではチェックは有効になりません。この管理型セットアップは **Codex 専用の実験的なワークフロー**です。実ホストでの一連の検証はまだ完了していないため、通知を頼りにする前に実際のイベントを確認してください。常駐サービスや追加の LLM 呼び出しはありません。

## できること

- **設定のプレビューと解除：** 他の Hook を保ち、Spellguard 自身の Hook だけを管理します。
- **意図を一度確認：** Agent が一時的な関数、理由、終了条件を提示します。承認後に登録、採用済み digest の更新、チェックを実行します。
- **少数の制約を維持：** 終了済みを含む最大八件の登録で、`no_external_callers` のみを使用します。
- **追跡できる根拠：** 呼び出し元のファイル、行、シンボルを示します。不完全な解析は明示し、同じ通知は重複させません。
- **中断した確認の復旧：** 承認済みの処理を再開し、登録内容の意図しない変更を黙認しません。

通知は確認すべき依存を示すもので、業務上の不具合の断定ではありません。既存の呼び出し元（テストを含む）も `no_external_callers` に違反します。「新規の本番コードの呼び出しだけ」を対象とする制約ではありません。`OPEN` は対応範囲で外部呼び出しが見つからない状態、`ACTIVE` は一時的な制約が引き続き有効な状態です。

## Agent integration

管理型セットアップは現在、**macOS/Linux 上の Codex のメイン checkout** を対象とします。linked worktree は非対応です。Claude Code と Cursor には手動アダプターがあり、検証はプロトコルテストのみです。新しい管理型フローの実機検証を、旧アダプターの結果で代用することはできません。

設定、確認、ネイティブな信頼操作、復旧、解除は[利用ガイド](docs/USAGE.md#agent-integration)を参照してください。上級者は固定 digest を使う `check` と `context` も利用できます。

## 対応言語

| 言語 | Repair Window |
| --- | --- |
| Python | ✓ |
| Go | ✓ |
| JavaScript / TypeScript（ESM サブセット） | ✓ |
| Java（トップレベル型の static メソッド） | ✓ |
| C（一致するヘッダープロトタイプを持つ free function） | ✓ |
| C++（free または名前空間スコープの free function） | ✓ |

現在の Alpha は直接的な静的参照に焦点を当てています。正確な構文の対応範囲は[利用ガイド](docs/USAGE.md)にあります。

## コアコマンド

| インターフェース | 用途 |
| --- | --- |
| `spellguard check` | 確認済みの一時的な制約をチェック |
| `spellguard context` | 確認済みの意図を Agent に提供 |
| Agent hook / adapter | 既存の Agent ワークフロー内で変更を自動チェック |

Agent は `setup`、`status`、`propose`、`confirm`、`recover` も使います。引数を覚える必要はありません。

## 実験的な分析ツール

Spellguard には、以前からある `scan`、`review`、`debt` の構造解析コマンドも含まれます。Repair Window の利用には必要ありません。

## Spellguard と Agent の指示ファイル

**AGENTS.md tells agents what to remember. Spellguard checks whether the code is still honoring it.**

AGENTS.md / CLAUDE.md は静的な指示を提供します。Spellguard は現在の変更が確認済みの一時的な制約に反していないかを調べ、具体的な呼び出しの根拠を示します。

## Limitations

Spellguard は現在 Alpha であり、デフォルトでは advisory として通知します。

- 対応範囲内の直接的な静的参照に焦点を当てています。
- 一時的な依存が業務上正しいかどうかは自動判断しません。
- 完全な実行時コールグラフは構築しません。
- 唯一の merge gate ではなく、追加のガードとして利用してください。

より正確な境界は[利用ガイド](docs/USAGE.md)にあります。

## アンインストール

```bash
uv tool uninstall spellguard
# または、対象の仮想環境内で：
python -m pip uninstall spellguard
```

先に Agent に `spellguard setup --remove` のプレビューを依頼し、承認後に解除してから CLI をアンインストールしてください。手動アダプターでは Spellguard の項目だけを削除します。確認済みの規則とローカル記録は保持されます。

## フィードバック

通知によって実装判断が変わりましたか。それとも割り込みに値しませんでしたか。最小限の合成例、コマンド出力、期待した結果を issue で共有してください。非公開のソースコードや認証情報は含めないでください。

## License

[MIT ライセンス](LICENSE)
