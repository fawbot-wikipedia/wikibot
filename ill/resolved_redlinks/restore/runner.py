"""調査のJSONの候補を、最新の状態で確かめて仮リンクに戻す。

ドライラン（`dry_run`）では本文を作るだけで、編集はしない。編集
（`execute`）では、ページごとに最新の状態を取り直してから保存する。
"""

import difflib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import requests
from wikiutil.log import log
from wikiutil.mediawiki import MediaWikiAPIError, MediaWikiError

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.report import STILL_REDLINK
from ill.resolved_redlinks.restore.editor import Editor
from ill.resolved_redlinks.restore.rules import (
    EDIT_FAILED,
    NO_DELETION,
    PAGE_GONE,
    TARGET_EXISTS,
    Target,
    TargetResult,
    deletion_wait_reason,
    restore_page,
)
from ill.resolved_redlinks.sources import api as wiki_api

REPORT_SCHEMA_VERSION = 2
# ログインやトークンのエラー。ほかのページでも同じく失敗するので止める。
STOPPING_ERROR_CODES = {"assertuserfailed", "assertbotfailed", "badtoken"}
# 対象は記事だけ。テンプレートの編集は読み込み先のすべてに及ぶため。
TARGET_NAMESPACE = 0


@dataclass(frozen=True)
class Candidate:
    """調査のJSONの1項目のうち、戻すのに使う部分。"""

    page_title: str
    target_title: str
    replaced_ills: tuple[str, ...]
    replaced_at: str
    revision_id: int


@dataclass(frozen=True)
class PageResult:
    """1つのページの結果。編集する場合は、置き換え前後の本文を持つ。"""

    title: str
    revid: int | None
    targets: list[TargetResult]
    text: str | None = None
    restored_text: str | None = None
    new_revid: int | None = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        """本文が変わるか。"""
        return self.text is not None and self.restored_text != self.text


@dataclass
class DryRunSummary:
    """ドライラン全体の件数。"""

    pages: int = 0
    pages_to_edit: int = 0
    targets: Counter[str] = field(default_factory=Counter)


def load_candidates(report: Mapping[str, Any]) -> dict[str, list[Candidate]]:
    """調査のJSONから、記事の赤リンクの項目をページ名ごとにまとめる。

    Raises:
        ValueError: JSONの版が `replaced_ills` を含まない場合。
    """
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError(
            "調査のJSONの版が {} ではありません（{}）。調査を実行し直して"
            "ください".format(
                REPORT_SCHEMA_VERSION, report.get("schema_version")
            )
        )
    pages: dict[str, list[Candidate]] = defaultdict(list)
    for item in report["items"]:
        if (
            item["category"] != STILL_REDLINK
            or item["namespace"]["id"] != TARGET_NAMESPACE
        ):
            continue
        pages[item["page"]["title"]].append(
            Candidate(
                page_title=item["page"]["title"],
                target_title=item["target"]["title"],
                replaced_ills=tuple(item["replaced_ills"]),
                replaced_at=item["replacement"]["timestamp"],
                revision_id=item["replacement"]["revision_id"],
            )
        )
    return dict(pages)


