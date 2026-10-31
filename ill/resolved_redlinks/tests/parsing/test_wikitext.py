"""最新版の本文の内部リンク・仮リンクの判定と、差分の解析を検証する。"""

import unittest

from ill.resolved_redlinks.parsing.titles import TitleKey, TitleNormalizer
from ill.resolved_redlinks.parsing.wikitext import (
    added_link_targets,
    ill_ja_names,
    ill_templates_for,
    ill_wording,
    internal_link_spans,
    page_link_state,
    parse_compare_body,
    removed_ill_templates,
)


class WikitextTests(unittest.TestCase):
    """最新版の本文の内部リンクと仮リンクの判定を検証する。"""

    def setUp(self):
        """正規化器を用意する。"""
        self.normalizer = TitleNormalizer()

    def test_collects_links_and_ill_names(self):
        """内部リンクと仮リンクの記事名を集め、テンプレートが作るリンクは除く。"""
        links, ills = page_link_state(
            "[[ニコライ・チェボタリョフ]] と {{仮リンク|A|en|B}}"
            " と {{ill|c|en|C}} と {{Main|D}} と {{説明|[[E|e]]}}",
            self.normalizer,
        )

        self.assertEqual(
            links,
            {TitleKey(0, "ニコライ・チェボタリョフ"), TitleKey(0, "E")},
        )
        self.assertEqual(ills, {TitleKey(0, "A"), TitleKey(0, "C")})

    def test_ignores_comments_nowiki_and_counts_noinclude(self):
        """コメント・nowiki内のリンクは数えず、noinclude内は数える。"""
        links, _ = page_link_state(
            "<!-- [[A]] --> <nowiki>[[B]]</nowiki> <nowiki/>"
            "<noinclude>[[C]]</noinclude>",
            self.normalizer,
        )

        self.assertEqual(links, {TitleKey(0, "C")})

    def test_collects_links_in_file_caption_and_ill_aliases(self):
        """画像の説明文の中のリンクと、仮リンクの別名（illm など）を数える。"""
        links, ills = page_link_state(
            "[[File:A.jpg|thumb|left|[[レンスター・ガーデンズ]]の写真]]"
            " {{illm|ドローバー|en|Drover}} {{Interlanguage link|X|en|Y}}",
            self.normalizer,
        )

        self.assertIn(TitleKey(0, "レンスター・ガーデンズ"), links)
        self.assertEqual(ills, {TitleKey(0, "ドローバー"), TitleKey(0, "X")})

    def test_unclosed_template_does_not_fail(self):
        """閉じていない仮リンクがあっても失敗しない。"""
        self.assertEqual(
            ill_ja_names("{{仮リンク|A|en|B}} {{仮リンク|X|en"), ["A"]
        )


COMPARE_BODY = """
<tr><td class="diff-marker" data-marker="−"></td>
<td class="diff-deletedline diff-side-deleted"><div>{{仮リンク|チャールズ・レノックス (第3代リッチモンド公)|label=チャールズ・レノックス|en|Charles Lennox}}と{{仮リンク|ジョージ・レノックス (1737-1805)|label=ジョージ|en|Lord George Lennox}}と[[既存]]</div></td>
<td class="diff-marker" data-marker="+"></td>
<td class="diff-addedline diff-side-added"><div><ins class="diffchange">[[チャールズ・レノックス (第3代リッチモンド公)|チャールズ・レノックス]]</ins>と{{仮リンク|ジョージ・レノックス (1737-1805)|label=ジョージ|en|Lord George Lennox}}と[[既存]]と[[B. L. van der Waerden|B &amp; L]]</div></td></tr>
<tr><td class="diff-deletedline diff-side-deleted"><div>{{仮リンク|ファン・デル・ヴェルデン|en|Bartel Leendert van der Waerden}}</div></td>
<td class="diff-addedline diff-side-added"><div></div></td></tr>
"""  # noqa: E501


