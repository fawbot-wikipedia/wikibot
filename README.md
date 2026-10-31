# wikibot

日本語版ウィキペディアの保守作業を自動化するツール集です。

## ツール一覧

| ディレクトリ | 内容 | 詳細 |
|---|---|---|
| [ill/resolved_redlinks/](ill/resolved_redlinks/) | Cewbotが仮リンクを内部リンクに置き換えたリンクのうち、リンク先が赤リンク・壊れた転送になり、最新版にも残っているものをJSONで一覧にする | [README](ill/resolved_redlinks/README.md) |

ツールは分野ごとのディレクトリ（`ill/` は仮リンク関連）の下に、1つのパッケージとして置きます。各ツールは `python -m <パッケージ>` で実行します。

## セットアップ

Python 3.15 を使います。パッケージ・仮想環境・Python 本体は [uv](https://docs.astral.sh/uv/) で管理します。uv が古いと Python 3.15 の正式版を取得できないため、先に `uv self update` で更新してください。共通処理の [wikiutil](https://github.com/fawbot-wikipedia/wikiutil) は `uv sync` で GitHub から取得するため、ネットワークに接続できる必要があります。

```powershell
uv sync
```

## テスト

テストは標準ライブラリの `unittest` を使います。リポジトリ直下で実行すると、すべてのツールのテストを実行します。

```powershell
uv run python -m unittest
```
