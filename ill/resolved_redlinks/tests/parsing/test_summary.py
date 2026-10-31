"""編集要約の判定とリンク先の取り出しを検証する。"""

import unittest

from ill.resolved_redlinks.parsing.summary import (
    is_target_summary,
    parse_summary_links,
)

EXAMPLE_SUMMARY = (
    "解消済み仮リンク[[Bartel Leendert van der Waerden|B. L. van der "
    "Waerden]]、[[ニコライ・チェボタリョフ]]、[[理論物理学ペリメーター"
    "研究所]]を内部リンクに置き換えます"
)
TRUNCATED_SUMMARY = (
    "bot: 解消済み仮リンク[[チャールズ・レノックス (第3代リッチモンド公)|"
    "チャールズ・レノックス]]、[[チャールズ・レノックス (第3代リッチモンド"
    "公)|3代リッチモンド公<br>'''3代レノックス..."
)


class SummaryTests(unittest.TestCase):
    """編集要約の判定とリンク先の取り出しを検証する。"""

    def test_accepts_summaries_with_and_without_bot_prefix(self):
        """「bot: 」の有無のどちらも対象にする。"""
        self.assertTrue(is_target_summary(EXAMPLE_SUMMARY))
        self.assertTrue(
            is_target_summary(
                "bot: 解消済み仮リンク[[アレクサンダー・フォン・ヴァーグナー]]"
                "を内部リンクに置き換えます"
            )
        )

    def test_rejects_work_report_and_other_summaries(self):
        """作業報告・ほかの要約・要約なしを対象外にする。"""
        self.assertFalse(
            is_target_summary("解消済み仮リンクを内部リンクに置き換える作業の報告")
        )
        self.assertFalse(is_target_summary("ウィキ文法修正: 角括弧の終了違反"))
        self.assertFalse(is_target_summary("x 解消済み仮リンク[[A]]"))
        self.assertFalse(is_target_summary(None))

    def test_extracts_targets_of_example_edit(self):
        """具体例の版 90950554 の要約から3件のリンク先を取り出す。"""
        result = parse_summary_links(EXAMPLE_SUMMARY)

        self.assertEqual(
            result.targets,
            (
                "Bartel Leendert van der Waerden",
                "ニコライ・チェボタリョフ",
                "理論物理学ペリメーター研究所",
            ),
        )
        self.assertFalse(result.truncated)

    def test_handles_markup_notes_and_progress(self):
        """表示名中のマークアップ、注記、進み具合の表示を扱う。"""
        result = parse_summary_links(
            "解消済み仮リンク[[F4 (数学)|F<sub>4</sub>]]、[[スコット・キング "
            "(バスケットボール)|スコット・キング]](Local page title contains "
            "the local title in the interlanguage template)を内部リンクに"
            "置き換えます (今回のBot作業のうち91%が完了しました)"
        )

        self.assertEqual(
            result.targets,
            ("F4 (数学)", "スコット・キング (バスケットボール)"),
        )
        self.assertFalse(result.truncated)

    def test_accepts_old_summary_ending(self):
        """2016年ごろの「…を内部リンクに置き換える」は切れた要約としない。"""
        result = parse_summary_links(
            "bot: 解消済み仮リンク[[ストックマン]]を内部リンクに置き換える"
        )

        self.assertEqual(result.targets, ("ストックマン",))
        self.assertFalse(result.truncated)

    def test_detects_truncated_summary_and_drops_unclosed_link(self):
        """切れた要約を検出し、閉じていない最後のリンクを捨て、重複を除く。"""
        result = parse_summary_links(TRUNCATED_SUMMARY)

        self.assertTrue(result.truncated)
        self.assertEqual(
            result.targets, ("チャールズ・レノックス (第3代リッチモンド公)",)
        )

    def test_rejects_non_target_summary(self):
        """対象でない要約はエラーにする。"""
        with self.assertRaises(ValueError):
            parse_summary_links("別の要約")


if __name__ == "__main__":
    unittest.main()
