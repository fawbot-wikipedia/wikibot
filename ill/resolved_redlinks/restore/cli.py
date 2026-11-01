"""仮リンクに戻す処理のコマンドラインの入口。既定はドライラン。"""

import argparse
import json
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from wikiutil.files import atomic_write
from wikiutil.mediawiki import MediaWikiError

from ill.resolved_redlinks.paths import (
    EDIT_STATE_PATH,
    ENV_PATH,
    OUTPUT_DIRECTORY,
)
from ill.resolved_redlinks.pipeline import OUTPUT_FILE_NAME
from ill.resolved_redlinks.report import JST
from ill.resolved_redlinks.restore.editor import (
    UNAPPROVED_MAX_EDITS,
    Editor,
    load_credentials,
)
from ill.resolved_redlinks.restore.runner import (
    dry_run,
    execute,
    load_candidates,
    render_diff,
    render_results,
    summarize,
)
from ill.resolved_redlinks.sources import api as wiki_api


def latest_report(output_directory: Path) -> Path | None:
    """出力先の中で、いちばん新しい調査のJSONを返す。"""
    reports = sorted(output_directory.glob("*/" + OUTPUT_FILE_NAME))
    return reports[-1] if reports else None


def _positive_int(value: str) -> int:
    """1以上の整数の引数を検査する。"""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(
            "1以上の整数を指定してください: " + value
        )
    return number


def build_parser() -> argparse.ArgumentParser:
    """コマンドラインの引数の定義を作る。"""
    parser = argparse.ArgumentParser(
        description=(
            "調査のJSONの赤リンク（記事だけ）を、最新の状態で確かめて"
            "置換前の仮リンクに戻す。既定はドライランで、編集はしない。"
        )
    )
    parser.add_argument(
        "--report",
        type=Path,
        help=(
            "調査のJSON（既定: ill/resolved_redlinks/output の中で"
            "いちばん新しいもの）"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        help="扱うページの数の上限（ページ名順の先頭から）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "実際に編集する。リポジトリ直下の .env のログイン情報を使う。"
            "ボットの承認前は、1回の実行で{}ページまでにする".format(
                UNAPPROVED_MAX_EDITS
            )
        ),
    )
    parser.add_argument(
        "--max-edits",
        type=_positive_int,
        help="1回の実行で編集するページの数の上限（--execute のとき）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """結果をJSONと差分に保存し、件数の概要を標準出力に書く。"""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.max_edits is not None and not arguments.execute:
        parser.error("--max-edits は --execute と一緒に指定してください")
    report_path = arguments.report or latest_report(OUTPUT_DIRECTORY)
    if report_path is None:
        parser.error("調査のJSONがありません。先に調査を実行してください")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    try:
        pages = load_candidates(report)
    except ValueError as error:
        parser.error(str(error))
    if arguments.max_pages is not None:
        pages = {
            title: pages[title]
            for title in sorted(pages)[:arguments.max_pages]
        }

    api = wiki_api.JaWikipediaApi()
    normalizer = api.siteinfo_normalizer()
    if arguments.execute:
        load_dotenv(ENV_PATH)
        try:
            credentials = load_credentials(os.environ)
        except ValueError as error:
            parser.error(str(error))
        editor = Editor(api, credentials, EDIT_STATE_PATH)
        try:
            editor.login()
        except MediaWikiError as error:
            parser.exit(1, "ログインできません: {}\n".format(error))
        max_edits = arguments.max_edits
        if not editor.approved:
            max_edits = min(
                max_edits or UNAPPROVED_MAX_EDITS, UNAPPROVED_MAX_EDITS
            )
        results = execute(
            api,
            editor,
            pages,
            normalizer,
            lambda: datetime.now(timezone.utc),
            max_edits=max_edits,
        )
    else:
        results = dry_run(
            api, pages, normalizer, datetime.now(timezone.utc)
        )

    generated_at = datetime.now(timezone.utc)
    stem = "restore_{}{}".format(
        "execute_" if arguments.execute else "",
        generated_at.astimezone(JST).strftime("%Y%m%d_%H%M%S"),
    )
    results_path = report_path.parent / (stem + ".json")
    diff_path = report_path.parent / (stem + ".diff")
    with atomic_write(results_path) as stream:
        stream.write(
            render_results(
                results,
                generated_at=generated_at,
                report_path=str(report_path),
                dry_run=not arguments.execute,
            )
        )
    with atomic_write(diff_path) as stream:
        stream.write(render_diff(results))

    summary = summarize(results)
    print(
        "{}ページのうち{}ページ {}件。リンク先ごとの結果: {}".format(
            summary.pages,
            "編集した" if arguments.execute else "編集する",
            summary.pages_to_edit,
            "、".join(
                "{} {}件".format(reason, count)
                for reason, count in sorted(summary.targets.items())
            ),
        )
    )
    print("結果: {}".format(results_path))
    print("差分: {}".format(diff_path))
    return 0
