"""投稿記録・差分から拾ったリンク先・削除と移動の記録をSQLiteに保存する。"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ill.resolved_redlinks.paths import CACHE_PATH

SCHEMA_VERSION = "1"
DEFAULT_CACHE_PATH = CACHE_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edits (
    revid INTEGER PRIMARY KEY,
    parentid INTEGER NOT NULL,
    pageid INTEGER NOT NULL,
    namespace INTEGER NOT NULL,
    title TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    comment TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diff_links (
    revid INTEGER PRIMARY KEY,
    targets TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diff_ills (
    revid INTEGER PRIMARY KEY,
    templates TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_entries (
    title TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    entry TEXT
);
"""


@dataclass(frozen=True)
class Edit:
    """Cewbotの置き換えの編集1件。"""

    revid: int
    parentid: int
    pageid: int
    namespace: int
    title: str
    timestamp: str
    comment: str


class Cache:
    """ツールのキャッシュ。書き込みは `with cache.transaction():` の中で行う。"""

    def __init__(self, path: Path = DEFAULT_CACHE_PATH) -> None:
        """キャッシュを開き、表がなければ作る。

        Raises:
            RuntimeError: キャッシュを読めない、または表の版が合わない場合。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        try:
            try:
                self.connection.executescript(_SCHEMA)
                version = self.get_meta("schema_version")
            except sqlite3.DatabaseError as error:
                raise RuntimeError(
                    "キャッシュを読み込めません: {}: {}".format(path, error)
                ) from error
            if version is None:
                with self.transaction():
                    self.set_meta("schema_version", SCHEMA_VERSION)
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    "キャッシュの版が合いません（期待: {}、実際: {}）。"
                    "キャッシュのファイルを削除してから実行し直してください: "
                    "{}".format(SCHEMA_VERSION, version, path)
                )
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        """接続を閉じる。"""
        self.connection.close()

    def __enter__(self) -> "Cache":
        """with 文で使うためにキャッシュ自身を返す。"""
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """with 文を抜けるときに接続を閉じる。"""
        self.close()

    def transaction(self) -> sqlite3.Connection:
        """with 文で使うトランザクション。例外が起きたら変更を取り消す。"""
        return self.connection

    def get_meta(self, key: str) -> str | None:
        """メタ情報の値を返す。なければ None。"""
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        """メタ情報を保存する。None なら消す。"""
        if value is None:
            self.connection.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def clear_edits(self) -> None:
        """投稿記録と、その取得状況を消す（`--refresh`）。"""
        with self.transaction():
            self.connection.execute("DELETE FROM edits")
            for key in (
                "contribs_complete",
                "contribs_continue",
                "contribs_latest",
                "contribs_pending_latest",
            ):
                self.set_meta(key, None)

    def add_edits(self, edits: Iterable[Edit]) -> int:
        """編集を追加し、新しく加わった件数を返す。同じ revision ID は無視する。"""
        before = self.connection.total_changes
        self.connection.executemany(
            "INSERT OR IGNORE INTO edits (revid, parentid, pageid, namespace,"
            " title, timestamp, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    edit.revid,
                    edit.parentid,
                    edit.pageid,
                    edit.namespace,
                    edit.title,
                    edit.timestamp,
                    edit.comment,
                )
                for edit in edits
            ),
        )
        return self.connection.total_changes - before

    def iter_edits(self) -> Iterator[Edit]:
        """保存した編集を、新しい順に返す。"""
        cursor = self.connection.execute(
            "SELECT revid, parentid, pageid, namespace, title, timestamp,"
            " comment FROM edits ORDER BY timestamp DESC, revid DESC"
        )
        for row in cursor:
            yield Edit(*row)

    def count_edits(self) -> int:
        """保存した編集の件数を返す。"""
        return self.connection.execute(
            "SELECT COUNT(*) FROM edits"
        ).fetchone()[0]

    def get_diff_links(self, revid: int) -> list[str] | None:
        """差分から拾ったリンク先を返す。未取得なら None。"""
        row = self.connection.execute(
            "SELECT targets FROM diff_links WHERE revid = ?", (revid,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_diff_links(self, revid: int, targets: list[str]) -> None:
        """差分から拾ったリンク先を保存する。"""
        with self.transaction():
            self.connection.execute(
                "INSERT OR REPLACE INTO diff_links (revid, targets)"
                " VALUES (?, ?)",
                (revid, json.dumps(targets, ensure_ascii=False)),
            )

    def get_diff_ills(self, revid: int) -> list[str] | None:
        """差分から拾った置換前の仮リンクを返す。未取得なら None。"""
        row = self.connection.execute(
            "SELECT templates FROM diff_ills WHERE revid = ?", (revid,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_diff_ills(self, revid: int, templates: list[str]) -> None:
        """置換前の仮リンクを版ごとに保存する。"""
        with self.transaction():
            self.connection.execute(
                "INSERT OR REPLACE INTO diff_ills (revid, templates)"
                " VALUES (?, ?)",
                (revid, json.dumps(templates, ensure_ascii=False)),
            )

    def get_log_entry(
        self, title: str
    ) -> tuple[str, dict[str, Any] | None] | None:
        """名前ごとの最後の削除・移動の記録と、確かめた時刻を返す。未確認なら None。"""
        row = self.connection.execute(
            "SELECT checked_at, entry FROM log_entries WHERE title = ?",
            (title,),
        ).fetchone()
        if row is None:
            return None
        return row[0], json.loads(row[1]) if row[1] else None

    def set_log_entry(
        self, title: str, checked_at: str, entry: dict[str, Any] | None
    ) -> None:
        """名前ごとの最後の削除・移動の記録を保存する。"""
        with self.transaction():
            self.connection.execute(
                "INSERT OR REPLACE INTO log_entries (title, checked_at, entry)"
                " VALUES (?, ?, ?)",
                (
                    title,
                    checked_at,
                    json.dumps(entry, ensure_ascii=False) if entry else None,
                ),
            )