class CompareTests(unittest.TestCase):
    """差分から置き換えたリンク先を拾う処理を検証する。"""

    def setUp(self):
        """ページ名の正規化器を用意する。"""
        self.normalizer = TitleNormalizer()

    def test_parses_diff_table(self):
        """差分表から消えた側・増えた側の行を取り出し、文字参照を戻す。"""
        deleted, added = parse_compare_body(COMPARE_BODY)

        self.assertEqual(len(deleted), 2)
        self.assertEqual(len(added), 2)
        self.assertIn("[[B. L. van der Waerden|B & L]]", added[0])

    def test_extracts_original_ill_template_and_display_wording(self):
        """差分から置換された仮リンクを拾い、labelまたは記事名を表示文字列にする。"""
        deleted, added = parse_compare_body(COMPARE_BODY)
        templates = removed_ill_templates(deleted, added)

        matched = ill_templates_for(
            templates,
            "チャールズ・レノックス (第3代リッチモンド公)",
            self.normalizer,
        )

        self.assertEqual(
            matched,
            [
                "{{仮リンク|チャールズ・レノックス "
                "(第3代リッチモンド公)|label=チャールズ・レノックス|en|"
                "Charles Lennox}}"
            ],
        )
        self.assertEqual(ill_wording(matched[0]), "チャールズ・レノックス")
        self.assertEqual(
            ill_templates_for(
                templates,
                "ジョージ・レノックス (1737-1805)",
                self.normalizer,
            ),
            [],
        )
        self.assertEqual(
            ill_wording(
                "{{仮リンク|イギリス領ソロモン諸島|en|"
                "British Solomon Islands}}"
            ),
            "イギリス領ソロモン諸島",
        )

    def test_internal_link_spans_point_into_original_text(self):
        """リンク先と元の本文での位置を返し、コメントの中は除く。

        中にリンクを含む画像のリンクは、終わりの位置を None にする。
        """
        text = "<!-- [[A]] -->[[B|b]] [[ファイル:X.jpg|[[C]]]]"

        spans = internal_link_spans(text)

        self.assertEqual(
            [(target, text[start:end] if end else None)
             for target, start, end in spans],
            [("B", "[[B|b]]"), ("ファイル:X.jpg", None), ("C", "[[C]]")],
        )

    def test_collects_every_ill_for_the_target_in_order(self):
        """同じ記事名の仮リンクを、名前の表記の違いも含めて出現順にすべて返す。"""
        templates = [
            "{{仮リンク|古鳥類|en|Archaeornithes|label=古鳥亜綱}}",
            "{{仮リンク|別の記事|en|Other}}",
            "{{ill2|古鳥類|en|Archaeornithes}}",
            "{{仮リンク|古鳥類|en|Archaeornithes|label=古鳥亜綱}}",
        ]

        self.assertEqual(
            ill_templates_for(templates, "古鳥類", self.normalizer),
            [templates[0], templates[2], templates[3]],
        )
        self.assertEqual(
            ill_templates_for(templates, "存在しない", self.normalizer), []
        )

    def test_picks_only_newly_added_links(self):
        """増えた側にだけある内部リンクを拾い、残った仮リンクは数えない。

        第1引数と違う名前で入れたリンク（B. L. van der Waerden）も拾う。
        """
        deleted, added = parse_compare_body(COMPARE_BODY)

        links, resolved = added_link_targets(
            deleted, added, TitleNormalizer()
        )

        self.assertEqual(
            links,
            [
                TitleKey(0, "B. L. van der Waerden"),
                TitleKey(0, "チャールズ・レノックス (第3代リッチモンド公)"),
            ],
        )
        self.assertEqual(resolved, 2)

    def test_picks_link_in_file_caption(self):
        """画像の説明文の中で置き換えたリンクも拾う（版 60286898 の形）。"""
        links, resolved = added_link_targets(
            [
                "[[File:A.jpg|thumb|{{仮リンク|レンスター・ガーデンズ|en|"
                "Leinster Gardens}}に実在するファサード。]]"
            ],
            ["[[File:A.jpg|thumb|[[レンスター・ガーデンズ]]に実在するファサード。]]"],
            TitleNormalizer(),
        )

        self.assertEqual(links, [TitleKey(0, "レンスター・ガーデンズ")])
        self.assertEqual(resolved, 1)


if __name__ == "__main__":
    unittest.main()
