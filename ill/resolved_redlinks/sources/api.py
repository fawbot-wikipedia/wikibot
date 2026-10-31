"""日本語版ウィキペディアのAPIへの問い合わせ。"""

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wikiutil.log import log
from wikiutil.mediawiki import MediaWikiError, MediaWikiSession, api_url

from ill.resolved_redlinks.parsing.summary import is_target_summary
from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.sources.cache import Cache, Edit
from ill.resolved_redlinks.sources.user_agent import USER_AGENT

API_URL = api_url("ja")
CEWBOT = "Cewbot"
BATCH_SIZE = 50
MAXLAG_SECONDS = 5
MAX_MAXLAG_RETRIES = 10
LOG_ACTIONS = ("delete/delete", "move/move", "move/move_redir")
# 版の本文が見られない（削除・版指定削除など）ときのエラーコード
UNAVAILABLE_REVISION_CODES = {
    "missingcontent",
    "nosuchrevid",
    "permissiondenied",
}


class ApiError(MediaWikiError):
    """APIがエラーを返した。エラーコードを `code` に持つ。"""

    def __init__(self, code: str | None, error: Mapping[str, Any]) -> None:
        """エラーコードと、応答の `error` を受け取る。"""
        super().__init__("MediaWiki APIがエラーを返しました: {}".format(error))
        self.code = code


