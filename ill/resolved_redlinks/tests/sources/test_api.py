"""投稿記録の取得とキャッシュへの追記、リンク先の最終判定を検証する。"""

import tempfile
import unittest
from pathlib import Path

from ill.resolved_redlinks.sources import api as wiki_api
from ill.resolved_redlinks.sources.cache import Cache
from ill.resolved_redlinks.tests.helpers import FakeApi


class ContributionTests(unittest.TestCase):
    """投稿記録の取得とキャッシュへの追記を検証する。"""

    def setUp(self):
        """一時ディレクトリにキャッシュを作る。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Cache(Path(directory.name) / "cache.sqlite3")
        self.addCleanup(self.cache.close)

    @staticmethod
    def _contrib(revid, timestamp, comment, **extra):
        """投稿記録の1件を作る。"""
        return {
            "revid": revid,
            "parentid": revid - 1,
            "pageid": revid,
            "ns": 0,
            "title": "P{}".format(revid),
            "timestamp": timestamp,
            "comment": comment,
            **extra,
        }

    def test_initial_fetch_resumes_and_then_fetches_newer_edits(self):
        """初回は止まった位置から再開し、完了後は新しい編集だけを取る。"""
        target = "解消済み仮リンク[[A]]を内部リンクに置き換えます"
        pages = {
            None: (
                [
                    self._contrib(30, "2026-01-03T00:00:00Z", target),
                    self._contrib(29, "2026-01-02T00:00:00Z", "他の作業"),
                ],
                {"uccontinue": "p2", "continue": "-||"},
            ),
            "p2": (
                [
                    self._contrib(
                        28, "2026-01-01T00:00:00Z", "", commenthidden=True
                    ),
                    self._contrib(
                        27, "2016-01-01T00:00:00Z", "bot: " + target
                    ),
                ],
                None,
            ),
        }
        fail_on = {"p2"}

        def responses(params):
            """ページングの位置ごとの応答を返す。1回目はp2で失敗させる。"""
            position = params.get("uccontinue")
            if position in fail_on:
                fail_on.discard(position)
                raise ConnectionError("切断")
            items, next_continue = pages[position]
            data = {"query": {"usercontribs": items}}
            if next_continue:
                data["continue"] = next_continue
            return data

        api = FakeApi(responses)
        with self.assertRaises(ConnectionError):
            wiki_api.update_contributions(api, self.cache)
        self.assertEqual(self.cache.get_meta("contribs_continue"), "p2")

        stats = wiki_api.update_contributions(api, self.cache)

        self.assertEqual(stats["comment_hidden"], 1)
        self.assertEqual(
            [edit.revid for edit in self.cache.iter_edits()], [30, 27]
        )
        self.assertEqual(self.cache.get_meta("contribs_complete"), "1")
        self.assertEqual(
            self.cache.get_meta("contribs_latest"), "2026-01-03T00:00:00Z"
        )

        pages[None] = (
            [
                self._contrib(31, "2026-02-01T00:00:00Z", target),
                self._contrib(30, "2026-01-03T00:00:00Z", target),
            ],
            None,
        )
        stats = wiki_api.update_contributions(api, self.cache)

        self.assertEqual(api.calls[-1]["ucend"], "2026-01-03T00:00:00Z")
        self.assertEqual(stats["added"], 1)
        self.assertEqual(
            self.cache.get_meta("contribs_latest"), "2026-02-01T00:00:00Z"
        )


class TargetStatusTests(unittest.TestCase):
    """リンク先の最終判定を検証する。"""

    def test_classifies_redlink_broken_redirect_and_invalid(self):
        """不存在・長さ0・壊れた転送・無効・存在を判定する。"""

        def responses(params):
            """prop=info の応答を返す。"""
            return {
                "query": {
                    "normalized": [{"from": "a_b", "to": "A b"}],
                    "redirects": [
                        {"from": "転送", "to": "消えた先"},
                        {"from": "生きた転送", "to": "記事"},
                    ],
                    "pages": [
                        {"title": "A b", "missing": True},
                        {"title": "空", "length": 0},
                        {"title": "消えた先", "missing": True},
                        {"title": "記事", "length": 10},
                        {"title": "<", "invalid": True},
                    ],
                }
            }

        statuses = wiki_api.fetch_target_status(
            FakeApi(responses), ["a_b", "空", "転送", "生きた転送", "<"]
        )

        self.assertEqual(statuses["a_b"].state, wiki_api.REDLINK)
        self.assertEqual(statuses["空"].state, wiki_api.REDLINK)
        self.assertEqual(
            statuses["転送"],
            wiki_api.TargetStatus(wiki_api.BROKEN_REDIRECT, "消えた先"),
        )
        self.assertEqual(statuses["生きた転送"].state, wiki_api.EXISTS)
        self.assertEqual(statuses["<"].state, wiki_api.INVALID)


if __name__ == "__main__":
    unittest.main()
