"""抽出結果をJSON形式で出力する。"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

JST = timezone(timedelta(hours=9), "JST")
PAGE_URL = "https://ja.wikipedia.org/wiki/"
INDEX_URL = "https://ja.wikipedia.org/w/index.php"

STILL_REDLINK = "still_redlink"
BROKEN_REDIRECT = "broken_redirect"
NAMESPACE_TITLES = {0: "記事", 10: "Template"}
# 削除の理由には削除前の本文の冒頭が自動で入ることがあるので、この長さで切る。
LOG_COMMENT_MAX_LENGTH = 100


@dataclass(frozen=True)
class ReportRow:
    """一覧の1行。"""

    category: str
    namespace: int
    page_title: str
    target_title: str
    revid: int
    timestamp: str
    log_note: str
    original_ill: str | None = None
    wording: str | None = None
    original_ill_error: str | None = None
    replaced_ills: tuple[str, ...] = ()


def page_url(title: str) -> str:
    """ページのURLを、記事名をパーセントエンコードして作る。"""
    return PAGE_URL + quote(title.replace(" ", "_"), safe="!$'*,;=@-._~:")


def to_jst_date(timestamp: str) -> str:
    """APIの時刻（ISO 8601、UTC）を日本時間の日付にする。"""
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return moment.astimezone(JST).strftime("%Y-%m-%d")


def render_json_report(
    rows: Iterable[ReportRow],
    *,
    generated_at: datetime,
    dump_date: str,
    namespaces: Sequence[int],
    namespace_names: Mapping[int, str],
    since: str | None,
    until: str | None,
    reference_counts: Mapping[str, int],
) -> str:
    """一覧をメタデータ・件数・リンク単位のレコードを含むJSONにする。"""
    rows = sorted(
        rows,
        key=lambda row: (
            row.category,
            row.namespace,
            row.page_title,
            row.target_title,
        ),
    )

    def namespace_title(number: int) -> str:
        """名前空間番号に対応する表示名を返す。"""
        return NAMESPACE_TITLES.get(
            number, namespace_names.get(number) or str(number)
        )

    categories = {
        STILL_REDLINK: sum(row.category == STILL_REDLINK for row in rows),
        BROKEN_REDIRECT: sum(row.category == BROKEN_REDIRECT for row in rows),
    }
    report = {
        "schema_version": 2,
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Tokyo",
        "dump_date": dump_date,
        "filters": {
            "namespaces": [
                {"id": number, "name": namespace_title(number)}
                for number in namespaces
            ],
            "period": {"since": since, "until": until},
        },
        "counts": {
            **categories,
            "restored_ill": reference_counts.get("restored_ill", 0),
            "link_removed": reference_counts.get("link_removed", 0),
            "page_gone": reference_counts.get("page_gone", 0),
        },
        "items": [
            {
                "category": row.category,
                "namespace": {
                    "id": row.namespace,
                    "name": namespace_title(row.namespace),
                },
                "page": {
                    "title": row.page_title,
                    "url": page_url(row.page_title),
                },
                "target": {
                    "title": row.target_title,
                    "url": page_url(row.target_title),
                },
                "replacement": {
                    "revision_id": row.revid,
                    "url": "{}?oldid={}".format(INDEX_URL, row.revid),
                    "diff_url": "{}?diff={}".format(INDEX_URL, row.revid),
                    "timestamp": row.timestamp,
                    "date_jst": to_jst_date(row.timestamp),
                },
                "original_ill": row.original_ill,
                "wording": row.wording,
                "original_ill_error": row.original_ill_error,
                "replaced_ills": list(row.replaced_ills),
                "log_note": row.log_note,
            }
            for row in rows
        ],
    }
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def format_log_note(
    entry_action: str | None,
    entry_timestamp: str | None,
    entry_comment: str | None,
    entry_target: str | None,
    redirect_target: str | None = None,
) -> str:
    """    削除・移動の記録を表示する文を作る。

    壊れた転送では、転送先のページ名を先に書く。記録がなければ空欄にする。
    理由は空白をまとめ、長ければ100文字で切って
    「…」を付ける。
    """
    parts = []
    if redirect_target:
        parts.append("転送先「{}」".format(redirect_target))
    if entry_action and entry_timestamp:
        if entry_action == "delete/delete":
            text = "{} 削除".format(to_jst_date(entry_timestamp))
        else:
            text = "{} 移動".format(to_jst_date(entry_timestamp))
            if entry_target:
                text += "（→ {}）".format(entry_target)
        if entry_comment:
            comment = " ".join(entry_comment.split())
            if len(comment) > LOG_COMMENT_MAX_LENGTH:
                comment = comment[:LOG_COMMENT_MAX_LENGTH] + "…"
            text += ": " + comment
        parts.append(text)
    return " ".join(parts)
