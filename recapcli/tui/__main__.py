"""启动极简终端预览：uv run --extra tui python -m recapcli.tui [--demo]。"""

import argparse
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recapcli.tui", description="RecapCLI 极简终端预览（不请求模型）")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="只读展示此工作区的会话")
    parser.add_argument("--demo", action="store_true", help="展示固定对话与光标演示，不请求模型")
    args = parser.parse_args(argv)
    if not args.workspace.is_dir():
        parser.error("--workspace 必须是存在的目录")
    try:
        from .app import RecapShell
    except ModuleNotFoundError as error:
        if (error.name or "").split(".")[0] != "textual":
            raise
        print("终端壳需要可选依赖 textual：uv sync --extra tui", file=sys.stderr)
        return 2
    from .state import load_shell_state
    RecapShell(load_shell_state(args.workspace), demo=args.demo).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
