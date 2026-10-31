"""処理の流れ。投稿記録の取得から、リンク先の判定、最新版での分類、JSONの保存までを順に行う。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from wikiutil.files import atomic_write
from wikiutil.log import log
from wikiutil.mediawiki import MediaWikiError

from ill.resolved_redlinks.parsing.summary import parse_summary_links
from ill.resolved_redlinks.parsing.titles import TitleKey, TitleNormalizer
from ill.resolved_redlinks.parsing.wikitext import (
    added_link_targets,
    ill_templates_for,
    ill_wording,
    page_link_state,
    parse_compare_body,
    removed_ill_templates,
)
from ill.resolved_redlinks.report import (
    BROKEN_REDIRECT,
    JST,
    STILL_REDLINK,
    ReportRow,
    format_log_note,
    render_json_report,
)
from ill.resolved_redlinks.sources import api as wiki_api
from ill.resolved_redlinks.sources.cache import Cache, Edit
from ill.resolved_redlinks.sources.dump import ensure_dump_files, judge_targets

OUTPUT_FILE_NAME = "resolved_ill_redlinks.json"


@dataclass(frozen=True)
class LinkRecord:
    """Cewbotが置き換えたリンク1件（ページとリンク先の組）。"""

    edit: Edit
    target: TitleKey


def _jst_date(timestamp: str) -> date:
    """APIの時刻を日本時間の日付にする。"""
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return moment.astimezone(JST).date()


def select_edits(
    edits: Iterable[Edit],
    namespaces: Sequence[int],
    since: date | None,
    until: date | None,
) -> list[Edit]:
    """名前空間と期間（日本時間）で編集を絞る。順序は保つ。"""
    selected = []
    for edit in edits:
        if edit.namespace not in namespaces:
            continue
        day = _jst_date(edit.timestamp)
        if since is not None and day < since:
            continue
        if until is not None and day > until:
            continue
        selected.append(edit)
    return selected


def edit_targets(
    edit: Edit,
    cache: Cache,
    api: wiki_api.JaWikipediaApi,
    normalizer: TitleNormalizer,
) -> list[TitleKey]:
    """編集で置き換えたリンク先のキーを返す。

    要約が切れている場合は、その編集の差分から新しく増えた内部リンクを
    拾い、revision ID ごとにキャッシュする。
    """
    summary = parse_summary_links(edit.comment)
    if not summary.truncated:
        return sorted(normalizer.keys(summary.targets))
    cached = cache.get_diff_links(edit.revid)
    if cached is None:
        body = wiki_api.fetch_compare_body(api, edit.revid)
        if body is None:
            log(
                "差分を取得できないため要約のリンクを使います: 版 {}".format(
                    edit.revid
                )
            )
            deleted, added = [], []
        else:
            deleted, added = parse_compare_body(body)
        cache.set_diff_ills(
            edit.revid, removed_ill_templates(deleted, added)
        )
        new_links, resolved_ills = added_link_targets(
            deleted, added, normalizer
        )
        if body is not None and (
            not new_links or len(new_links) > resolved_ills
        ):
            log(
                "差分の確認が必要: 版 {} 増えた内部リンク {}件、"
                "解消された仮リンク {}件".format(
                    edit.revid, len(new_links), resolved_ills
                )
            )
        cached = [normalizer.display(key) for key in new_links]
        cache.set_diff_links(edit.revid, cached)
    keys = normalizer.keys(cached)
    if not keys:
        keys = normalizer.keys(summary.targets)
    return sorted(keys)


def collect_records(
    edits: Sequence[Edit],
    cache: Cache,
    api: wiki_api.JaWikipediaApi,
    normalizer: TitleNormalizer,
) -> list[LinkRecord]:
    """編集ごとのリンク先を集め、同じページ・リンク先は最新の編集にまとめる。

    `edits` は新しい順に並んでいるものとする。
    """
    records: dict[tuple[int, TitleKey], LinkRecord] = {}
    truncated = 0
    for edit in edits:
        if parse_summary_links(edit.comment).truncated:
            truncated += 1
        for target in edit_targets(edit, cache, api, normalizer):
            records.setdefault((edit.pageid, target), LinkRecord(edit, target))
    log(
        "置き換えたリンク {}件を集計（編集 {}件、うち要約が切れたもの {}件）"
        .format(len(records), len(edits), truncated)
    )
    return list(records.values())


def _logevents_since(dump_date: str) -> str:
    """ダンプの実行日の0時（UTC）をAPIの時刻の形で返す。"""
    return datetime.strptime(dump_date, "%Y%m%d").strftime(
        "%Y-%m-%dT00:00:00Z"
    )


def add_recent_changes(
    candidates: dict[TitleKey, str],
    targets: set[TitleKey],
    redirect_targets: dict[TitleKey, TitleKey],
    entries: Sequence[wiki_api.LogEntry],
    normalizer: TitleNormalizer,
) -> int:
    """ダンプ作成後に削除・移動されたリンク先と転送先を候補に加える。

    Returns:
        新しく加えた候補の件数。
    """
    affected = normalizer.keys(entry.title for entry in entries)
    added = 0
    for key in targets:
        if key in candidates:
            continue
        if key in affected or redirect_targets.get(key) in affected:
            candidates[key] = "recent_change"
            added += 1
    return added


def latest_log_entries(
    entries: Sequence[wiki_api.LogEntry], normalizer: TitleNormalizer
) -> dict[TitleKey, wiki_api.LogEntry]:
    """名前ごとに、最も新しい削除・移動の記録を返す。"""
    latest: dict[TitleKey, wiki_api.LogEntry] = {}
    for entry in entries:
        key = normalizer.key(entry.title)
        if key is None:
            continue
        if key not in latest or entry.timestamp > latest[key].timestamp:
            latest[key] = entry
    return latest


def log_entry_for(
    title: str,
    recent: dict[TitleKey, wiki_api.LogEntry],
    since: str,
    cache: Cache,
    api: wiki_api.JaWikipediaApi,
    normalizer: TitleNormalizer,
) -> wiki_api.LogEntry | None:
    """名前の最後の削除・移動の記録を返す。

    ダンプ作成後の記録があればそれを使う。なければ、ダンプの実行日以降に
    確かめたキャッシュを使い、それもなければAPIに問い合わせて保存する。
    """
    key = normalizer.key(title)
    if key is not None and key in recent:
        return recent[key]
    cached = cache.get_log_entry(title)
    if cached is not None and cached[0] >= since:
        return wiki_api.LogEntry.from_dict(cached[1]) if cached[1] else None
    entry = wiki_api.fetch_last_log_entry(api, title)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache.set_log_entry(title, checked_at, entry.as_dict() if entry else None)
    return entry


def classify_latest(
    records: Sequence[LinkRecord],
    statuses: dict[TitleKey, wiki_api.TargetStatus],
    pages: dict[int, wiki_api.LatestPage],
    namespaces: Sequence[int],
    normalizer: TitleNormalizer,
) -> tuple[list[tuple[LinkRecord, wiki_api.LatestPage]], dict[str, int]]:
    """最新版に内部リンクが残っているものを選び、残りを件数で数える。"""
    remaining = []
    counts = {
        "restored_ill": 0,
        "link_removed": 0,
        "page_gone": 0,
    }
    states: dict[int, tuple[set[TitleKey], set[TitleKey]]] = {}
    for record in records:
        if record.target not in statuses:
            continue
        page = pages.get(record.edit.pageid)
        if page is None or page.namespace not in namespaces:
            counts["page_gone"] += 1
            continue
        if page.pageid not in states:
            states[page.pageid] = page_link_state(page.text, normalizer)
        links, ills = states[page.pageid]
        if record.target in links:
            remaining.append((record, page))
        elif record.target in ills:
            counts["restored_ill"] += 1
        else:
            counts["link_removed"] += 1
    return remaining, counts


@dataclass(frozen=True)
class RunResult:
    """1回の実行の結果。"""

    output_path: Path
    rows: list[ReportRow]
    counts: dict[str, int]


def run(
    namespaces: Sequence[int],
    since: date | None,
    until: date | None,
    *,
    refresh: bool,
    dump_date: str | None,
    output_dir: Path,
    cache_file: Path,
) -> RunResult:
    """一覧を作ってJSONで保存し、結果を返す。"""
    api = wiki_api.JaWikipediaApi()
    normalizer = api.siteinfo_normalizer()

    with Cache(cache_file) as cache:
        if refresh:
            cache.clear_edits()
        wiki_api.update_contributions(api, cache)
        edits = select_edits(
            cache.iter_edits(), namespaces, since, until
        )
        log("対象の編集 {}件".format(len(edits)))
        records = collect_records(edits, cache, api, normalizer)
        targets = {record.target for record in records}

        files = ensure_dump_files(dump_date)
        judgement = judge_targets(files, targets, normalizer)
        candidates = dict(judgement.candidates)
        logs_since = _logevents_since(files.dump_date)
        entries = wiki_api.fetch_recent_log_entries(api, logs_since)
        added = add_recent_changes(
            candidates,
            targets,
            judgement.redirect_targets,
            entries,
            normalizer,
        )
        log(
            "ダンプ作成後の削除・移動の記録 {}件から候補を {}件追加".format(
                len(entries), added
            )
        )

        candidate_titles = {
            normalizer.display(key): key for key in candidates
        }
        log("リンク先の最終判定 開始（{}件）".format(len(candidate_titles)))
        title_statuses = wiki_api.fetch_target_status(
            api, sorted(candidate_titles)
        )
        statuses: dict[TitleKey, wiki_api.TargetStatus] = {}
        invalid = []
        for title, status in title_statuses.items():
            if status.state in (wiki_api.REDLINK, wiki_api.BROKEN_REDIRECT):
                statuses[candidate_titles[title]] = status
            elif status.state == wiki_api.INVALID:
                invalid.append(title)
        if invalid:
            log("無効なページ名 {}件を除外: {}".format(
                len(invalid), "、".join(invalid[:20])
            ))
        log(
            "最終判定 完了: 赤リンク {}件、壊れた転送 {}件".format(
                sum(s.state == wiki_api.REDLINK for s in statuses.values()),
                sum(
                    s.state == wiki_api.BROKEN_REDIRECT
                    for s in statuses.values()
                ),
            )
        )

        pageids = {
            record.edit.pageid
            for record in records
            if record.target in statuses
        }
        log("ページの最新版の取得 開始（{}件）".format(len(pageids)))
        pages = wiki_api.fetch_latest_pages(api, pageids)
        remaining, counts = classify_latest(
            records, statuses, pages, namespaces, normalizer
        )
        if counts["page_gone"]:
            log(
                "削除・対象外の名前空間への移動で除外したページのリンク {}件"
                .format(counts["page_gone"])
            )

        recent = latest_log_entries(entries, normalizer)
        log_titles = set()
        for record, _ in remaining:
            status = statuses[record.target]
            log_titles.add(
                status.redirect_target or normalizer.display(record.target)
            )
        log("削除・移動の記録の確認 開始（{}件）".format(len(log_titles)))
        notes = {}
        for title in sorted(log_titles):
            entry = log_entry_for(
                title, recent, logs_since, cache, api, normalizer
            )
            notes[title] = entry

        source_revids = list(dict.fromkeys(
            record.edit.revid for record, _ in remaining
        ))
        uncached_source_revids = [
            revid for revid in source_revids
            if cache.get_diff_ills(revid) is None
        ]
        log(
            "置換前の仮リンク取得 開始（対象版 {}件、未キャッシュ {}件）"
            .format(len(source_revids), len(uncached_source_revids))
        )
        source_templates_by_revid = {}
        source_errors_by_revid = {}
        for index, revid in enumerate(source_revids, start=1):
            templates = cache.get_diff_ills(revid)
            if templates is None:
                try:
                    body = wiki_api.fetch_compare_body(api, revid)
                except (MediaWikiError, requests.RequestException) as error:
                    source_templates_by_revid[revid] = []
                    source_errors_by_revid[revid] = (
                        "版 {} の差分取得に失敗: {}".format(revid, error)
                    )
                    log(source_errors_by_revid[revid])
                    continue
                if body is None:
                    templates = []
                    source_errors_by_revid[revid] = (
                        "版 {} の差分本文が利用できません".format(revid)
                    )
                    log(source_errors_by_revid[revid])
                else:
                    deleted, added = parse_compare_body(body)
                    templates = removed_ill_templates(deleted, added)
                cache.set_diff_ills(revid, templates)
            source_templates_by_revid[revid] = templates
            if index % 100 == 0 or index == len(source_revids):
                log(
                    "置換前の仮リンク取得 {} / {}版".format(
                        index, len(source_revids)
                    )
                )

        rows = []
        for record, page in remaining:
            status = statuses[record.target]
            category = (
                STILL_REDLINK
                if status.state == wiki_api.REDLINK
                else BROKEN_REDIRECT
            )
            log_title = status.redirect_target or normalizer.display(
                record.target
            )
            entry = notes.get(log_title)
            original_ill_error = source_errors_by_revid.get(
                record.edit.revid
            )
            replaced_ills: list[str] = []
            if original_ill_error is None:
                replaced_ills = ill_templates_for(
                    source_templates_by_revid[record.edit.revid],
                    normalizer.display(record.target),
                    normalizer,
                )
                if not replaced_ills:
                    original_ill_error = (
                        "版 {} の差分にリンク先「{}」の仮リンクが"
                        "見つかりません".format(
                            record.edit.revid,
                            normalizer.display(record.target),
                        )
                    )
                    log(original_ill_error)
            rows.append(
                ReportRow(
                    category=category,
                    namespace=page.namespace,
                    page_title=page.title,
                    target_title=normalizer.display(record.target),
                    revid=record.edit.revid,
                    timestamp=record.edit.timestamp,
                    log_note=format_log_note(
                        entry.action if entry else None,
                        entry.timestamp if entry else None,
                        entry.comment if entry else None,
                        entry.target if entry else None,
                        status.redirect_target,
                    ),
                    original_ill=(
                        replaced_ills[0] if replaced_ills else None
                    ),
                    wording=(
                        ill_wording(replaced_ills[0])
                        if replaced_ills
                        else None
                    ),
                    original_ill_error=original_ill_error,
                    replaced_ills=tuple(replaced_ills),
                )
            )

    # 現在の状態を確かめ終えた時刻を取得日時にする。
    generated_at = datetime.now(timezone.utc)
    report_json = render_json_report(
        rows,
        generated_at=generated_at,
        dump_date=files.dump_date,
        namespaces=namespaces,
        namespace_names=normalizer.namespaces,
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
        reference_counts=counts,
    )
    output_directory = output_dir / generated_at.astimezone(
        JST
    ).strftime("%Y%m%d_%H%M%S")
    output_path = output_directory / OUTPUT_FILE_NAME
    with atomic_write(output_path) as stream:
        stream.write(report_json)
    return RunResult(output_path, rows, counts)
