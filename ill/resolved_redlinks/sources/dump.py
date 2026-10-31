"""`page.sql`・`redirect.sql` のダンプを取得し、リンク先を一次判定する。"""

import gzip
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wikiutil.dumps import (
    dated_file_name,
    dated_file_url,
    download_file,
    fetch_dump_sha1s,
    fetch_latest_dump_date,
    verify_sha1,
)
from wikiutil.log import log

from ill.resolved_redlinks.parsing.titles import TitleKey, TitleNormalizer
from ill.resolved_redlinks.paths import INPUT_DIRECTORY
from ill.resolved_redlinks.sources.user_agent import USER_AGENT

DEFAULT_INPUT_DIRECTORY = INPUT_DIRECTORY
PAGE_SUFFIX = "page.sql.gz"
REDIRECT_SUFFIX = "redirect.sql.gz"

_STRING = r"'((?:[^'\\]|\\.)*)'"
_NULLABLE_STRING = r"(?:'((?:[^'\\]|\\.)*)'|NULL)"
# page: (page_id, page_namespace, page_title, page_is_redirect, ...)
_PAGE_ROW = re.compile(r"\((\d+),(-?\d+)," + _STRING + r",(\d),")
# redirect: (rd_from, rd_namespace, rd_title, rd_interwiki, rd_fragment)
_REDIRECT_ROW = re.compile(
    r"\((\d+),(-?\d+),"
    + _STRING
    + ","
    + _NULLABLE_STRING
    + ","
    + _NULLABLE_STRING
    + r"\)"
)
_ESCAPES = {
    "0": "\0",
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
}
_ESCAPE = re.compile(r"\\(.)", flags=re.DOTALL)


def unescape_sql(value: str) -> str:
    """MySQLの文字列リテラル中のバックスラッシュによるエスケープを戻す。"""
    return _ESCAPE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), value)


def _insert_lines(path: Path) -> Iterator[str]:
    """ダンプのうち、行のデータを含む部分だけを返す。"""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("(") or line.startswith("INSERT INTO"):
                yield line


def iter_page_rows(path: Path) -> Iterator[tuple[int, int, str, bool]]:
    """`page.sql.gz` の各行を (page_id, 名前空間, ページ名, 転送か) で返す。"""
    for line in _insert_lines(path):
        for match in _PAGE_ROW.finditer(line):
            yield (
                int(match.group(1)),
                int(match.group(2)),
                unescape_sql(match.group(3)),
                match.group(4) == "1",
            )


def iter_redirect_rows(
    path: Path,
) -> Iterator[tuple[int, int, str, str, str]]:
    """`redirect.sql.gz` の各行を (転送元ID, 名前空間, ページ名, ウィキ間接頭辞, 節) で返す。"""
    for line in _insert_lines(path):
        for match in _REDIRECT_ROW.finditer(line):
            yield (
                int(match.group(1)),
                int(match.group(2)),
                unescape_sql(match.group(3)),
                unescape_sql(match.group(4) or ""),
                unescape_sql(match.group(5) or ""),
            )


@dataclass(frozen=True)
class DumpFiles:
    """一次判定に使うダンプのファイル。"""

    dump_date: str
    page_path: Path
    redirect_path: Path


def ensure_dump_files(
    dump_date: str | None = None,
    input_directory: Path = DEFAULT_INPUT_DIRECTORY,
) -> DumpFiles:
    """`page.sql.gz`・`redirect.sql.gz` を取得し、そのパスを返す。

    日付を省略した場合は、`latest` のRSSから両ファイルの実行日を決める。
    取得済みなら再取得しない。新しく取得したファイルは `dumpstatus.json` の
    sha1と照合する。

    Raises:
        RuntimeError: 実行日がそろわない、取得に失敗した、sha1が合わない場合。
    """
    if dump_date is None:
        page_date = fetch_latest_dump_date(PAGE_SUFFIX, USER_AGENT)
        redirect_date = fetch_latest_dump_date(REDIRECT_SUFFIX, USER_AGENT)
        if page_date != redirect_date:
            raise RuntimeError(
                "page.sql と redirect.sql のダンプの実行日が違います"
                "（{} と {}）。しばらく待ってから実行してください".format(
                    page_date, redirect_date
                )
            )
        dump_date = page_date
    directory = input_directory / dump_date
    paths = []
    sha1s: dict[str, str] | None = None
    for suffix in (PAGE_SUFFIX, REDIRECT_SUFFIX):
        name = dated_file_name(dump_date, suffix)
        path = directory / name
        if not path.is_file():
            log("ダンプを取得します: {}".format(name))
            download_file(
                path, dated_file_url(dump_date, suffix), USER_AGENT, name
            )
            if sha1s is None:
                sha1s = fetch_dump_sha1s(dump_date, USER_AGENT)
            if name not in sha1s:
                path.unlink()
                raise RuntimeError(
                    "dumpstatus.json に {} のsha1がありません".format(name)
                )
            try:
                verify_sha1(path, sha1s[name])
            except RuntimeError:
                path.unlink()
                raise
        paths.append(path)
    return DumpFiles(dump_date, paths[0], paths[1])


