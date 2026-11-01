"""調査のJSONの候補を最新の状態で確かめ、戻す本文を作ることを検証する。"""

import json
import unittest
from datetime import datetime, timezone

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.restore.rules import (
    DELETED_RECENTLY,
    LINK_CHANGED,
    NO_DELETION,
    PAGE_GONE,
    TARGET_EXISTS,
    TargetResult,
)
from ill.resolved_redlinks.restore.runner import (
    Candidate,
    candidate_reason,
    dry_run,
    load_candidates,
    plan_page,
    render_diff,
    render_results,
    summarize,
)
from ill.resolved_redlinks.sources import api as wiki_api
from ill.resolved_redlinks.tests.helpers import FakeApi

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
REPLACED_AT = "2024-01-01T00:00:00Z"
PLAIN = "{{仮リンク|サルド|en|sardo}}"
LABELED = "{{仮リンク|古鳥類|en|Archaeornithes|label=古鳥亜綱}}"


def _item(page, target, replaced_ills, category="still_redlink", ns=0):
    """調査のJSONの項目を作る。"""
    return {
        "category": category,
        "namespace": {"id": ns, "name": "記事"},
        "page": {"title": page},
        "target": {"title": target},
        "replacement": {"timestamp": REPLACED_AT, "revision_id": 90},
        "replaced_ills": list(replaced_ills),
    }


def _redlink(deleted_at="2026-01-01T00:00:00Z", action="delete/delete"):
    """赤リンクの状態と、最後の記録の組を作る。"""
    return (
        wiki_api.TargetStatus(wiki_api.REDLINK),
        wiki_api.LogEntry(action, deleted_at, "", ""),
    )


def _page(title, text):
    """編集に使う最新版を作る。"""
    return wiki_api.EditablePage(
        title, 0, 100, "2026-10-01T00:00:00Z", text, "2026-11-01T00:00:00Z"
    )


class LoadCandidatesTests(unittest.TestCase):
    """調査のJSONから候補を読むことを検証する。"""

    def test_groups_article_redlinks_by_page(self):
        """記事の赤リンクだけを、ページ名ごとにまとめる。"""
        report = {
            "schema_version": 2,
            "items": [
                _item("A", "サルド", [PLAIN]),
                _item("A", "古鳥類", [LABELED]),
                _item("B", "サルド", [PLAIN]),
                _item("Template:T", "サルド", [PLAIN], ns=10),
                _item("C", "転送", [], category="broken_redirect"),
            ],
        }

        pages = load_candidates(report)

        self.assertEqual(sorted(pages), ["A", "B"])
        self.assertEqual(
            pages["A"][1],
            Candidate("A", "古鳥類", (LABELED,), REPLACED_AT, 90),
        )

    def test_rejects_old_report(self):
        """`replaced_ills` のない古い版のJSONは読まない。"""
        with self.assertRaises(ValueError):
            load_candidates({"schema_version": 1, "items": []})


class CandidateReasonTests(unittest.TestCase):
    """リンク先の状態と記録から、戻すかどうかを決めることを検証する。"""

    candidate = Candidate("A", "サルド", (PLAIN,), REPLACED_AT, 90)

    def test_target_exists(self):
        """リンク先が作り直されていれば戻さない。"""
        check = (wiki_api.TargetStatus(wiki_api.EXISTS), None)

        self.assertEqual(
            candidate_reason(self.candidate, check, NOW), TARGET_EXISTS
        )

    def test_last_log_is_not_deletion(self):
        """最後の記録が削除でなければ戻さない。"""
        self.assertEqual(
            candidate_reason(
                self.candidate, _redlink(action="move/move"), NOW
            ),
            NO_DELETION,
        )
        self.assertEqual(
            candidate_reason(
                self.candidate,
                (wiki_api.TargetStatus(wiki_api.REDLINK), None),
                NOW,
            ),
            NO_DELETION,
        )

    def test_recent_deletion(self):
        """削除から30日たっていなければ戻さない。"""
        self.assertEqual(
            candidate_reason(
                self.candidate, _redlink("2026-10-20T00:00:00Z"), NOW
            ),
            DELETED_RECENTLY,
        )

    def test_allows_old_deletion(self):
        """置き換えの後に削除され、30日たっていれば戻してよい。"""
        self.assertIsNone(
            candidate_reason(self.candidate, _redlink(), NOW)
        )


