"""SQLダンプの解析と一次判定を検証する。"""

import gzip
import tempfile
import unittest
from pathlib import Path

from ill.resolved_redlinks.parsing.titles import TitleNormalizer
from ill.resolved_redlinks.sources.dump import (
    BROKEN_REDIRECT_CANDIDATE,
    REDLINK_CANDIDATE,
    DumpFiles,
    iter_page_rows,
    iter_redirect_rows,
    judge_targets,
    unescape_sql,
)

PAGE_SQL = """-- MySQL dump
CREATE TABLE `page` (`page_id` int);
INSERT INTO `page` VALUES
(1,0,'エミー・ネーター',0,0,0.1,'20260101000000',NULL,10,100,'wikitext',NULL),
(2,0,'転送A',1,0,0.2,'20260101000000',NULL,11,30,'wikitext',NULL),
(3,0,'転送B',1,0,0.3,'20260101000000',NULL,12,30,'wikitext',NULL),
(4,0,'転送先あり',0,0,0.4,'20260101000000',NULL,13,30,'wikitext',NULL),
(5,0,'O\\'Brien',0,0,0.5,'20260101000000',NULL,14,30,'wikitext',NULL),
(6,0,'他ウィキ転送',1,0,0.6,'20260101000000',NULL,15,30,'wikitext',NULL),
(7,0,'表にない転送',1,0,0.7,'20260101000000',NULL,16,30,'wikitext',NULL),
(8,10,'Lie_groups',0,0,0.8,'20260101000000',NULL,17,30,'wikitext',NULL);
"""
REDIRECT_SQL = """INSERT INTO `redirect` VALUES
(2,0,'存在しない転送先','',''),
(3,0,'転送先あり','','節'),
(6,0,'Foo','en',NULL);
"""


class SqlDumpTests(unittest.TestCase):
    """SQLダンプの解析と一次判定を検証する。"""

    def setUp(self):
        """一時ディレクトリにダンプを作る。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        page = root / "page.sql.gz"
        redirect = root / "redirect.sql.gz"
        page.write_bytes(gzip.compress(PAGE_SQL.encode("utf-8")))
        redirect.write_bytes(gzip.compress(REDIRECT_SQL.encode("utf-8")))
        self.files = DumpFiles("20261001", page, redirect)
        self.normalizer = TitleNormalizer()

    def test_unescapes_sql_strings(self):
        """バックスラッシュによるエスケープを戻す。"""
        self.assertEqual(unescape_sql(r"a\'b\\c\nd"), "a'b\\c\nd")

    def test_parses_rows_with_escape_and_null(self):
        """エスケープされた文字列とNULLを含む行を読む。"""
        pages = list(iter_page_rows(self.files.page_path))
        redirects = list(iter_redirect_rows(self.files.redirect_path))

        self.assertEqual(len(pages), 8)
        self.assertEqual(pages[4], (5, 0, "O'Brien", False))
        self.assertEqual(redirects[2], (6, 0, "Foo", "en", ""))

    def test_judges_candidates(self):
        """不存在・壊れた転送・転送表にない転送を候補にし、それ以外は除く。"""
        key = self.normalizer.key
        targets = {
            key("エミー・ネーター"),
            key("削除された記事"),
            key("転送A"),
            key("転送B"),
            key("他ウィキ転送"),
            key("表にない転送"),
            key("O'Brien"),
            key("Template:Lie groups"),
        }

        judgement = judge_targets(self.files, targets, self.normalizer)

        self.assertEqual(
            judgement.candidates,
            {
                key("削除された記事"): REDLINK_CANDIDATE,
                key("転送A"): BROKEN_REDIRECT_CANDIDATE,
                key("表にない転送"): BROKEN_REDIRECT_CANDIDATE,
            },
        )
        self.assertEqual(
            judgement.redirect_targets[key("転送B")], key("転送先あり")
        )


if __name__ == "__main__":
    unittest.main()
