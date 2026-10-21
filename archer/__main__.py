"""通过 python -m archer 启动命令行。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
