"""Cewbotが入れた内部リンクを、置換前の仮リンクに戻す規則。通信しない。"""

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from wikiutil.ill import parse_ill_template

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.parsing.wikitext import (
    internal_link_spans,
    page_link_state,
)

# 削除から戻すまでに待つ日数。削除の直後は復帰や作り直しがありうるため。
WAIT_DAYS = 30

# 見送りの理由。
TARGET_EXISTS = "target_exists"
DELETED_RECENTLY = "deleted_recently"
DELETED_BEFORE_REPLACEMENT = "deleted_before_replacement"
NO_ORIGINAL_ILL = "no_original_ill"
NO_LINK = "no_link"
LINK_CHANGED = "link_changed"
EXTRA_LINKS = "extra_links"
NOT_RESTORED = "not_restored"
EDIT_FAILED = "edit_failed"


@dataclass(frozen=True)
class Target:
    """1つのページで戻す対象のリンク先と、Cewbotがその版で消した仮リンク。"""

    title: str
    replaced_ills: Sequence[str]


@dataclass(frozen=True)
class TargetResult:
    """リンク先ごとの結果。戻した数と、見送った場合はその理由を持つ。"""

    title: str
    restored: int
    reason: str | None = None


def cewbot_link(template: str) -> str:
    """仮リンクを、Cewbotが置き換えるときと同じ形の内部リンクにする。

    表示文言（`label`）があって記事名と違えば `[[記事名|表示文言]]`、
    そうでなければ `[[記事名]]` になる。記事名は仮リンクに書かれた表記のまま。
    """
    parameters = parse_ill_template(template)
    japanese_title = parameters.get("ja_name", "").strip()
    label = parameters.get("label", "").strip()
    if label and label != japanese_title:
        return "[[{}|{}]]".format(japanese_title, label)
    return "[[{}]]".format(japanese_title)


def deletion_wait_reason(
    deleted_at: datetime,
    replaced_at: datetime,
    now: datetime,
    wait_days: int = WAIT_DAYS,
) -> str | None:
    """削除の時刻から、まだ戻さない理由を返す。戻してよければ None。"""
    if deleted_at <= replaced_at:
        return DELETED_BEFORE_REPLACEMENT
    if now - deleted_at < timedelta(days=wait_days):
        return DELETED_RECENTLY
    return None


def restore_target(
    text: str, target: Target, normalizer: TitleNormalizer
) -> tuple[str, TargetResult]:
    """本文の中の、1つのリンク先へのリンクを仮リンクに戻す。

    リンク先へのリンクを出現順に見て、Cewbotが入れた形のうちまだ使って
    いないものと1つずつ突き合わせる。すべて突き合わせられたときだけ戻し、
    1つでも合わなければ本文を変えずに理由を返す。

    Returns:
        戻したあとの本文（見送ったときは元の本文）と、結果。
    """
    if not target.replaced_ills:
        return text, TargetResult(target.title, 0, NO_ORIGINAL_ILL)
    templates_by_link: dict[str, deque[str]] = defaultdict(deque)
    for template in target.replaced_ills:
        templates_by_link[cewbot_link(template)].append(template)

    target_key = normalizer.key(target.title)
    replacements = []
    for link_target, start, end in internal_link_spans(text):
        if normalizer.key(link_target) != target_key:
            continue
        link = text[start:end] if end is not None else None
        if link not in templates_by_link:
            return text, TargetResult(target.title, 0, LINK_CHANGED)
        if not templates_by_link[link]:
            return text, TargetResult(target.title, 0, EXTRA_LINKS)
        replacements.append((start, end, templates_by_link[link].popleft()))
    if not replacements:
        return text, TargetResult(target.title, 0, NO_LINK)

    restored = text
    for start, end, template in reversed(replacements):
        restored = restored[:start] + template + restored[end:]
    links, ills = page_link_state(restored, normalizer)
    if target_key in links or target_key not in ills:
        return text, TargetResult(target.title, 0, NOT_RESTORED)
    return restored, TargetResult(target.title, len(replacements))


def restore_page(
    text: str, targets: Sequence[Target], normalizer: TitleNormalizer
) -> tuple[str, list[TargetResult]]:
    """1つのページの本文で、リンク先ごとに仮リンクに戻す。

    戻せなかったリンク先があっても、ほかのリンク先は戻す。
    """
    results = []
    for target in targets:
        text, result = restore_target(text, target, normalizer)
        results.append(result)
    return text, results
