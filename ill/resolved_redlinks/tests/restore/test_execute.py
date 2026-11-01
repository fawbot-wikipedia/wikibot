"""最新の状態を取り直して保存する流れを検証する。"""

import unittest
from datetime import datetime, timezone

import requests
from wikiutil.mediawiki import MediaWikiAPIError

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.restore.rules import EDIT_FAILED, TargetResult
from ill.resolved_redlinks.restore.runner import (
    Candidate,
    PageResult,
    edit_summary,
    execute,
)
from ill.resolved_redlinks.tests.helpers import FakeApi

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
PLAIN = "{{仮リンク|サルド|en|sardo}}"


def _candidate(page, target="サルド", revid=90):
    """候補を作る。"""
    return Candidate(page, target, (PLAIN,), "2024-01-01T00:00:00Z", revid)


def _responses(params):
    """リンク先は削除済みの赤リンク、ページはどれも `[[サルド]]` だけの本文。"""
    if params.get("prop") == "info":
        return {"query": {"pages": [{"title": "サルド", "missing": True}]}}
    if params.get("list") == "logevents":
        return {
            "query": {
                "logevents": [
                    {
                        "type": "delete",
                        "action": "delete",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "title": params["letitle"],
                    }
                ]
            }
        }
    if params.get("prop") == "revisions":
        return {
            "curtimestamp": "2026-11-01T00:00:00Z",
            "query": {
                "pages": [
                    {
                        "title": params["titles"],
                        "ns": 0,
                        "revisions": [
                            {
                                "revid": 100,
                                "timestamp": "2026-10-01T00:00:00Z",
                                "slots": {"main": {"content": "[[サルド]]"}},
                            }
                        ],
                    }
                ]
            },
        }
    raise AssertionError(params)


class FakeEditor:
    """保存を記録し、決めた結果を返す編集の代わり。"""

    def __init__(self, failures=None, stop_after=None):
        """ページ名ごとの例外と、何回目の確認から止めるかを受け取る。"""
        self.failures = failures or {}
        self.stop_after = stop_after
        self.saved = []
        self.waits = 0
        self.stop_checks = 0

    def stop_reason(self):
        """`stop_after` 回目以降の確認で止める。"""
        self.stop_checks += 1
        if self.stop_after is not None and self.stop_checks > self.stop_after:
            return "停止用のページに書き込みがあります"
        return None

    def wait_turn(self):
        """待った回数を数える。"""
        self.waits += 1

    def save(self, page, text, summary):
        """保存を記録する。"""
        if page.title in self.failures:
            raise self.failures[page.title]
        self.saved.append((page.title, page.revid, text, summary))
        return {"result": "Success", "newrevid": 1000 + len(self.saved)}


def _run(pages, editor, max_edits=None):
    """候補のページ名から実行する。"""
    return execute(
        FakeApi(_responses),
        editor,
        {title: [_candidate(title)] for title in pages},
        TitleNormalizer(),
        lambda: NOW,
        max_edits=max_edits,
    )


class ExecuteTests(unittest.TestCase):
    """保存の流れを検証する。"""

    def test_saves_restored_text(self):
        """戻した本文を、取得した版を基準に保存し、新しい版を記録する。"""
        editor = FakeEditor()

        results = _run(["A"], editor)

        self.assertEqual(editor.saved[0][:3], ("A", 100, PLAIN))
        self.assertEqual(results[0].new_revid, 1001)
        self.assertEqual(results[0].targets, [TargetResult("サルド", 1)])
        self.assertEqual(editor.waits, 1)

    def test_stops_at_max_edits(self):
        """上限のページ数を保存したら止める。"""
        editor = FakeEditor()

        results = _run(["A", "B", "C"], editor, max_edits=2)

        self.assertEqual([saved[0] for saved in editor.saved], ["A", "B"])
        self.assertEqual(len(results), 2)

    def test_stops_on_stop_reason(self):
        """保存の直前に停止の理由があれば、保存せずに止める。"""
        editor = FakeEditor(stop_after=1)

        results = _run(["A", "B", "C"], editor)

        self.assertEqual([saved[0] for saved in editor.saved], ["A"])
        self.assertEqual(len(results), 1)

    def test_api_error_fails_only_that_page(self):
        """編集の競合などのAPIエラーは、そのページだけ失敗にして続ける。"""
        editor = FakeEditor(
            failures={"A": MediaWikiAPIError({"code": "editconflict"})}
        )

        results = _run(["A", "B"], editor)

        self.assertEqual(
            results[0].targets, [TargetResult("サルド", 0, EDIT_FAILED)]
        )
        self.assertIn("editconflict", results[0].error)
        self.assertEqual([saved[0] for saved in editor.saved], ["B"])

    def test_login_error_stops(self):
        """ログインが切れたなどのエラーでは、ほかのページも失敗するので止める。"""
        editor = FakeEditor(
            failures={"A": MediaWikiAPIError({"code": "assertuserfailed"})}
        )

        results = _run(["A", "B"], editor)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].targets[0].reason, EDIT_FAILED)
        self.assertEqual(editor.saved, [])

    def test_network_error_stops(self):
        """通信の失敗では、反映されたか分からないので止める。"""
        editor = FakeEditor(failures={"A": requests.ConnectionError("切断")})

        results = _run(["A", "B"], editor)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].targets[0].reason, EDIT_FAILED)
        self.assertEqual(editor.saved, [])


class EditSummaryTests(unittest.TestCase):
    """編集の要約を検証する。"""

    def test_lists_targets_and_cewbot_diffs(self):
        """戻したリンク先と、Cewbotの置き換えの差分を示す。"""
        result = PageResult(
            "A",
            100,
            [
                TargetResult("サルド", 1),
                TargetResult("古鳥類", 0, "link_changed"),
            ],
        )

        summary = edit_summary(
            result, [_candidate("A", "サルド", 90), _candidate("A", "古鳥類", 91)]
        )

        self.assertEqual(
            summary,
            "解消済み仮リンクのリンク先が削除されたため、仮リンクに戻します: "
            "[[サルド]]（Cewbotの置き換え: [[特別:差分/90|90]]）",
        )

    def test_shortens_many_targets(self):
        """リンク先が4件以上なら、3件まで示して残りの数を書く。"""
        titles = ["A1", "A2", "A3", "A4", "A5"]
        result = PageResult(
            "A", 100, [TargetResult(title, 1) for title in titles]
        )

        summary = edit_summary(
            result, [_candidate("A", title, 90) for title in titles]
        )

        self.assertIn("[[A1]]、[[A2]]、[[A3]] ほか2件", summary)


if __name__ == "__main__":
    unittest.main()
