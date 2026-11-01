# Cewbotが解消した仮リンクのうち赤リンクに戻ったものの抽出

[特別:投稿記録/Cewbot](https://ja.wikipedia.org/wiki/特別:投稿記録/Cewbot) のうち、要約が「解消済み仮リンク」で始まる編集（仮リンクを内部リンクに置き換えた編集）を調べます。置き換え先が現在赤リンク、または壊れた転送（転送先が存在しない転送ページ）になっていて、ページの最新版にもその内部リンクが残っているもの（仮リンクに戻されていないもの）を、JSON形式で出力します。出力した赤リンクを置換前の仮リンクに戻すコマンドもあります（既定はドライランで、`--execute` を付けたときだけ記事を編集します）。

仕様の詳細は [design.md](design.md) を参照してください。

## 使い方

コマンドはリポジトリ直下で実行します。

```powershell
uv run python -m ill.resolved_redlinks
uv run python -m ill.resolved_redlinks --since 2022-08-13 --until 2022-08-13
uv run python -m ill.resolved_redlinks --namespace 0
```

| オプション | 内容 |
|---|---|
| `--namespace N` | 対象の名前空間（複数指定可、既定: 0 と 10。記事とTemplate） |
| `--since YYYY-MM-DD` | この日（日本時間）以降のCewbotの編集だけを対象にする |
| `--until YYYY-MM-DD` | この日（日本時間）以前のCewbotの編集だけを対象にする |
| `--refresh` | 投稿記録のキャッシュを使わず最初から取得する |
| `--dump-date YYYYMMDD` | リンク先の一次判定に使うダンプの日付（既定: 最新） |
| `--output-dir PATH` | 出力先の親ディレクトリ（既定: `ill/resolved_redlinks/output`） |
| `--cache-file PATH` | キャッシュのファイル（既定: `ill/resolved_redlinks/cache/cache.sqlite3`） |

仮リンクに戻す本文を、編集せずに作って確かめます（ドライラン）。記事の赤リンクだけを対象にします。`--execute` を付けると、実際に編集します。

```powershell
uv run python -m ill.resolved_redlinks.restore
uv run python -m ill.resolved_redlinks.restore --max-pages 10
uv run python -m ill.resolved_redlinks.restore --execute --max-edits 10
```

編集する場合は、リポジトリ直下の [`.env.example`](../../.env.example) を `.env` にコピーし、ボットの BotPassword のログイン情報に書き換えます。`.env` は git の管理から外してあります。

```powershell
Copy-Item .env.example .env
```

```dotenv
WIKIPEDIA_USERNAME=利用者名@BotPassword名
WIKIPEDIA_BOT_PASSWORD=BotPassword
```

| オプション | 内容 |
|---|---|
| `--report PATH` | 調査のJSON（既定: `ill/resolved_redlinks/output` の中でいちばん新しいもの） |
| `--max-pages N` | 扱うページの数の上限（ページ名順の先頭から） |
| `--execute` | 実際に編集する。ボットの承認前は、1回の実行で10ページまでにする |
| `--max-edits N` | 1回の実行で編集するページの数の上限（`--execute` のとき） |

## 出力

実行ごとに `ill/resolved_redlinks/output/YYYYMMDD_HHMMSS/resolved_ill_redlinks.json` を作ります（日時は日本時間で、処理を終えた時刻）。

- `items` にリンク1件ごとのレコードを格納します。分類、名前空間、ページ名とURL、リンク先とURL、置き換えた版のID・固定リンク・差分リンク・日時、日本時間の日付、置換前の仮リンクと表示文言、その版で置き換えた同じリンク先の仮リンクの一覧、削除・移動の記録を含みます。
- `items` の `replaced_ills` は、仮リンクに戻すコマンドが使います。古い版（`schema_version` が `1`）のJSONは使えないので、調査を実行し直してください。
- `counts` に分類ごとの件数と、最新版で仮リンクに戻されたもの・リンクが消えたもの・ページがなくなったものの件数を格納します。
- `filters` に対象名前空間とCewbotの編集期間、`dump_date` に一次判定に使ったダンプの日付、`generated_at` に取得日時（ISO 8601）を格納します。
- JSONはUTF-8で、日本語をエスケープせずに出力します。構造の詳細は [design.md](design.md) を参照してください。
- 標準出力には件数の概要と出力先を表示します。

## 注意

- 初回は Cewbot の投稿記録（約90万件）をAPIで全件取得するため、時間がかかります（2026年10月の実測で約3時間。混雑の応答（HTTP 429）で待たされるため）。途中で止まっても、次回は止まった位置から再開します。2回目からは新しい編集だけを取得します。`--since`・`--until`・`--namespace` を付けても、投稿記録は全件をキャッシュします。
- 全期間を対象にすると、投稿記録の取得後も2〜3時間かかります（2026年10月の実測。最新版の取得に約50分、削除・移動の記録の確認に約1時間45分。どちらも混雑の応答（HTTP 429）で待たされるため）。削除・移動の記録はキャッシュするので、2回目からは短くなります。結果は赤リンク約2,000件でした。
- JSONに置換前の仮リンクを含めるため、初回は出力対象の置換版ごとに差分を追加取得します。同じ版の複数リンクは1回の取得で処理します。差分はキャッシュされ、2回目以降は未取得の版だけを取得します。取得できない場合は `original_ill_error` に理由を記録します。
- リンク先の存在の一次判定に、ダンプの `page.sql.gz`（約170 MB）と `redirect.sql.gz`（約14 MB）を `ill/resolved_redlinks/input/YYYYMMDD/` に取得します。取得済みなら再取得しません。
- ダンプ作成後の削除・移動は記録から補い、最後は必ずAPIで現在の状態を確かめます。
- 進み具合は時刻付きで標準エラー出力に表示します。
- 仮リンクに戻すコマンドは、使った調査のJSONと同じディレクトリに `restore_YYYYMMDD_HHMMSS.json`（ページごとの結果と、見送った理由）と `restore_YYYYMMDD_HHMMSS.diff`（編集するページの差分）を作ります。`--execute` のときは `restore_execute_YYYYMMDD_HHMMSS.json`・`.diff` で、保存した版のIDも記録します。保存の前には、編集の間隔（60秒＋0〜100秒の揺らぎ）を空け、会話ページの新着と停止用のページ（`利用者:<ボット名>/停止`）を確かめます。停止用のページに何か書き込むと、次の保存の前に止まります。リンク先の状態・削除の記録・ページの最新版を必ずAPIで取り直すので、調査のJSONが古くても、今の状態で判定します。リンク先の削除の記録を1件ずつ取るため、全件では時間がかかります（2026年10月の実測で、記事1,584ページ・リンク先約1,900件で約25分）。理由の一覧は [design.md](design.md#仮リンクに戻す規則) を参照してください。

## テスト

```powershell
uv run python -m unittest discover -s ill/resolved_redlinks/tests -t .
```
