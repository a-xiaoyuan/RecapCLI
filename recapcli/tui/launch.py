"""默认交互入口。有终端时打开 Textual 壳，单次提问仍走命令行。"""

import math
import sys
from pathlib import Path

from ..cli import build_parser
from ..cli import main as cli_main
from ..core.agent import AgentLimits
from .driver import ShellOptions


def _tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _interactive_shell(args):
    if args.prompt is not None:
        return False
    if args.models or args.tasks or args.task or args.compact or args.json or args.preview:
        return False
    return True


def shell_options(args):
    if (isinstance(args.approval_timeout, bool) or not isinstance(args.approval_timeout, (int, float))
            or not math.isfinite(args.approval_timeout) or not 0 < args.approval_timeout <= 3600):
        raise ValueError("--approval-timeout 必须是大于 0 且不超过 3600 的秒数")
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        raise ValueError("--workspace 必须是存在的目录")
    max_steps = args.max_steps
    if max_steps is None:
        max_steps = 20 if args.mode in ("plan", "multi") else 8
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        raise ValueError("--max-steps 必须大于 0")
    if args.mode == "plan" and max_steps < 4:
        raise ValueError("计划模式至少需要 4 次模型请求预算")
    if args.mode == "multi" and max_steps < 3:
        raise ValueError("Multi 仅本地只读，至少需要 3 次模型请求预算")
    try:
        limits = AgentLimits(
            max_context_bytes=args.context_bytes, max_tool_calls=args.max_tool_calls,
            max_tool_bytes=args.max_tool_bytes, max_seconds=args.max_seconds,
        )
    except ValueError as error:
        raise ValueError(str(error)) from error
    requested = {"read"}
    if args.allow_web:
        requested.add("web")
    if args.allow_write:
        requested.add("write")
    if args.allow_exec:
        requested.add("exec")
    capabilities = {"read"} if args.mode == "multi" else requested
    return ShellOptions(
        workspace=workspace.resolve(), mode=args.mode, capabilities=tuple(sorted(capabilities)),
        max_steps=max_steps, limits=limits, profile=args.profile, session_id=args.session,
        continue_session=bool(args.continue_), memory=args.memory, auto_compact=args.auto_compact,
        yes=bool(args.yes), approval_timeout=float(args.approval_timeout),
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
    demo = False
    cleaned = []
    for item in argv:
        if item == "--demo":
            demo = True
        else:
            cleaned.append(item)
    try:
        args = build_parser().parse_args(cleaned)
    except SystemExit as error:
        code = error.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    blocked = args.prompt is not None or args.models or args.tasks or args.task or args.compact or args.json or args.preview
    if demo and blocked:
        print("--demo 只启动界面演示，不能和提问、任务查看或 --preview 同时使用。", file=sys.stderr)
        return 2
    if force_shell and blocked:
        print("这些参数属于命令行入口：python -m recapcli.cli …", file=sys.stderr)
        return 2
    if force_shell or demo or (_interactive_shell(args) and _tty()):
        return launch_shell(args, demo=demo)
    return cli_main(cleaned)
