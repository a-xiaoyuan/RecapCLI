"""RecapCLI 入口。交互终端进入 Textual 壳，单次提问仍走命令行。"""

import sys

from recapcli.tui.launch import dispatch

if __name__ == "__main__":
    sys.exit(dispatch())