REDLINK_CANDIDATE = "redlink"
BROKEN_REDIRECT_CANDIDATE = "broken_redirect"


@dataclass(frozen=True)
class DumpJudgement:
    """ダンプでの一次判定の結果。"""

    candidates: dict[TitleKey, str]
    redirect_targets: dict[TitleKey, TitleKey]


def judge_targets(
    files: DumpFiles,
    targets: set[TitleKey],
    normalizer: TitleNormalizer,
) -> DumpJudgement:
    """リンク先をダンプで一次判定し、赤リンク・壊れた転送の候補を返す。

    `page` を全件メモリーに持たないよう、リンク先の集合と照らし合わせながら
    `page.sql` を2回、`redirect.sql` を1回読む。他のウィキへの転送と、
    転送先のページがある転送（二重転送を含む）は候補にしない。転送表に
    行がない転送ページは判定できないので、候補に入れてAPIで確かめる。

    Returns:
        候補（リンク先ごとの候補の種類）と、転送ページのリンク先ごとの転送先。
    """
    log("page.sql の照合 開始（リンク先 {}件）".format(len(targets)))
    found: dict[TitleKey, tuple[int, bool]] = {}
    for page_id, namespace, title, is_redirect in iter_page_rows(
        files.page_path
    ):
        key = normalizer.dump_key(namespace, title)
        if key in targets:
            found[key] = (page_id, is_redirect)
    redirect_ids = {
        page_id: key
        for key, (page_id, is_redirect) in found.items()
        if is_redirect
    }
    log(
        "page.sql の照合 完了: 存在 {}件（うち転送 {}件）".format(
            len(found), len(redirect_ids)
        )
    )

    redirect_targets: dict[TitleKey, TitleKey] = {}
    interwiki_redirects: set[TitleKey] = set()
    for rd_from, namespace, title, interwiki, _ in iter_redirect_rows(
        files.redirect_path
    ):
        key = redirect_ids.get(rd_from)
        if key is None:
            continue
        if interwiki:
            interwiki_redirects.add(key)
        else:
            redirect_targets[key] = normalizer.dump_key(namespace, title)

    target_keys = set(redirect_targets.values())
    existing_targets: set[TitleKey] = set()
    if target_keys:
        for _, namespace, title, _ in iter_page_rows(files.page_path):
            key = normalizer.dump_key(namespace, title)
            if key in target_keys:
                existing_targets.add(key)

    candidates: dict[TitleKey, str] = {}
    for key in targets:
        if key not in found:
            candidates[key] = REDLINK_CANDIDATE
        elif key in redirect_targets:
            if redirect_targets[key] not in existing_targets:
                candidates[key] = BROKEN_REDIRECT_CANDIDATE
        elif found[key][1] and key not in interwiki_redirects:
            candidates[key] = BROKEN_REDIRECT_CANDIDATE
    log(
        "ダンプでの一次判定 完了: 赤リンク候補 {}件、壊れた転送候補 {}件"
        "（他のウィキへの転送 {}件は対象外）".format(
            sum(v == REDLINK_CANDIDATE for v in candidates.values()),
            sum(v == BROKEN_REDIRECT_CANDIDATE for v in candidates.values()),
            len(interwiki_redirects),
        )
    )
    return DumpJudgement(candidates, redirect_targets)
