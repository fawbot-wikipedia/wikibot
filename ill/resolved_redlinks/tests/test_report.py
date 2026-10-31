"""JSON出力と補助的な表示値を検証する。"""

import json
import unittest
from datetime import datetime, timezone

from ill.resolved_redlinks.report import (
    BROKEN_REDIRECT,
    STILL_REDLINK,
    ReportRow,
    format_log_note,
    page_url,
    render_json_report,
    to_jst_date,
)


class ReportTests(unittest.TestCase):
    """JSON出力と補助的な表示値を検証する。"""

    def test_renders_metadata_counts_and_link_records(self):
        """条件・件数・リンク単位のレコードを構造化して出力する。"""
        rows = [
            ReportRow(
                STILL_REDLINK,
                0,
                "エミー・ネーター",
                "ニコライ・チェボタリョフ",
                90950554,
                "2022-08-12T20:30:48Z",
                format_log_note(
                    "delete/delete",
                    "2025-12-21T12:08:22Z",
                    "転送先がないリダイレクト",
                    None,
                ),
                "{{仮リンク|ニコライ・チェボタリョフ|en|Nikolai "
                "Chebotaryov}}",
                "ニコライ・チェボタリョフ",
            ),
            ReportRow(
                BROKEN_REDIRECT,
                10,
                "Template:X|Y",
                "転送",
                5,
                "2020-01-01T00:00:00Z",
                format_log_note(
                    "move/move", "2024-01-01T00:00:00Z", "改名", "新", "旧先"
                ),
            ),
        ]

        text = render_json_report(
            rows,
            generated_at=datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc),
            dump_date="20261001",
            namespaces=(0, 10),
            namespace_names={0: "", 10: "Template"},
            since="2022-08-13",
            until="2022-08-13",
            reference_counts={
                "restored_ill": 2,
                "link_removed": 3,
                "page_gone": 1,
            },
        )
        report = json.loads(text)

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(
            report["generated_at"], "2026-10-27T03:00:00+00:00"
        )
        self.assertEqual(report["dump_date"], "20261001")
        self.assertEqual(
            report["filters"],
            {
                "namespaces": [
                    {"id": 0, "name": "記事"},
                    {"id": 10, "name": "Template"},
                ],
                "period": {"since": "2022-08-13", "until": "2022-08-13"},
            },
        )
        self.assertEqual(
            report["counts"],
            {
                "still_redlink": 1,
                "broken_redirect": 1,
                "restored_ill": 2,
                "link_removed": 3,
                "page_gone": 1,
            },
        )
        self.assertEqual(
            [item["category"] for item in report["items"]],
            [BROKEN_REDIRECT, STILL_REDLINK],
        )
        redlink = report["items"][1]
        self.assertEqual(redlink["page"]["title"], "エミー・ネーター")
        self.assertEqual(
            redlink["target"]["url"],
            page_url("ニコライ・チェボタリョフ"),
        )
        self.assertEqual(
            redlink["replacement"],
            {
                "revision_id": 90950554,
                "url": "https://ja.wikipedia.org/w/index.php?oldid=90950554",
                "diff_url": (
                    "https://ja.wikipedia.org/w/index.php?diff=90950554"
                ),
                "timestamp": "2022-08-12T20:30:48Z",
                "date_jst": "2022-08-13",
            },
        )
        self.assertEqual(
            redlink["log_note"],
            "2025-12-21 削除: 転送先がないリダイレクト",
        )
        self.assertEqual(
            redlink["original_ill"],
            "{{仮リンク|ニコライ・チェボタリョフ|en|Nikolai "
            "Chebotaryov}}",
        )
        self.assertEqual(redlink["wording"], "ニコライ・チェボタリョフ")
        self.assertEqual(
            report["items"][0]["log_note"],
            "転送先「旧先」 2024-01-01 移動（→ 新）: 改名",
        )
        self.assertIn("エミー・ネーター", text)

    def test_formats_urls_dates_and_escapes(self):
        """URLのエンコードと日本時間の日付を確認する。"""
        self.assertEqual(
            page_url("F4 (数学)"),
            "https://ja.wikipedia.org/wiki/F4_%28%E6%95%B0%E5%AD%A6%29",
        )
        self.assertEqual(to_jst_date("2022-08-12T20:30:48Z"), "2022-08-13")
        self.assertEqual(format_log_note(None, None, None, None), "")

    def test_truncates_long_log_comment(self):
        """削除の理由が長ければ、空白をまとめたうえで100文字で切る。"""
        note = format_log_note(
            "delete/delete",
            "2022-10-19T00:00:00Z",
            "依頼\n内容:" + "あ" * 200,
            None,
        )

        self.assertEqual(note, "2022-10-19 削除: 依頼 内容:" + "あ" * 94 + "…")


if __name__ == "__main__":
    unittest.main()
