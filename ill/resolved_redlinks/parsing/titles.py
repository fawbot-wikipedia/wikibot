"""ページ名をMediaWikiと同じ規則で正規化し、比較用のキーを作る。"""

import html
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# 日本語版で使う主な名前空間。siteinfo を取得できないテストなどで使う。
DEFAULT_NAMESPACES = {
    0: "",
    1: "ノート",
    2: "利用者",
    3: "利用者‐会話",
    4: "Wikipedia",
    5: "Wikipedia‐ノート",
    6: "ファイル",
    7: "ファイル‐ノート",
    8: "MediaWiki",
    9: "MediaWiki‐ノート",
    10: "Template",
    11: "Template‐ノート",
    12: "Help",
    13: "Help‐ノート",
    14: "Category",
    15: "Category‐ノート",
    100: "Portal",
    101: "Portal‐ノート",
    102: "プロジェクト",
    103: "プロジェクト‐ノート",
    118: "Draft",
    119: "Draft‐ノート",
    828: "モジュール",
    829: "モジュール‐ノート",
}
DEFAULT_ALIASES = {
    "画像": 6,
    "image": 6,
    "file": 6,
    "user": 2,
    "project": 4,
    "wp": 4,
    "テンプレート": 10,
    "カテゴリ": 14,
    "module": 828,
}


@dataclass(frozen=True, order=True)
class TitleKey:
    """正規化したページ名。名前空間番号と、接頭辞を除いた名前を持つ。"""

    namespace: int
    name: str


class TitleNormalizer:
    """名前空間の名前と別名を使って、ページ名を正規化する。"""

    def __init__(
        self,
        namespaces: Mapping[int, str] | None = None,
        aliases: Mapping[str, int] | None = None,
    ) -> None:
        """名前空間番号ごとの正式名と、別名から番号への対応を受け取る。"""
        self.namespaces = dict(
            DEFAULT_NAMESPACES if namespaces is None else namespaces
        )
        self._prefixes: dict[str, int] = {}
        for number, name in self.namespaces.items():
            if name:
                self._prefixes[_prefix_key(name)] = number
        for alias, number in (
            DEFAULT_ALIASES if aliases is None else aliases
        ).items():
            self._prefixes[_prefix_key(alias)] = number

    @classmethod
    def from_siteinfo(cls, data: Mapping[str, Any]) -> "TitleNormalizer":
        """`meta=siteinfo&siprop=namespaces|namespacealiases` の応答から作る。

        Raises:
            ValueError: 応答に名前空間の情報がない場合。
        """
        query = data.get("query", {})
        namespace_items = query.get("namespaces")
        if not namespace_items:
            raise ValueError("siteinfoの応答に名前空間の情報がありません")
        if isinstance(namespace_items, Mapping):
            namespace_items = namespace_items.values()
        namespaces = {}
        for item in namespace_items:
            number = int(item["id"])
            namespaces[number] = item.get("name", "")
        aliases = {}
        for item in namespace_items:
            canonical = item.get("canonical")
            if canonical:
                aliases[canonical] = int(item["id"])
        for item in query.get("namespacealiases", []):
            aliases[item["alias"]] = int(item["id"])
        return cls(namespaces, aliases)

    def key(self, title: str) -> TitleKey | None:
        """ページ名またはリンク先から比較用のキーを作る。

        `_` を空白にし、空白をまとめ、`#節` と先頭の `:` を除き、名前空間
        接頭辞を番号にして、名前の先頭1文字だけを大文字にする。名前が空に
        なる場合（節だけへのリンクなど）は None を返す。
        """
        text = html.unescape(title).replace("_", " ")
        text = text.split("#", 1)[0]
        text = re.sub(r"\s+", " ", text).strip()
        if text.startswith(":"):
            text = text[1:].strip()
        namespace = 0
        if ":" in text:
            prefix, rest = text.split(":", 1)
            number = self._prefixes.get(_prefix_key(prefix))
            if number is not None:
                namespace = number
                text = rest.strip()
        if not text:
            return None
        return TitleKey(namespace, text[0].upper() + text[1:])

    def dump_key(self, namespace: int, db_title: str) -> TitleKey:
        """ダンプの `page_namespace` と `page_title` から比較用のキーを作る。"""
        text = db_title.replace("_", " ")
        return TitleKey(namespace, text[:1].upper() + text[1:])

    def display(self, key: TitleKey) -> str:
        """キーを、名前空間接頭辞付きのページ名に戻す。"""
        prefix = self.namespaces.get(key.namespace, "")
        return "{}:{}".format(prefix, key.name) if prefix else key.name

    def keys(self, titles: Iterable[str]) -> set[TitleKey]:
        """複数のページ名のキーを、空のものを除いて集める。"""
        return {key for key in map(self.key, titles) if key is not None}


def _prefix_key(prefix: str) -> str:
    """名前空間接頭辞を、大文字・小文字と空白の違いを無視して比べる形にする。"""
    return re.sub(r"[\s_]+", " ", prefix).strip().casefold()
