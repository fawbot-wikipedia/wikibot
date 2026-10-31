"""既定の置き場所。いずれもこのパッケージの直下に置く。"""

from pathlib import Path

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
CACHE_PATH = PACKAGE_DIRECTORY / "cache" / "cache.sqlite3"
INPUT_DIRECTORY = PACKAGE_DIRECTORY / "input"
OUTPUT_DIRECTORY = PACKAGE_DIRECTORY / "output"