def _parse_timestamp(value: str) -> datetime:
    """APIの時刻（ISO 8601、UTC）を読む。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_targets(
    api: wiki_api.JaWikipediaApi, titles: Iterable[str]
) -> dict[str, tuple[wiki_api.TargetStatus, wiki_api.LogEntry | None]]:
    """リンク先ごとに、現在の状態と、赤リンクなら最後の削除・移動の記録を返す。"""
    titles = sorted(set(titles))
    statuses = wiki_api.fetch_target_status(api, titles)
    result = {}
    for index, title in enumerate(titles, start=1):
        status = statuses[title]
        entry = (
            wiki_api.fetch_last_log_entry(api, title)
            if status.state == wiki_api.REDLINK
            else None
        )
        result[title] = (status, entry)
        if index % 100 == 0 or index == len(titles):
            log("削除の記録の確認 {} / {}件".format(index, len(titles)))
    return result


def candidate_reason(
    candidate: Candidate,
    check: tuple[wiki_api.TargetStatus, wiki_api.LogEntry | None],
    now: datetime,
) -> str | None:
    """リンク先の状態と記録から、戻さない理由を返す。戻してよければ None。"""
    status, entry = check
    if status.state != wiki_api.REDLINK:
        return TARGET_EXISTS
    if entry is None or entry.action != "delete/delete":
        return NO_DELETION
    return deletion_wait_reason(
        _parse_timestamp(entry.timestamp),
        _parse_timestamp(candidate.replaced_at),
        now,
    )


def plan_page(
    candidates: Sequence[Candidate],
    page: wiki_api.EditablePage | None,
    checks: Mapping[
        str, tuple[wiki_api.TargetStatus, wiki_api.LogEntry | None]
    ],
    normalizer: TitleNormalizer,
    now: datetime,
) -> PageResult:
    """1つのページについて、戻す本文と、リンク先ごとの結果を作る。"""
    title = candidates[0].page_title
    if page is None:
        return PageResult(
            title,
            None,
            [TargetResult(c.target_title, 0, PAGE_GONE) for c in candidates],
        )
    skipped = []
    targets = []
    for candidate in candidates:
        reason = candidate_reason(
            candidate, checks[candidate.target_title], now
        )
        if reason is None:
            targets.append(
                Target(candidate.target_title, candidate.replaced_ills)
            )
        else:
            skipped.append(TargetResult(candidate.target_title, 0, reason))
    restored_text, results = restore_page(page.text, targets, normalizer)
    return PageResult(
        title, page.revid, skipped + results, page.text, restored_text
    )


def dry_run(
    api: wiki_api.JaWikipediaApi,
    pages: Mapping[str, Sequence[Candidate]],
    normalizer: TitleNormalizer,
    now: datetime,
) -> list[PageResult]:
    """すべての候補のページについて、編集せずに結果を作る。"""
    titles = sorted(pages)
    target_titles = {
        candidate.target_title
        for candidates in pages.values()
        for candidate in candidates
    }
    log("リンク先の確認 開始（{}件）".format(len(target_titles)))
    checks = check_targets(api, target_titles)
    log("最新版の取得 開始（{}ページ）".format(len(titles)))
    latest = wiki_api.fetch_pages_for_edit(api, titles)
    return [
        plan_page(pages[title], latest.get(title), checks, normalizer, now)
        for title in titles
    ]


def summarize(results: Iterable[PageResult]) -> DryRunSummary:
    """ページ数・編集するページ数・リンク先ごとの結果の件数を数える。"""
    summary = DryRunSummary()
    for result in results:
        summary.pages += 1
        summary.pages_to_edit += result.changed
        for target in result.targets:
            summary.targets[target.reason or "restored"] += 1
    return summary


def edit_summary(
    result: PageResult, candidates: Sequence[Candidate]
) -> str:
    """編集の要約を作る。戻したリンク先と、Cewbotの置き換えの差分を示す。"""
    restored = [target.title for target in result.targets if target.restored]
    names = "、".join("[[{}]]".format(title) for title in restored[:3])
    if len(restored) > 3:
        names += " ほか{}件".format(len(restored) - 3)
    revids = sorted(
        {
            candidate.revision_id
            for candidate in candidates
            if candidate.target_title in restored
        }
    )
    diffs = "、".join(
        "[[特別:差分/{0}|{0}]]".format(revid) for revid in revids[:3]
    )
    if len(revids) > 3:
        diffs += " ほか"
    return (
        "解消済み仮リンクのリンク先が削除されたため、仮リンクに戻します: "
        "{}（Cewbotの置き換え: {}）".format(names, diffs)
    )


def _failed(result: PageResult, reason: str, error: str) -> PageResult:
    """戻すはずだったリンク先を、理由付きの見送りにした結果を返す。"""
    return replace(
        result,
        targets=[
            TargetResult(target.title, 0, reason) if target.restored
            else target
            for target in result.targets
        ],
        restored_text=result.text,
        error=error,
    )


def execute(
    api: wiki_api.JaWikipediaApi,
    editor: Editor,
    pages: Mapping[str, Sequence[Candidate]],
    normalizer: TitleNormalizer,
    now: Callable[[], datetime],
    *,
    max_edits: int | None,
) -> list[PageResult]:
    """ページごとに最新の状態を取り直して判定し、戻せるものを保存する。

    ページ名順に処理し、`max_edits` ページを保存したら止める。保存の
    直前に、会話ページの新着や停止用のページへの書き込みがあれば止める。
    APIのエラー（編集の競合など）はそのページだけ失敗にして続ける。
    ログインやトークンのエラーと通信の失敗では止める（後者は編集が
    反映されたか分からないため）。
    """
    results: list[PageResult] = []
    edits = 0
    for title in sorted(pages):
        if max_edits is not None and edits >= max_edits:
            break
        candidates = pages[title]
        checks = check_targets(
            api, [candidate.target_title for candidate in candidates]
        )
        page = wiki_api.fetch_pages_for_edit(api, [title]).get(title)
        result = plan_page(candidates, page, checks, normalizer, now())
        if not result.changed:
            results.append(result)
            continue
        editor.wait_turn()
        reason = editor.stop_reason()
        if reason is not None:
            log("編集を止めます: " + reason)
            break
        try:
            edit = editor.save(
                page, result.restored_text, edit_summary(result, candidates)
            )
        except MediaWikiAPIError as error:
            results.append(_failed(result, EDIT_FAILED, str(error)))
            if error.code in STOPPING_ERROR_CODES:
                log("「{}」の保存で止めます: {}".format(title, error))
                break
            log("「{}」を保存できません: {}".format(title, error))
            continue
        except (MediaWikiError, requests.RequestException) as error:
            log("「{}」の保存で止めます: {}".format(title, error))
            results.append(_failed(result, EDIT_FAILED, str(error)))
            break
        edits += 1
        results.append(replace(result, new_revid=edit.get("newrevid")))
        log("「{}」を保存しました（版 {}）".format(title, edit.get("newrevid")))
    return results


def render_results(
    results: Sequence[PageResult],
    *,
    generated_at: datetime,
    report_path: str,
    dry_run: bool = True,
) -> str:
    """結果をJSONにする。"""
    summary = summarize(results)
    return json.dumps(
        {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "report": report_path,
            "dry_run": dry_run,
            "counts": {
                "pages": summary.pages,
                "pages_to_edit": summary.pages_to_edit,
                "targets": dict(sorted(summary.targets.items())),
            },
            "pages": [
                {
                    "title": result.title,
                    "revision_id": result.revid,
                    "edit": result.changed,
                    "new_revision_id": result.new_revid,
                    "error": result.error,
                    "targets": [
                        {
                            "title": target.title,
                            "restored": target.restored,
                            "reason": target.reason,
                        }
                        for target in result.targets
                    ],
                }
                for result in results
            ],
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def render_diff(results: Iterable[PageResult]) -> str:
    """編集するページの、置き換え前後の差分（変わった行だけ）を並べる。"""
    blocks = []
    for result in results:
        if not result.changed:
            continue
        lines = [
            line
            for line in difflib.unified_diff(
                result.text.splitlines(),
                result.restored_text.splitlines(),
                lineterm="",
                n=0,
            )
            if line.startswith(("-", "+"))
            and not line.startswith(("---", "+++"))
        ]
        blocks.append(
            "=== {}（版 {}）\n{}\n".format(
                result.title, result.revid, "\n".join(lines)
            )
        )
    return "\n".join(blocks)
