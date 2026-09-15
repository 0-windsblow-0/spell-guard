# Spellguard

> The grimoire remembers. The guard checks.

**一時的な回避策を、恒久的なアーキテクチャにしない。**

Alpha · Local-first · MIT

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

Python 3.10+ と Git が必要です。リポジトリのルートで、[uv](https://docs.astral.sh/uv/getting-started/installation/)をインストールしてから実行します。

```bash
uv tool install .
spellguard demo
```

Demo は現在の一連の動作を示します。

```text
OPEN → 新しい外部呼び出し → VIOLATED → 呼び出しを削除 → OPEN
```

この合成 demo はローカルで動作し、ソースコードをアップロードせず、LLM も呼び出しません。

## 仕組み

1. 一時的な関数と、その `no_external_callers` 制約を確認します。
2. コードの変更時に `spellguard check` を実行します。
3. 具体的な呼び出しの根拠を見て、独立性を保つか、依存を受け入れるか、削除するかを判断します。

一度インストールすれば、確認済みの一時的な実装に新しい依存が増えるまで、Spellguard は静かに待機します。

## Agent integration

Agent hook / adapter と連携すると、通常の開発フローを保ったまま Spellguard が自動で確認できます。通常は静かに動作し、意味のある変化だけを提示します。詳しくは [Agent 連携ガイド](docs/USAGE.md#agent-integration)を参照してください。

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

現在の製品を試すために必要なのは、これらのインターフェースだけです。

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

Agent hook を手動設定した場合は、アンインストール前に Spellguard の項目を削除してください。

## フィードバック

通知によって実装判断が変わりましたか。それとも割り込みに値しませんでしたか。最小限の合成例、コマンド出力、期待した結果を issue で共有してください。非公開のソースコードや認証情報は含めないでください。

## License

[MIT ライセンス](LICENSE)
