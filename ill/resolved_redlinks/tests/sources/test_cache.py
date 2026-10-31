"""キャッシュの保存と版の照合を検証する。"""

import tempfile
import unittest
from pathlib import Path

from ill.resolved_redlinks.sources.cache import Cache
from ill.resolved_redlinks.tests.helpers import make_edit


class CacheTests(unittest.TestCase):
    """キャッシュの保存と版の照合を検証する。"""

    def setUp(self):
        """一時ディレクトリを用意する。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "cache.sqlite3"

    def test_keeps_edits_and_ignores_duplicates(self):
        """同じ revision ID の編集は1件にまとめ、新しい順に返す。"""
        with Cache(self.path) as cache:
            with cache.transaction():
                added = cache.add_edits(
                    [
                        make_edit(1, "A", "2020-01-01T00:00:00Z", "c"),
                        make_edit(2, "B", "2021-01-01T00:00:00Z", "c"),
                        make_edit(1, "A", "2020-01-01T00:00:00Z", "c"),
                    ]
                )
            self.assertEqual(added, 2)
        with Cache(self.path) as cache:
            self.assertEqual(
                [edit.revid for edit in cache.iter_edits()], [2, 1]
            )
            cache.clear_edits()
            self.assertEqual(cache.count_edits(), 0)

    def test_caches_removed_ill_templates_by_revision(self):
        """置換前の仮リンクを版ごとに保存し、再利用する。"""
        template = "{{仮リンク|A|en|A}}"
        with Cache(self.path) as cache:
            self.assertIsNone(cache.get_diff_ills(10))
            cache.set_diff_ills(10, [template])
            self.assertEqual(cache.get_diff_ills(10), [template])

    def test_rejects_other_schema_version(self):
        """表の版が違うキャッシュはエラーにする。"""
        with Cache(self.path) as cache:
            with cache.transaction():
                cache.set_meta("schema_version", "0")
        with self.assertRaisesRegex(RuntimeError, "版が合いません"):
            Cache(self.path)

    def test_rejects_broken_file(self):
        """SQLiteとして読めないファイルはエラーにする。"""
        self.path.write_bytes(b"not a database" * 100)
        with self.assertRaisesRegex(RuntimeError, "読み込めません"):
            Cache(self.path)


if __name__ == "__main__":
    unittest.main()
