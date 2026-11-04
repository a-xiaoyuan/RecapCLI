"""除终端界面（tests/ui）之外的回归，日常改动先跑这个：python -X utf8 tests/fast.py

UI 测试要启动 Textual，Windows 上很慢；只在改了 recapcli/tui 或 recapcli/ui 时再跑全量。
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(TESTS.parent), str(TESTS)]
GROUPS = ("core", "tools", "storage", "integration", "mcp")

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.discover(str(TESTS / name), top_level_dir=str(TESTS)) for name in GROUPS)
    sys.exit(0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1)
