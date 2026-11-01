"""ボットのログインと、編集の間隔・緊急停止・保存を検証する。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wikiutil.mediawiki import MediaWikiError

from ill.resolved_redlinks.restore.editor import (
    Credentials,
    Editor,
    load_credentials,
)
from ill.resolved_redlinks.sources import api as wiki_api

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
CREDENTIALS = Credentials("Fawbot@restore", "secret")
PAGE = wiki_api.EditablePage(
    "A", 0, 100, "2026-10-01T00:00:00Z", "[[サルド]]", "2026-11-01T00:00:00Z"
)


class FakeSession:
    """ログイン・利用者情報・トークン・編集の応答を返すSessionの代わり。"""

    def __init__(self, groups=("user",), name="Fawbot", messages=False):
        """利用者情報の中身を受け取る。"""
        self.info = {"name": name, "groups": list(groups)}
        if messages:
            self.info["messages"] = True
        self.logins = []
        self.edits = []

    def login(self, url, username, password):
        """ログインを記録する。"""
        self.logins.append((username, password))

    def user_info(self, url):
        """利用者情報を返す。"""
        return self.info

    def csrf_token(self, url):
        """トークンを返す。"""
        return "token+\\"

    def edit(self, url, **arguments):
        """編集を記録する。"""
        self.edits.append(arguments)
        return {"result": "Success", "newrevid": 101}


class FakeEditorApi:
    """Sessionと、停止用のページの取得に答えるAPIの代わり。"""

    def __init__(self, session, stop_text=None):
        """Sessionと、停止用のページの本文（なければ None）を受け取る。"""
        self.session = session
        self.stop_text = stop_text

    def query_pages(self, params):
        """停止用のページの最新版の応答を返す。"""
        page = {"title": params["titles"], "missing": True}
        if self.stop_text is not None:
            page = {
                "title": params["titles"],
                "ns": 2,
                "revisions": [
                    {
                        "revid": 1,
                        "timestamp": "2026-10-01T00:00:00Z",
                        "slots": {"main": {"content": self.stop_text}},
                    }
                ],
            }
        yield {"query": {"pages": [page]}}


class CredentialsTests(unittest.TestCase):
    """ログイン情報の読み込みを検証する。"""

    def test_reads_environment(self):
        """ログイン名とパスワードを読み、ボット名は `@` より前にする。"""
        credentials = load_credentials(
            {
                "WIKIPEDIA_USERNAME": " Fawbot@restore ",
                "WIKIPEDIA_BOT_PASSWORD": "secret",
            }
        )

        self.assertEqual(credentials.username, "Fawbot@restore")
        self.assertEqual(credentials.bot_name, "Fawbot")
        self.assertNotIn("secret", repr(credentials))

    def test_rejects_missing_or_malformed(self):
        """足りない場合と、`@` のないログイン名は受け付けない。"""
        with self.assertRaises(ValueError):
            load_credentials({"WIKIPEDIA_USERNAME": "Fawbot@restore"})
        with self.assertRaises(ValueError):
            load_credentials(
                {
                    "WIKIPEDIA_USERNAME": "Fawbot",
                    "WIKIPEDIA_BOT_PASSWORD": "secret",
                }
            )


class EditorTests(unittest.TestCase):
    """ログイン・間隔・停止・保存を検証する。"""

    def setUp(self):
        """次に編集してよい時刻の保存先を用意する。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "cache" / "edit_state.json"
        self.sleeps = []

    def editor(self, session, stop_text=None, now=NOW):
        """時刻と揺らぎを固定した Editor を作る。"""
        return Editor(
            FakeEditorApi(session, stop_text),
            CREDENTIALS,
            self.state_path,
            sleep=self.sleeps.append,
            now=lambda: now,
            jitter=lambda: 10,
        )

    def test_login_before_approval(self):
        """`bot` グループがなければ、承認前として `assert=user` で保存する。"""
        session = FakeSession()
        editor = self.editor(session)

        editor.login()
        editor.save(PAGE, "新しい本文", "要約")

        self.assertEqual(session.logins, [("Fawbot@restore", "secret")])
        self.assertFalse(editor.approved)
        sent = session.edits[0]
        self.assertEqual(sent["assertion"], "user")
        self.assertFalse(sent["bot"])
        self.assertEqual(sent["base_revision_id"], 100)
        self.assertEqual(sent["base_timestamp"], "2026-10-01T00:00:00Z")
        self.assertEqual(sent["start_timestamp"], "2026-11-01T00:00:00Z")
        self.assertEqual(sent["token"], "token+\\")

    def test_login_after_approval(self):
        """`bot` グループがあれば、ボットの編集として保存する。"""
        session = FakeSession(groups=("user", "bot"))
        editor = self.editor(session)

        editor.login()
        editor.save(PAGE, "新しい本文", "要約")

        self.assertTrue(editor.approved)
        self.assertEqual(session.edits[0]["assertion"], "bot")
        self.assertTrue(session.edits[0]["bot"])

    def test_rejects_other_user(self):
        """ログインした利用者がログイン名と違えば止める。"""
        with self.assertRaises(MediaWikiError):
            self.editor(FakeSession(name="Someone")).login()

    def test_accepts_lowercase_first_letter(self):
        """利用者名の先頭の小文字は、大文字と同じとみなす。"""
        session = FakeSession()
        editor = Editor(
            FakeEditorApi(session),
            Credentials("fawbot@restore", "secret"),
            self.state_path,
        )

        editor.login()

    def test_records_interval_and_waits_next_time(self):
        """保存したら次に編集してよい時刻を記録し、次は前の実行でも待つ。"""
        session = FakeSession()
        editor = self.editor(session)
        editor.login()

        editor.save(PAGE, "新しい本文", "要約")

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            datetime.fromisoformat(state["next_edit_at"]),
            NOW + timedelta(seconds=70),
        )
        later = self.editor(session, now=NOW + timedelta(seconds=30))
        later.wait_turn()
        self.assertEqual(self.sleeps, [40])

    def test_records_interval_even_when_save_fails(self):
        """保存に失敗しても、次に編集してよい時刻を記録する。"""
        session = FakeSession()
        session.edit = lambda url, **arguments: (_ for _ in ()).throw(
            MediaWikiError("失敗")
        )
        editor = self.editor(session)
        editor.login()

        with self.assertRaises(MediaWikiError):
            editor.save(PAGE, "新しい本文", "要約")
        self.assertTrue(self.state_path.exists())

    def test_no_wait_without_state(self):
        """記録がなければ待たない。"""
        self.editor(FakeSession()).wait_turn()

        self.assertEqual(self.sleeps, [])

    def test_stop_reasons(self):
        """会話ページの新着か、停止用のページへの書き込みがあれば止める。"""
        self.assertIsNone(self.editor(FakeSession()).stop_reason())
        self.assertIsNone(self.editor(FakeSession(), " \n").stop_reason())
        self.assertIn(
            "利用者:Fawbot/停止",
            self.editor(FakeSession(), "止めて").stop_reason(),
        )
        self.assertIn(
            "会話ページ",
            self.editor(FakeSession(messages=True)).stop_reason(),
        )

    def test_save_requires_login(self):
        """ログインしていなければ保存しない。"""
        with self.assertRaises(MediaWikiError):
            self.editor(FakeSession()).save(PAGE, "本文", "要約")


if __name__ == "__main__":
    unittest.main()
