"""複数のテストで使う、編集とAPIの代わりの作り方。"""

from ill.resolved_redlinks.sources.cache import Edit


def make_edit(revid, title, timestamp, comment, pageid=None, namespace=0):
    """テスト用の編集を作る。"""
    return Edit(
        revid=revid,
        parentid=revid - 1,
        pageid=pageid if pageid is not None else revid,
        namespace=namespace,
        title=title,
        timestamp=timestamp,
        comment=comment,
    )


class FakeApi:
    """要求の内容に応じて決まった応答を返すAPI。"""

    def __init__(self, responses):
        """要求を受け取って応答を返す関数を受け取る。"""
        self.responses = responses
        self.calls = []

    def call(self, params):
        """要求を記録し、応答を返す。"""
        self.calls.append(dict(params))
        return self.responses(params)

    def query_pages(self, params):
        """`continue` のない1回分の応答を返す。"""
        yield self.call({"action": "query", **params})

