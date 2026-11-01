"""既定の置き場所。ログイン情報のほかは、このパッケージの直下に置く。"""

from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
CACHE_PATH = PACKAGE_DIRECTORY / "cache" / "cache.sqlite3"
# 次に編集してよい時刻。実行をまたいで編集の間隔を守るため。
EDIT_STATE_PATH = PACKAGE_DIRECTORY / "cache" / "edit_state.json"
# BotPassword のログイン情報。リポジトリ直下に置く。
ENV_PATH = PACKAGE_DIRECTORY.parents[1] / ".env"
INPUT_DIRECTORY = PACKAGE_DIRECTORY / "input"
OUTPUT_DIRECTORY = PACKAGE_DIRECTORY / "output"
