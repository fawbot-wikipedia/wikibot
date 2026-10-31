"""ウィキテキストと差分から、内部リンクと仮リンクを取り出す。"""

import html
import re
from collections import Counter
from collections.abc import Iterable

from wikiutil.ill import parse_ill_template

from ill.resolved_redlinks.parsing.titles import TitleKey, TitleNormalizer

# 先頭1文字を大文字にした名前で比べる（「ill」と「Ill」は同じテンプレート）。
# 「仮リンク」への転送（別名）も含める。
ILL_TEMPLATE_NAMES = {
    "仮リンク",
    "日本語版にない記事リンク",
    "Ill",
    "Ill2",
    "Illm",
    "Interlanguage link",
    "Interlanguage link multi",
    "Link-interwiki",
}

_IGNORED_REGIONS = re.compile(
    r"<!--.*?(?:-->|\Z)"
    r"|<nowiki\s*/>"
    r"|<nowiki(?:\s[^>]*)?>.*?(?:</nowiki\s*>|\Z)"
    r"|<pre(?:\s[^>]*)?>.*?(?:</pre\s*>|\Z)",
    flags=re.DOTALL | re.IGNORECASE,
)
# リンクの開始だけを見る。閉じ括弧まで読むと、画像の説明文の中のリンク
# （`[[File:…|[[リンク]]…]]`）を外側のリンクが飲み込んでしまうため。
_INTERNAL_LINK = re.compile(r"\[\[([^\[\]|{}<>\n]+?)\s*(?=\||\]\])")
_TEMPLATE_OPENER = re.compile(r"\{\{\s*([^|{}]+?)\s*\|")
_DIFF_CELL = re.compile(
    r'<td class="diff-(deleted|added)line[^"]*">(.*?)</td>', flags=re.DOTALL
)


def strip_ignored(text: str) -> str:
    """コメント・`<nowiki>`・`<pre>` の中身を除く（リンクとして数えないため）。"""
    return _IGNORED_REGIONS.sub("", text)


def internal_link_targets(text: str) -> list[str]:
    """本文中の `[[…]]` 形式の内部リンクのリンク先を、出現順に返す。

    テンプレートの引数の中や、画像の説明文の中のリンクも含む。テンプレートが
    作るリンク（`{{Main|…}}` など）は含まない。
    """
    return [match.group(1) for match in _INTERNAL_LINK.finditer(text)]


def _template_name(name: str) -> str:
    """テンプレート名を、`Template:` 接頭辞を除き先頭1文字を大文字にして返す。"""
    name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    if name.casefold().startswith("template:"):
        name = name[len("template:"):].strip()
    return name[:1].upper() + name[1:]


def ill_templates(text: str) -> list[str]:
    """仮リンク系テンプレートの呼び出しを、入れ子も含めて取り出す。

    閉じていないテンプレートは、そこで読むのをやめる（記事の書きかけの
    部分で全体を失敗させないため）。
    """
    templates = []
    position = 0
    while True:
        match = _TEMPLATE_OPENER.search(text, position)
        if match is None:
            break
        if _template_name(match.group(1)) not in ILL_TEMPLATE_NAMES:
            position = match.end()
            continue
        depth = 0
        cursor = match.start()
        end = None
        while cursor < len(text) - 1:
            pair = text[cursor:cursor + 2]
            if pair == "{{":
                depth += 1
                cursor += 2
            elif pair == "}}":
                depth -= 1
                cursor += 2
                if depth == 0:
                    end = cursor
                    break
            else:
                cursor += 1
        if end is None:
            break
        templates.append(text[match.start():end])
        position = end
    return templates


def ill_ja_names(text: str) -> list[str]:
    """仮リンク系テンプレートの日本語版記事名（第1引数）を返す。"""
    names = []
    for template in ill_templates(text):
        ja_name = parse_ill_template(template).get("ja_name", "").strip()
        if ja_name:
            names.append(ja_name)
    return names


def page_link_state(
    text: str, normalizer: TitleNormalizer
) -> tuple[set[TitleKey], set[TitleKey]]:
    """最新版の本文から、内部リンクのリンク先と仮リンクの記事名のキーを返す。"""
    cleaned = strip_ignored(text)
    links = normalizer.keys(internal_link_targets(cleaned))
    ills = normalizer.keys(ill_ja_names(cleaned))
    return links, ills


def parse_compare_body(body: str) -> tuple[list[str], list[str]]:
    """`action=compare` の差分表HTMLから、消えた側・増えた側の行を返す。

    差分表のセルからタグを除き、HTMLの文字参照を戻した文字列にする。
    """
    deleted: list[str] = []
    added: list[str] = []
    for side, cell in _DIFF_CELL.findall(body):
        line = html.unescape(re.sub(r"<[^>]+>", "", cell))
        (deleted if side == "deleted" else added).append(line)
    return deleted, added


def removed_ill_templates(
    deleted: Iterable[str], added: Iterable[str]
) -> list[str]:
    """差分で消えた仮リンクテンプレートを出現順に返す。"""
    before = ill_templates(strip_ignored("\n".join(deleted)))
    after = Counter(ill_templates(strip_ignored("\n".join(added))))
    removed = Counter(before) - after
    result = []
    for template in before:
        if removed[template] > 0:
            result.append(template)
            removed[template] -= 1
    return result


def original_ill_and_wording(
    templates: Iterable[str],
    target_title: str,
    normalizer: TitleNormalizer,
) -> tuple[str, str] | None:
    """対象記事名に一致する元の仮リンクと、実際の表示文字列を返す。"""
    target_key = normalizer.key(target_title)
    for template in templates:
        parameters = parse_ill_template(template)
        japanese_title = parameters.get("ja_name", "").strip()
        if normalizer.key(japanese_title) != target_key:
            continue
        wording = parameters.get("label", "").strip() or japanese_title
        return template, wording
    return None


def added_link_targets(
    deleted: Iterable[str],
    added: Iterable[str],
    normalizer: TitleNormalizer,
) -> tuple[list[TitleKey], int]:
    """差分で新しく増えた内部リンクのリンク先と、解消された仮リンクの数を返す。

    増えた側の行にあって消えた側の行にない内部リンク（同じリンク先が
    複数ある場合は数の差）を、その編集で入れたリンク先とする。仮リンクは、
    消えた側にあって増えた側に残っていないものを解消されたものとして数える。
    """
    deleted_lines = list(deleted)
    added_lines = list(added)
    deleted_text = strip_ignored("\n".join(deleted_lines))
    added_text = strip_ignored("\n".join(added_lines))
    before = Counter(
        key
        for key in map(normalizer.key, internal_link_targets(deleted_text))
        if key is not None
    )
    after = Counter(
        key
        for key in map(normalizer.key, internal_link_targets(added_text))
        if key is not None
    )
    new_links = sorted((after - before).keys())
    resolved_ills = len(removed_ill_templates(deleted_lines, added_lines))
    return new_links, resolved_ills
