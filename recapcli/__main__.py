"""通过 python -m recapcli 启动。交互终端进入 Textual 壳。"""

import sys

from .tui.launch import dispatch

if __name__ == "__main__":
    sys.exit(dispatch())
