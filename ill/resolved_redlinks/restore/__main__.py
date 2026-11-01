"""`python -m ill.resolved_redlinks.restore` で実行するための入口。"""

from ill.resolved_redlinks.restore.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
