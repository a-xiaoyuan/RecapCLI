"""Archer 入口：只负责启动 CLI。"""

import sys

from archer.cli import main

if __name__ == "__main__":
    sys.exit(main())
