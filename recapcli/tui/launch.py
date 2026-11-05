"""默认交互入口。有终端时打开 Textual 壳，单次提问仍走命令行。"""

import sys
from pathlib import Path

from ..application.options import (capabilities_from, check_approval_timeout, limits_from, max_steps_from,
                                   verify_command_from)
from ..cli import build_parser
from ..cli import main as cli_main
from .driver import ShellOptions


def _tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def shell_options(args):
    if args.banner != "auto" or args.color != "auto":
        raise ValueError("--banner/--color 用于行式 CLI：python -m recapcli.cli；TUI 演示请去掉这两个参数")
    check_approval_timeout(args.approval_timeout)
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        raise ValueError("--workspace 必须是存在的目录")
    max_steps = max_steps_from(args)
    limits = limits_from(args)
    return ShellOptions(
        workspace=workspace.resolve(), mode=args.mode, capabilities=tuple(sorted(capabilities_from(args))),
        additional_dirs=tuple(args.add_dir),
        max_steps=max_steps, limits=limits, profile=args.profile, session_id=args.session,
        continue_session=bool(args.continue_), memory=args.memory, auto_compact=args.auto_compact,
        yes=bool(args.yes), approval_timeout=float(args.approval_timeout),
        mcp_repo=bool(args.mcp_repo), mcp_config=args.mcp_config, verify_command=verify_command_from(args),
    )


def launch_shell(args, *, demo=False) -> int:
    try:
        options = shell_options(args)
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    try:
        from .app import RecapShell
        from .state import load_shell_state
    except ModuleNotFoundError as error:
        if (error.name or "").split(".")[0] != "textual":
            raise
        print("终端壳需要可选依赖 textual：uv sync --extra tui\n降级入口：python -m recapcli.cli", file=sys.stderr)
        return 2
    if not _tty():
        print("终端壳需要交互式终端。\n降级入口：python -m recapcli.cli", file=sys.stderr)
        return 2
    state = load_shell_state(options.workspace)
    RecapShell(state, demo=demo, options=options).run()
    return 0


def dispatch(argv=None, *, force_shell=False) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(item in {"-h", "--help"} for item in argv):
        return cli_main(argv)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as error:
        code = error.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    demo = args.demo
    blocked = args.prompt is not None or args.models or args.tasks or args.task or args.compact or args.json or args.mcp_tools
    if demo and blocked:
        print("--demo 只启动界面演示，不能和提问或任务查看同时使用。", file=sys.stderr)
        return 2
    if force_shell and blocked:
        print("这些参数属于命令行入口：python -m recapcli.cli …", file=sys.stderr)
        return 2
    if not force_shell and not demo and (args.banner != "auto" or args.color != "auto"):
        return cli_main(argv)
    if force_shell or demo or (not blocked and _tty()):
        return launch_shell(args, demo=demo)
    return cli_main(argv)
