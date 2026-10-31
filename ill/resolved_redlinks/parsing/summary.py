"""Cewbotの「解消済み仮リンク」の編集要約を解析する。"""

import re
from dataclasses import dataclass

# 2016〜2020年ごろの要約は「bot: 」で始まる。直後が「[[」であることで、
# 利用者ページの「…を内部リンクに置き換える作業の報告」を除く。
TARGET_SUMMARY = re.compile(r"^(?:bot: )?解消済み仮リンク(?=\[\[)")
# 2016年ごろの要約は「…を内部リンクに置き換える」で終わる。
SUMMARY_END = "を内部リンクに置き換え"


@dataclass(frozen=True)
class SummaryLinks:
    """要約から取り出したリンク先と、要約が途中で切れているか。"""

    targets: tuple[str, ...]
    truncated: bool


def is_target_summary(comment: str | None) -> bool:
    """仮リンクを内部リンクに置き換えた編集の要約かを返す。"""
    return bool(comment) and TARGET_SUMMARY.match(comment) is not None


def parse_summary_links(comment: str) -> SummaryLinks:
    """要約から、置き換えた内部リンクのリンク先を取り出す。

    「解消済み仮リンク」と「を内部リンクに置き換えます」（または「…置き換える」）
    の間にある `[[リンク先|表示名]]` のリンク先を、書かれた順に重複を除いて
    返す。末尾の「を内部リンクに置き換え」がなければ途中で切れているとみなし、
    閉じていない最後のリンクは捨てる。

    Raises:
        ValueError: 対象の要約でない場合。
    """
    match = TARGET_SUMMARY.match(comment)
    if match is None:
        raise ValueError("解消済み仮リンクの要約ではありません: " + comment)
    body = comment[match.end():]
    end = body.find(SUMMARY_END)
    truncated = end < 0
    if not truncated:
        body = body[:end]

    targets: list[str] = []
    position = 0
    while True:
        start = body.find("[[", position)
        if start < 0:
            break
        close = body.find("]]", start + 2)
        if close < 0:
            break
        target = body[start + 2:close].split("|", 1)[0].strip()
        if target and target not in targets:
            targets.append(target)
        position = close + 2
    return SummaryLinks(tuple(targets), truncated)