class PlanPageTests(unittest.TestCase):
    """1つのページの戻し方を決めることを検証する。"""

    def test_page_gone(self):
        """ページがなければ、すべてのリンク先を見送る。"""
        result = plan_page(
            [Candidate("A", "サルド", (PLAIN,), REPLACED_AT, 90)],
            None,
            {},
            TitleNormalizer(),
            NOW,
        )

        self.assertEqual(
            result.targets, [TargetResult("サルド", 0, PAGE_GONE)]
        )
        self.assertFalse(result.changed)

    def test_restores_allowed_targets_and_reports_skipped(self):
        """戻してよいリンク先だけを戻し、見送ったものは理由を付ける。"""
        result = plan_page(
            [
                Candidate("A", "サルド", (PLAIN,), REPLACED_AT, 90),
                Candidate("A", "古鳥類", (LABELED,), REPLACED_AT, 90),
            ],
            _page("A", "[[サルド]] と [[古鳥類|古鳥亜綱]]"),
            {
                "サルド": _redlink(),
                "古鳥類": _redlink("2026-10-20T00:00:00Z"),
            },
            TitleNormalizer(),
            NOW,
        )

        self.assertEqual(result.restored_text, PLAIN + " と [[古鳥類|古鳥亜綱]]")
        self.assertEqual(
            result.targets,
            [
                TargetResult("古鳥類", 0, DELETED_RECENTLY),
                TargetResult("サルド", 1),
            ],
        )
        self.assertTrue(result.changed)


class DryRunTests(unittest.TestCase):
    """APIで確かめて、結果と差分を出すまでを検証する。"""

    def test_dry_run_and_render(self):
        """リンク先・記録・最新版を取得し、結果のJSONと差分を作る。"""

        def responses(params):
            """要求の種類ごとの応答を返す。"""
            if params.get("prop") == "info":
                return {
                    "query": {
                        "pages": [
                            {"title": "サルド", "missing": True},
                            {"title": "古鳥類", "missing": True},
                        ]
                    }
                }
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
                                "title": "A",
                                "ns": 0,
                                "revisions": [
                                    {
                                        "revid": 100,
                                        "timestamp": "2026-10-01T00:00:00Z",
                                        "slots": {
                                            "main": {
                                                "content": "前\n[[サルド]]\n後"
                                            }
                                        },
                                    }
                                ],
                            },
                            {
                                "title": "B",
                                "ns": 0,
                                "revisions": [
                                    {
                                        "revid": 200,
                                        "timestamp": "2026-10-01T00:00:00Z",
                                        "slots": {
                                            "main": {"content": "[[古鳥類]]"}
                                        },
                                    }
                                ],
                            },
                            {"title": "C", "missing": True},
                        ]
                    },
                }
            raise AssertionError(params)

        pages = {
            "A": [Candidate("A", "サルド", (PLAIN,), REPLACED_AT, 90)],
            "B": [Candidate("B", "古鳥類", (LABELED,), REPLACED_AT, 90)],
            "C": [Candidate("C", "サルド", (PLAIN,), REPLACED_AT, 90)],
        }

        results = dry_run(FakeApi(responses), pages, TitleNormalizer(), NOW)

        summary = summarize(results)
        self.assertEqual((summary.pages, summary.pages_to_edit), (3, 1))
        self.assertEqual(
            dict(summary.targets),
            {"restored": 1, LINK_CHANGED: 1, PAGE_GONE: 1},
        )
        self.assertEqual(
            render_diff(results),
            "=== A（版 100）\n-[[サルド]]\n+" + PLAIN + "\n",
        )
        output = json.loads(
            render_results(results, generated_at=NOW, report_path="r.json")
        )
        self.assertTrue(output["dry_run"])
        self.assertEqual(output["counts"]["pages_to_edit"], 1)
        self.assertEqual(
            output["pages"][0],
            {
                "title": "A",
                "revision_id": 100,
                "edit": True,
                "new_revision_id": None,
                "error": None,
                "targets": [
                    {"title": "サルド", "restored": 1, "reason": None}
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
