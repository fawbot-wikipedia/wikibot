"""Cewbotが入れた内部リンクを仮リンクに戻す規則を検証する。"""

import unittest
from datetime import datetime, timedelta, timezone

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.restore.rules import (
    DELETED_BEFORE_REPLACEMENT,
    DELETED_RECENTLY,
    EXTRA_LINKS,
    LINK_CHANGED,
    NO_LINK,
    NO_ORIGINAL_ILL,
    NOT_RESTORED,
    Target,
    TargetResult,
    cewbot_link,
    deletion_wait_reason,
    restore_page,
    restore_target,
)

PLAIN = "{{仮リンク|サルド|en|sardo}}"
LABELED = "{{仮リンク|古鳥類|en|Archaeornithes|label=古鳥亜綱}}"
UNLABELED = "{{仮リンク|古鳥類|en|Archaeornithes}}"


class CewbotLinkTests(unittest.TestCase):
    """仮リンクからCewbotが入れる内部リンクの形を作ることを検証する。"""

    def test_plain(self):
        """表示文言がなければ記事名だけのリンクにする。"""
        self.assertEqual(cewbot_link(PLAIN), "[[サルド]]")

    def test_label(self):
        """表示文言があれば、記事名と表示文言のリンクにする。"""
        self.assertEqual(cewbot_link(LABELED), "[[古鳥類|古鳥亜綱]]")
        self.assertEqual(
            cewbot_link(
                "{{仮リンク|label=アル・ズバラ考古遺跡|アル・ズバラ|en|"
                "Zubarah}}"
            ),
            "[[アル・ズバラ|アル・ズバラ考古遺跡]]",
        )

    def test_label_same_as_title_or_empty(self):
        """表示文言が記事名と同じか空なら、記事名だけのリンクにする。"""
        self.assertEqual(
            cewbot_link("{{仮リンク|スコピエ地震|en|X|label=スコピエ地震}}"),
            "[[スコピエ地震]]",
        )
        self.assertEqual(cewbot_link("{{仮リンク|A|en|B|label=}}"), "[[A]]")

    def test_other_template_names(self):
        """仮リンクの別名のテンプレートも同じ形にする。"""
        self.assertEqual(
            cewbot_link("{{ill2|アマルーラ|en|Amarula}}"), "[[アマルーラ]]"
        )


