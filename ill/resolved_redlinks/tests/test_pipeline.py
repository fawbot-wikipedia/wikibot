"""候補の絞り込みと最新版での分類を検証する。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.pipeline import (
    LinkRecord,
    add_recent_changes,
    classify_latest,
    collect_records,
    log_entry_for,
    select_edits,
)
from ill.resolved_redlinks.sources import api as wiki_api
from ill.resolved_redlinks.sources.cache import Cache
from ill.resolved_redlinks.sources.dump import REDLINK_CANDIDATE
from ill.resolved_redlinks.tests.helpers import FakeApi, make_edit


class FlowTests(unittest.TestCase):
    """候補の絞り込みと最新版での分類を検証する。"""

    def setUp(self):
        """正規化器とキャッシュを用意する。"""
        self.normalizer = TitleNormalizer()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Cache(Path(directory.name) / "cache.sqlite3")
        self.addCleanup(self.cache.close)

    def test_selects_edits_by_namespace_and_jst_date(self):
        """名前空間と日本時間の日付で編集を絞る。"""
        edits = [
            make_edit(1, "A", "2022-08-12T20:30:48Z", "c"),
            make_edit(2, "B", "2022-08-12T10:00:00Z", "c"),
            make_edit(
                3, "Template:C", "2022-08-13T01:00:00Z", "c", namespace=10
            ),
            make_edit(4, "利用者:D", "2022-08-13T01:00:00Z", "c", namespace=2),
        ]

        selected = select_edits(
            edits, (0, 10), date(2022, 8, 13), date(2022, 8, 13)
        )

        self.assertEqual([edit.revid for edit in selected], [1, 3])

    def test_collects_records_with_newest_edit_and_diff_fallback(self):
        """同じページ・リンク先は最新の編集にまとめ、切れた要約は差分で補う。"""
        body = (
            '<td class="diff-deletedline"><div>{{仮リンク|X|en|X}}'
            "{{仮リンク|Y|en|Y}}</div></td>"
            '<td class="diff-addedline"><div>[[X]][[Y]]</div></td>'
        )
        api = FakeApi(lambda params: {"compare": {"body": body}})
        edits = [
            make_edit(3, "P", "2024-01-01T00:00:00Z",
                  "解消済み仮リンク[[A]]を内部リンクに置き換えます", pageid=1),
            make_edit(2, "P", "2023-01-01T00:00:00Z",
                  "解消済み仮リンク[[A]]、[[B]]を内部リンクに置き換えます",
                  pageid=1),
            make_edit(1, "Q", "2016-01-01T00:00:00Z",
                  "bot: 解消済み仮リンク[[X]]、[[Y|y...", pageid=2),
        ]

        records = collect_records(edits, self.cache, api, self.normalizer)
        records_again = collect_records(
            edits, self.cache, api, self.normalizer
        )

        found = {(r.edit.pageid, r.target.name): r.edit.revid for r in records}
        self.assertEqual(
            found,
            {(1, "A"): 3, (1, "B"): 2, (2, "X"): 1, (2, "Y"): 1},
        )
        self.assertEqual(len(records_again), 4)
        self.assertEqual(len(api.calls), 1)

    def test_uses_summary_links_when_revision_is_hidden(self):
        """版の本文が見られない場合は、要約の閉じたリンクを使い、問い合わせ直さない。"""

        def hidden(params):
            """本文が見られない版のエラーを返す。"""
            raise wiki_api.ApiError("missingcontent", {})

        api = FakeApi(hidden)
        edits = [
            make_edit(1, "Q", "2016-01-01T00:00:00Z",
                  "bot: 解消済み仮リンク[[X]]、[[Y|y...", pageid=2),
        ]

        records = collect_records(edits, self.cache, api, self.normalizer)
        collect_records(edits, self.cache, api, self.normalizer)

        self.assertEqual([r.target.name for r in records], ["X"])
        self.assertEqual(len(api.calls), 1)

    def test_adds_targets_changed_after_dump(self):
        """ダンプ作成後に削除・移動されたリンク先と転送先を候補に加える。"""
        key = self.normalizer.key
        candidates = {key("既に候補"): REDLINK_CANDIDATE}
        entries = [
            wiki_api.LogEntry("delete/delete", "t", "", "消えた記事"),
            wiki_api.LogEntry("move/move", "t", "", "移動元", "移動先"),
            wiki_api.LogEntry("delete/delete", "t", "", "転送先"),
        ]

        added = add_recent_changes(
            candidates,
            {key("既に候補"), key("消えた記事"), key("移動元"), key("転送"),
             key("無関係")},
            {key("転送"): key("転送先")},
            entries,
            self.normalizer,
        )

        self.assertEqual(added, 3)
        self.assertNotIn(key("無関係"), candidates)

    def test_classifies_latest_pages(self):
        """最新版にリンクが残るもの・仮リンクに戻されたもの・消えたものを分ける。"""
        key = self.normalizer.key
        edit = make_edit(5, "P", "2024-01-01T00:00:00Z", "c", pageid=1)
        gone = make_edit(6, "Gone", "2024-01-01T00:00:00Z", "c", pageid=2)
        records = [
            LinkRecord(edit, key("残る")),
            LinkRecord(edit, key("戻る")),
            LinkRecord(edit, key("消える")),
            LinkRecord(edit, key("存在する")),
            LinkRecord(gone, key("残る")),
        ]
        statuses = {
            key(name): wiki_api.TargetStatus(wiki_api.REDLINK)
            for name in ("残る", "戻る", "消える")
        }
        pages = {
            1: wiki_api.LatestPage(
                1, 0, "P（改名後）", 9, "[[残る]] {{仮リンク|戻る|en|R}}"
            )
        }

        remaining, counts = classify_latest(
            records, statuses, pages, (0, 10), self.normalizer
        )

        self.assertEqual(
            [(r.target.name, p.title) for r, p in remaining],
            [("残る", "P（改名後）")],
        )
        self.assertEqual(
            counts,
            {"restored_ill": 1, "link_removed": 1, "page_gone": 1},
        )

    def test_log_entry_uses_recent_then_cache_then_api(self):
        """ダンプ作成後の記録、新しいキャッシュ、APIの順に使う。"""
        recent_entry = wiki_api.LogEntry(
            "delete/delete", "2026-10-10T00:00:00Z", "最近", "最近消えた"
        )
        recent = {self.normalizer.key("最近消えた"): recent_entry}
        api_entry = {
            "type": "delete",
            "action": "delete",
            "timestamp": "2025-12-21T12:08:22Z",
            "comment": "転送先がないリダイレクト",
            "title": "古い",
        }
        api = FakeApi(lambda params: {"query": {"logevents": [api_entry]}})
        since = "2026-10-01T00:00:00Z"

        self.assertEqual(
            log_entry_for(
                "最近消えた", recent, since, self.cache, api, self.normalizer
            ),
            recent_entry,
        )
        first = log_entry_for(
            "古い", recent, since, self.cache, api, self.normalizer
        )
        second = log_entry_for(
            "古い", recent, since, self.cache, api, self.normalizer
        )

        self.assertEqual(first, second)
        self.assertEqual(first.comment, "転送先がないリダイレクト")
        self.assertEqual(len(api.calls), 1)

        with self.cache.transaction():
            self.cache.set_log_entry("古い", "2026-09-01T00:00:00Z", None)
        log_entry_for("古い", recent, since, self.cache, api, self.normalizer)
        self.assertEqual(len(api.calls), 2)


if __name__ == "__main__":
    unittest.main()