class JaWikipediaApi:
    """日本語版のAPIを、`maxlag` の待機と `continue` の処理付きで呼ぶ。"""

    def __init__(
        self,
        session: MediaWikiSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """要求に使う Session を受け取る。省略時は新しく作る。

        読み取りだけなので、POSTの要求も一時的な失敗のときに再試行する
        （ページ名を50件並べるとURLが長くなるため、POSTで送る）。
        """
        self.session = session or MediaWikiSession(
            user_agent=USER_AGENT,
            on_retry=log,
            retry_methods=("GET", "POST"),
        )
        self.sleep = sleep

    def call(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """APIを1回呼び、JSONを返す。サーバーが混んでいれば待って再試行する。

        Raises:
            MediaWikiError: APIエラーが返った場合や、混雑が続く場合。
        """
        request = {
            "format": "json",
            "formatversion": 2,
            "maxlag": MAXLAG_SECONDS,
            **params,
        }
        for attempt in range(MAX_MAXLAG_RETRIES + 1):
            response = self.session.post(API_URL, data=request)
            response.raise_for_status()
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            if error and error.get("code") == "maxlag":
                if attempt >= MAX_MAXLAG_RETRIES:
                    break
                delay = float(response.headers.get("Retry-After") or 5)
                log("サーバーが混んでいるため{:.0f}秒待ちます".format(delay))
                self.sleep(delay)
                continue
            if not isinstance(data, dict):
                raise MediaWikiError("MediaWiki APIの応答の形式が不正です")
            if error:
                raise ApiError(error.get("code"), error)
            return data
        raise MediaWikiError("サーバーの混雑が続いたため要求を中止しました")

    def query_pages(
        self, params: Mapping[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """`action=query` を `continue` で最後まで続け、各応答を返す。"""
        request = {"action": "query", **params}
        continuation: dict[str, Any] = {}
        while True:
            data = self.call({**request, **continuation})
            yield data
            if "continue" not in data:
                return
            continuation = data["continue"]

    def siteinfo_normalizer(self) -> TitleNormalizer:
        """名前空間の名前と別名を取得し、ページ名の正規化に使う。"""
        data = self.call(
            {
                "action": "query",
                "meta": "siteinfo",
                "siprop": "namespaces|namespacealiases",
            }
        )
        return TitleNormalizer.from_siteinfo(data)


def _edit_from_contrib(item: Mapping[str, Any]) -> Edit:
    """投稿記録の1件を Edit にする。"""
    return Edit(
        revid=int(item["revid"]),
        parentid=int(item.get("parentid", 0)),
        pageid=int(item["pageid"]),
        namespace=int(item["ns"]),
        title=item["title"],
        timestamp=item["timestamp"],
        comment=item["comment"],
    )


def update_contributions(api: JaWikipediaApi, cache: Cache) -> dict[str, int]:
    """Cewbotの投稿記録を取得し、置き換えの編集をキャッシュに追加する。

    初回は全期間を新しい順に取得し、要求ごとに再開位置を保存する。最後まで
    取得し終えた後は、保存済みの最新時刻より新しい編集だけを取得する。

    Returns:
        取得した投稿の件数、追加した置き換えの編集の件数、要約が隠された件数。
    """
    stats = {"fetched": 0, "added": 0, "comment_hidden": 0}
    complete = cache.get_meta("contribs_complete") == "1"
    params: dict[str, Any] = {
        "list": "usercontribs",
        "ucuser": CEWBOT,
        "ucprop": "ids|title|timestamp|comment",
        "uclimit": "max",
    }
    continuation: dict[str, Any] = {}
    if complete:
        params["ucend"] = cache.get_meta("contribs_latest")
        log("投稿記録の差分取得 開始: {} 以降".format(params["ucend"]))
    else:
        saved = cache.get_meta("contribs_continue")
        if saved:
            continuation = {"uccontinue": saved, "continue": "-||"}
            log("投稿記録の初回取得を途中から再開します")
        else:
            log("投稿記録の初回取得 開始（全期間、約90万件）")

    newest = None
    while True:
        data = api.call({"action": "query", **params, **continuation})
        items = data.get("query", {}).get("usercontribs", [])
        edits = []
        for item in items:
            if newest is None or item["timestamp"] > newest:
                newest = item["timestamp"]
            if item.get("commenthidden"):
                stats["comment_hidden"] += 1
                continue
            if is_target_summary(item.get("comment")):
                edits.append(_edit_from_contrib(item))
        stats["fetched"] += len(items)
        next_continue = data.get("continue")
        with cache.transaction():
            stats["added"] += cache.add_edits(edits)
            if not complete:
                pending = cache.get_meta("contribs_pending_latest")
                if pending is None and newest:
                    cache.set_meta("contribs_pending_latest", newest)
                cache.set_meta(
                    "contribs_continue",
                    next_continue["uccontinue"] if next_continue else None,
                )
        if stats["fetched"] and stats["fetched"] % 50000 < len(items):
            log(
                "投稿記録 {}件取得（置き換えの編集 {}件を追加）".format(
                    stats["fetched"], stats["added"]
                )
            )
        if not next_continue:
            break
        continuation = next_continue

    with cache.transaction():
        if complete:
            if newest:
                latest = cache.get_meta("contribs_latest") or ""
                cache.set_meta("contribs_latest", max(latest, newest))
        else:
            pending = cache.get_meta("contribs_pending_latest") or newest
            if pending:
                cache.set_meta("contribs_latest", pending)
            cache.set_meta("contribs_pending_latest", None)
            cache.set_meta("contribs_complete", "1")
    log(
        "投稿記録の取得 完了: {}件取得、置き換えの編集 {}件を追加".format(
            stats["fetched"], stats["added"]
        )
    )
    return stats


def fetch_compare_body(api: JaWikipediaApi, revid: int) -> str | None:
    """その編集と1つ前の版との差分表のHTMLを返す。

    版の本文が削除・版指定削除などで見られない場合は None を返す。

    Raises:
        MediaWikiError: 差分を取得できない場合。
    """
    try:
        data = api.call(
            {"action": "compare", "fromrev": revid, "torelative": "prev"}
        )
    except ApiError as error:
        if error.code in UNAVAILABLE_REVISION_CODES:
            return None
        raise
    body = data.get("compare", {}).get("body")
    if body is None:
        raise MediaWikiError("差分を取得できません: 版 {}".format(revid))
    return body


@dataclass(frozen=True)
class LogEntry:
    """削除・移動の記録1件。"""

    action: str
    timestamp: str
    comment: str
    title: str
    target: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """キャッシュに保存する形にする。"""
        return {
            "action": self.action,
            "timestamp": self.timestamp,
            "comment": self.comment,
            "title": self.title,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LogEntry":
        """キャッシュに保存した形から戻す。"""
        return cls(
            action=value["action"],
            timestamp=value["timestamp"],
            comment=value.get("comment", ""),
            title=value["title"],
            target=value.get("target"),
        )


def _log_entry(item: Mapping[str, Any]) -> LogEntry:
    """`list=logevents` の1件を LogEntry にする。"""
    params = item.get("params") or {}
    return LogEntry(
        action="{}/{}".format(item.get("type"), item.get("action")),
        timestamp=item.get("timestamp", ""),
        comment=item.get("comment", ""),
        title=item.get("title", ""),
        target=params.get("target_title"),
    )


def fetch_recent_log_entries(
    api: JaWikipediaApi, since: str
) -> list[LogEntry]:
    """指定時刻（ISO 8601、UTC）以降の削除・移動の記録をすべて返す。"""
    entries = []
    for action in LOG_ACTIONS:
        log("{} の記録を {} まで遡って取得".format(action, since))
        for data in api.query_pages(
            {
                "list": "logevents",
                "leaction": action,
                "leend": since,
                "leprop": "title|type|timestamp|comment|details",
                "lelimit": "max",
            }
        ):
            for item in data.get("query", {}).get("logevents", []):
                if not item.get("actionhidden") and "title" in item:
                    entries.append(_log_entry(item))
    return entries


def fetch_last_log_entry(api: JaWikipediaApi, title: str) -> LogEntry | None:
    """名前ごとの最後の削除・移動の記録を返す。なければ None。"""
    data = api.call(
        {
            "action": "query",
            "list": "logevents",
            "letitle": title,
            "leprop": "title|type|timestamp|comment|details",
            "lelimit": 50,
        }
    )
    for item in data.get("query", {}).get("logevents", []):
        action = "{}/{}".format(item.get("type"), item.get("action"))
        if action in LOG_ACTIONS and not item.get("actionhidden"):
            return _log_entry(item)
    return None


@dataclass(frozen=True)
class TargetStatus:
    """リンク先の現在の状態。"""

    state: str
    redirect_target: str | None = None


REDLINK = "redlink"
BROKEN_REDIRECT = "broken_redirect"
EXISTS = "exists"
INVALID = "invalid"


def fetch_target_status(
    api: JaWikipediaApi, titles: Sequence[str]
) -> dict[str, TargetStatus]:
    """リンク先の現在の状態（赤リンク・壊れた転送・存在・無効）を返す。

    50件ずつ `prop=info`（`redirects` 指定）で問い合わせる。転送ページは
    1段だけたどり、転送先が存在しなければ壊れた転送とする。
    """
    result: dict[str, TargetStatus] = {}
    for start in range(0, len(titles), BATCH_SIZE):
        batch = list(titles[start:start + BATCH_SIZE])
        pages: dict[str, dict[str, Any]] = {}
        redirects: dict[str, str] = {}
        normalized: dict[str, str] = {}
        for data in api.query_pages(
            {"prop": "info", "titles": "|".join(batch), "redirects": 1}
        ):
            query = data.get("query", {})
            for page in query.get("pages", []):
                pages[page.get("title", "")] = page
            for item in query.get("redirects", []):
                redirects[item["from"]] = item["to"]
            for item in query.get("normalized", []) + query.get(
                "converted", []
            ):
                normalized[item["from"]] = item["to"]
        for title in batch:
            resolved = normalized.get(title, title)
            if resolved in redirects:
                target = redirects[resolved]
                page = pages.get(target, {})
                broken = (
                    page.get("missing") is not None
                    or page.get("invalid") is not None
                )
                result[title] = TargetStatus(
                    BROKEN_REDIRECT if broken else EXISTS, target
                )
                continue
            page = pages.get(resolved)
            if page is None:
                raise MediaWikiError(
                    "APIの応答にページ情報がありません: {}".format(title)
                )
            if page.get("invalid") is not None:
                result[title] = TargetStatus(INVALID)
            elif page.get("missing") is not None or page.get("length") == 0:
                result[title] = TargetStatus(REDLINK)
            else:
                result[title] = TargetStatus(EXISTS)
    return result


@dataclass(frozen=True)
class LatestPage:
    """ページの最新版。"""

    pageid: int
    namespace: int
    title: str
    revid: int
    text: str


def fetch_latest_pages(
    api: JaWikipediaApi, pageids: Iterable[int]
) -> dict[int, LatestPage]:
    """ページIDごとの最新版の本文を返す。削除されたページは含まない。"""
    ids = sorted(set(pageids))
    result: dict[int, LatestPage] = {}
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start:start + BATCH_SIZE]
        for data in api.query_pages(
            {
                "prop": "revisions",
                "pageids": "|".join(map(str, batch)),
                "rvprop": "ids|content",
                "rvslots": "main",
            }
        ):
            for page in data.get("query", {}).get("pages", []):
                if page.get("missing") is not None:
                    continue
                revisions = page.get("revisions") or []
                if not revisions:
                    continue
                main = revisions[0].get("slots", {}).get("main", {})
                if "content" not in main:
                    continue
                result[int(page["pageid"])] = LatestPage(
                    pageid=int(page["pageid"]),
                    namespace=int(page["ns"]),
                    title=page["title"],
                    revid=int(revisions[0]["revid"]),
                    text=main["content"],
                )
    return result