class RestoreTargetTests(unittest.TestCase):
    """1つのリンク先へのリンクを仮リンクに戻すことを検証する。"""

    def setUp(self):
        """正規化器を用意する。"""
        self.normalizer = TitleNormalizer()

    def restore(self, text, title, *templates):
        """リンク先と消えた仮リンクを指定して戻す。"""
        return restore_target(text, Target(title, templates), self.normalizer)

    def test_restores_plain_link(self):
        """Cewbotが入れた形のリンクを、元の仮リンクに戻す。"""
        text, result = self.restore("前 [[サルド]] 後", "サルド", PLAIN)

        self.assertEqual(text, "前 " + PLAIN + " 後")
        self.assertEqual(result, TargetResult("サルド", 1))

    def test_restores_labeled_and_unlabeled_links_by_form(self):
        """同じリンク先の、表示文言付きとなしを、それぞれの仮リンクに戻す。"""
        text, result = self.restore(
            "[[古鳥類]] と [[古鳥類|古鳥亜綱]]", "古鳥類", LABELED, UNLABELED
        )

        self.assertEqual(text, UNLABELED + " と " + LABELED)
        self.assertEqual(result.restored, 2)

    def test_restores_same_link_twice(self):
        """同じ仮リンクを2つ置き換えていれば、2か所とも戻す。"""
        text, result = self.restore(
            "[[サルド]]、[[サルド]]", "サルド", PLAIN, PLAIN
        )

        self.assertEqual(text, PLAIN + "、" + PLAIN)
        self.assertEqual(result.restored, 2)

    def test_restores_link_in_image_caption(self):
        """画像の説明文の中のリンクも戻す。"""
        text, result = self.restore(
            "[[ファイル:X.jpg|thumb|[[サルド]]の図]]", "サルド", PLAIN
        )

        self.assertEqual(text, "[[ファイル:X.jpg|thumb|" + PLAIN + "の図]]")
        self.assertEqual(result.restored, 1)

    def test_ignores_links_in_comments(self):
        """コメントの中のリンクは数えず、そのまま残す。"""
        text, result = self.restore(
            "<!-- [[サルド]] --> [[サルド]]", "サルド", PLAIN
        )

        self.assertEqual(text, "<!-- [[サルド]] --> " + PLAIN)
        self.assertEqual(result.restored, 1)

    def test_skips_when_wording_changed(self):
        """表示文言が書き換えられていたら、本文を変えずに見送る。"""
        original = "[[古鳥類|古鳥]]"

        text, result = self.restore(original, "古鳥類", LABELED)

        self.assertEqual(text, original)
        self.assertEqual(result, TargetResult("古鳥類", 0, LINK_CHANGED))

    def test_skips_when_more_links_than_replaced(self):
        """Cewbotが置き換えた数より多ければ、どれも戻さずに見送る。"""
        original = "[[サルド]] と [[サルド]]"

        text, result = self.restore(original, "サルド", PLAIN)

        self.assertEqual(text, original)
        self.assertEqual(result.reason, EXTRA_LINKS)

    def test_skips_link_with_different_first_letter_case(self):
        """先頭の大文字・小文字が違うリンクも同じリンク先として数え、見送る。"""
        original = "[[Foo]] と [[foo]]"

        text, result = self.restore(
            original, "Foo", "{{仮リンク|Foo|en|Foo}}"
        )

        self.assertEqual(text, original)
        self.assertEqual(result.reason, LINK_CHANGED)

    def test_skips_link_with_underscore(self):
        """空白を `_` で書いたリンクも同じリンク先として数え、見送る。"""
        original = "[[Foo_bar]]"

        text, result = self.restore(
            original, "Foo bar", "{{仮リンク|Foo bar|en|Foo}}"
        )

        self.assertEqual(text, original)
        self.assertEqual(result.reason, LINK_CHANGED)

    def test_skips_when_no_link_remains(self):
        """リンクが残っていなければ見送る。"""
        text, result = self.restore("本文", "サルド", PLAIN)

        self.assertEqual(text, "本文")
        self.assertEqual(result.reason, NO_LINK)

    def test_skips_when_result_is_not_an_ill(self):
        """置き換えたあとに仮リンクになっていなければ、本文を変えずに見送る。

        仮リンク系でないテンプレートが渡された場合。
        """
        text, result = self.restore("[[サルド]]", "サルド", "{{Main|サルド}}")

        self.assertEqual(text, "[[サルド]]")
        self.assertEqual(result, TargetResult("サルド", 0, NOT_RESTORED))

    def test_skips_without_original_ill(self):
        """置換前の仮リンクが分からなければ見送る。"""
        text, result = self.restore("[[サルド]]", "サルド")

        self.assertEqual(text, "[[サルド]]")
        self.assertEqual(result.reason, NO_ORIGINAL_ILL)


class RestorePageTests(unittest.TestCase):
    """1つのページで、リンク先ごとに戻すことを検証する。"""

    def test_restores_other_targets_when_one_is_skipped(self):
        """戻せないリンク先があっても、ほかのリンク先は戻す。"""
        text, results = restore_page(
            "[[サルド]] と [[古鳥類|古鳥]]",
            [Target("サルド", [PLAIN]), Target("古鳥類", [LABELED])],
            TitleNormalizer(),
        )

        self.assertEqual(text, PLAIN + " と [[古鳥類|古鳥]]")
        self.assertEqual(
            results,
            [
                TargetResult("サルド", 1),
                TargetResult("古鳥類", 0, LINK_CHANGED),
            ],
        )


class DeletionWaitTests(unittest.TestCase):
    """削除から戻すまでの待ちを検証する。"""

    replaced = datetime(2024, 1, 1, tzinfo=timezone.utc)
    deleted = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_waits_after_deletion(self):
        """削除から30日たっていなければ待つ。"""
        self.assertEqual(
            deletion_wait_reason(
                self.deleted,
                self.replaced,
                self.deleted + timedelta(days=29),
            ),
            DELETED_RECENTLY,
        )

    def test_allows_after_wait(self):
        """削除から30日たっていれば戻してよい。"""
        self.assertIsNone(
            deletion_wait_reason(
                self.deleted,
                self.replaced,
                self.deleted + timedelta(days=30),
            )
        )

    def test_skips_deletion_before_replacement(self):
        """置き換えより前の削除は、この作業の対象にしない。"""
        self.assertEqual(
            deletion_wait_reason(
                self.replaced,
                self.deleted,
                self.deleted + timedelta(days=60),
            ),
            DELETED_BEFORE_REPLACEMENT,
        )


if __name__ == "__main__":
    unittest.main()
