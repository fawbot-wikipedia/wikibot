"""ボットのログインと、編集の間隔・緊急停止・保存。"""

import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wikiutil.files import write_json
from wikiutil.log import log
from wikiutil.mediawiki import MediaWikiError

from ill.resolved_redlinks.sources import api as wiki_api

MIN_EDIT_INTERVAL_SECONDS = 60
# 間隔に加える揺らぎの上限（秒）。
EDIT_INTERVAL_JITTER_SECONDS = 100
# ボットの承認前に、1回の実行で編集してよいページ数。
UNAPPROVED_MAX_EDITS = 10
STOP_PAGE = "利用者:{}/停止"


@dataclass(frozen=True)
class Credentials:
    """BotPasswordのログイン名（`利用者名@BotPassword名`）とパスワード。"""

    username: str
    password: str = field(repr=False)

    @property
    def bot_name(self) -> str:
        """ボットの利用者名（ログイン名の `@` より前）。"""
        return self.username.split("@", 1)[0]


def load_credentials(environ: Mapping[str, str]) -> Credentials:
    """環境変数からログイン情報を読む。

    Raises:
        ValueError: 足りない、またはログイン名の形が違う場合。
    """
    username = environ.get("WIKIPEDIA_USERNAME", "").strip()
    password = environ.get("WIKIPEDIA_BOT_PASSWORD", "")
    if not username or not password:
        raise ValueError(
            ".env に WIKIPEDIA_USERNAME と WIKIPEDIA_BOT_PASSWORD を"
            "設定してください"
        )
    if "@" not in username:
        raise ValueError(
            "WIKIPEDIA_USERNAME は「利用者名@BotPassword名」の形で"
            "設定してください"
        )
    return Credentials(username, password)


def _same_user(left: str, right: str) -> bool:
    """利用者名を、先頭1文字の大文字・小文字と `_` の違いを無視して比べる。"""

    def key(name: str) -> str:
        name = name.replace("_", " ").strip()
        return name[:1].upper() + name[1:]

    return key(left) == key(right)


class Editor:
    """ログインした状態で、間隔を守ってページを保存する。"""

    def __init__(
        self,
        api: wiki_api.JaWikipediaApi,
        credentials: Credentials,
        state_path: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        jitter: Callable[[], float] = lambda: random.randint(
            0, EDIT_INTERVAL_JITTER_SECONDS
        ),
    ) -> None:
        """APIと、ログイン情報、次に編集してよい時刻の保存先を受け取る。"""
        self.api = api
        self.credentials = credentials
        self.state_path = state_path
        self.sleep = sleep
        self.now = now
        self.jitter = jitter
        self.approved = False
        self._token: str | None = None

    def login(self) -> None:
        """ログインし、ボットの承認を受けているか（`bot` グループ）を確かめる。

        Raises:
            MediaWikiError: ログインできない場合や、ログインした利用者が
                ログイン名と違う場合。
        """
        session = self.api.session
        session.login(
            wiki_api.API_URL,
            self.credentials.username,
            self.credentials.password,
        )
        info = session.user_info(wiki_api.API_URL)
        if not _same_user(info.get("name", ""), self.credentials.bot_name):
            raise MediaWikiError(
                "ログインした利用者がログイン名と違います: {}".format(
                    info.get("name")
                )
            )
        self.approved = "bot" in info.get("groups", [])
        self._token = session.csrf_token(wiki_api.API_URL)
        log(
            "{} でログインしました（{}）".format(
                info["name"],
                "ボットの承認あり" if self.approved else "ボットの承認前",
            )
        )

    def stop_reason(self) -> str | None:
        """編集を止めるべき理由を返す。続けてよければ None。

        会話ページに新しいメッセージがある場合と、停止用のページに
        書き込みがある場合に止める。
        """
        info = self.api.session.user_info(wiki_api.API_URL)
        if info.get("messages"):
            return "会話ページに新しいメッセージがあります"
        stop_page = STOP_PAGE.format(self.credentials.bot_name)
        page = wiki_api.fetch_pages_for_edit(self.api, [stop_page]).get(
            stop_page
        )
        if page is not None and page.text.strip():
            return "停止用のページ「{}」に書き込みがあります".format(stop_page)
        return None

    def _next_edit_at(self) -> datetime | None:
        """保存した、次に編集してよい時刻を返す。"""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        value = state.get("next_edit_at") if isinstance(state, dict) else None
        if value is None:
            return None
        return datetime.fromisoformat(value)

    def wait_turn(self) -> None:
        """前の編集から間隔が空くまで待つ。実行をまたいでも守る。"""
        next_edit_at = self._next_edit_at()
        if next_edit_at is None:
            return
        remaining = (next_edit_at - self.now()).total_seconds()
        if remaining > 0:
            log("編集の間隔を空けるため{:.0f}秒待ちます".format(remaining))
            self.sleep(remaining)

    def save(
        self, page: wiki_api.EditablePage, text: str, summary: str
    ) -> dict[str, Any]:
        """取得した版を基準に本文を保存し、応答の `edit` を返す。

        送った後は、成否にかかわらず次に編集してよい時刻を記録する。
        """
        if self._token is None:
            raise MediaWikiError("ログインしていません")
        try:
            return self.api.session.edit(
                wiki_api.API_URL,
                title=page.title,
                text=text,
                summary=summary,
                token=self._token,
                base_revision_id=page.revid,
                base_timestamp=page.timestamp,
                start_timestamp=page.fetched_at,
                bot=self.approved,
                assertion="bot" if self.approved else "user",
            )
        finally:
            next_edit_at = self.now() + timedelta(
                seconds=MIN_EDIT_INTERVAL_SECONDS + self.jitter()
            )
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                self.state_path, {"next_edit_at": next_edit_at.isoformat()}
            )
