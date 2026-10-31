"""ページ名の正規化を検証する。"""

import unittest

from ill.resolved_redlinks.parsing.titles import TitleKey, TitleNormalizer


class TitleTests(unittest.TestCase):
    """ページ名の正規化を検証する。"""

    def setUp(self):
        """日本語版の既定の名前空間で正規化器を作る。"""
        self.normalizer = TitleNormalizer()

    def test_capitalizes_only_first_letter(self):
        """先頭1文字だけを大文字にし、ほかの大文字・小文字は区別する。"""
        self.assertEqual(
            self.normalizer.key("iPhone"), TitleKey(0, "IPhone")
        )
        self.assertNotEqual(
            self.normalizer.key("Abc"), self.normalizer.key("ABC")
        )

    def test_normalizes_spaces_sections_and_colon(self):
        """`_`・空白・`#節`・先頭の `:` を正規化する。"""
        self.assertEqual(
            self.normalizer.key(" :ニコライ_チェボタリョフ#生涯 "),
            TitleKey(0, "ニコライ チェボタリョフ"),
        )
        self.assertIsNone(self.normalizer.key("#節だけ"))

    def test_resolves_namespace_aliases(self):
        """名前空間の正式名と別名を番号にそろえる。"""
        expected = TitleKey(10, "仮リンク")
        self.assertEqual(self.normalizer.key("Template:仮リンク"), expected)
        self.assertEqual(self.normalizer.key("template:仮リンク"), expected)
        self.assertEqual(self.normalizer.key("テンプレート:仮リンク"), expected)
        self.assertEqual(
            self.normalizer.key("未知:名前"), TitleKey(0, "未知:名前")
        )

    def test_builds_from_siteinfo(self):
        """siteinfoの応答から名前空間と別名を読み込む。"""
        normalizer = TitleNormalizer.from_siteinfo(
            {
                "query": {
                    "namespaces": {
                        "0": {"id": 0, "name": ""},
                        "10": {
                            "id": 10,
                            "name": "Template",
                            "canonical": "Template",
                        },
                    },
                    "namespacealiases": [{"id": 10, "alias": "T"}],
                }
            }
        )

        self.assertEqual(normalizer.key("T:X"), TitleKey(10, "X"))
        self.assertEqual(normalizer.display(TitleKey(10, "X")), "Template:X")

    def test_dump_key_matches_link_key(self):
        """ダンプのページ名とリンク先のキーが一致する。"""
        self.assertEqual(
            self.normalizer.dump_key(0, "ニコライ・チェボタリョフ"),
            self.normalizer.key("ニコライ・チェボタリョフ"),
        )
        self.assertEqual(
            self.normalizer.dump_key(10, "Lie_groups"),
            self.normalizer.key("Template:Lie groups"),
        )


if __name__ == "__main__":
    unittest.main()
