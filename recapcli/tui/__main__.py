"""启动终端壳：uv run --extra tui python -m recapcli.tui [--demo]。"""

import sys

from .launch import dispatch


def main(argv=None) -> int:
    return dispatch(argv, force_shell=True)


if __name__ == "__main__":
    sys.exit(main())
