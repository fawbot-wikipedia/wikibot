"""コマンドラインの入口。引数を解析して処理の流れを実行し、件数の概要を表示する。"""

import argparse
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from ill.resolved_redlinks.paths import CACHE_PATH, OUTPUT_DIRECTORY
from ill.resolved_redlinks.pipeline import run
from ill.resolved_redlinks.report import STILL_REDLINK

DEFAULT_NAMESPACES = (0, 10)


def _parse_date(value: str) -> date:
    """`YYYY-MM-DD` の引数を日付にする。"""
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "日付は YYYY-MM-DD の形で指定してください: " + value
        ) from error


def _parse_dump_date(value: str) -> str:
    """`YYYYMMDD` の引数を検査する。"""
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "ダンプの日付は YYYYMMDD の形で指定してください: " + value
        ) from error
    return value


def build_parser() -> argparse.ArgumentParser:
    """コマンドラインの引数の定義を作る。"""
    parser = argparse.ArgumentParser(
        description=(
            "Cewbotが「解消済み仮リンク」として内部リンクに置き換えたリンクの"
            "うち、リンク先が現在赤リンク・壊れた転送になっていて、ページの"
            "最新版にも残っているものをJSONで一覧にする"
        )
    )
    parser.add_argument(
        "--namespace",
        type=int,
        action="append",
        help="対象の名前空間（複数指定可、既定: 0 と 10）",
    )
    parser.add_argument(
        "--since",
        type=_parse_date,
        help="この日（日本時間、YYYY-MM-DD）以降のCewbotの編集だけを対象にする",
    )
    parser.add_argument(
        "--until",
        type=_parse_date,
        help="この日（日本時間、YYYY-MM-DD）以前のCewbotの編集だけを対象にする",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="投稿記録のキャッシュを使わず最初から取得する",
    )
    parser.add_argument(
        "--dump-date",
        type=_parse_dump_date,
        help="一次判定に使うダンプの日付（YYYYMMDD、既定: 最新）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIRECTORY,
        help="出力先の親ディレクトリ（既定: ill/resolved_redlinks/output）",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=CACHE_PATH,
        help="キャッシュのファイル（既定: ill/resolved_redlinks/cache/"
        "cache.sqlite3）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """一覧を作って保存し、件数の概要を標準出力に書く。"""
    arguments = build_parser().parse_args(argv)
    namespaces = tuple(arguments.namespace or DEFAULT_NAMESPACES)
    if (
        arguments.since
        and arguments.until
        and arguments.since > arguments.until
    ):
        raise SystemExit("--since は --until 以前の日付にしてください")
    result = run(
        namespaces,
        arguments.since,
        arguments.until,
        refresh=arguments.refresh,
        dump_date=arguments.dump_date,
        output_dir=arguments.output_dir,
        cache_file=arguments.cache_file,
    )
    rows, counts = result.rows, result.counts
    still = sum(row.category == STILL_REDLINK for row in rows)
    print(
        "赤リンクのまま {}件、壊れた転送 {}件、仮リンクに戻された {}件、"
        "リンクが消えた {}件: {}".format(
            still,
            len(rows) - still,
            counts["restored_ill"],
            counts["link_removed"],
            result.output_path,
        )
    )
    return 0
