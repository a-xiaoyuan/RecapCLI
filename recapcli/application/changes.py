"""由 Git 起始状态和工具事实生成可查看的修改报告。"""

import json
import os
import shlex
from subprocess import list2cmdline


def change_report(task):
    git = None
    files, groups, commands, starts = {}, [], [], {}
    command_starts, completed, records = {}, set(), {}
    for event in task.get("events", []):
        data = event.get("data", {})
        if data.get("agent"):
            continue
        kind = event.get("kind")
        if kind == "project_version":
            git = data
        elif kind == "tool_start":
            try:
                arguments = json.loads(data["arguments"])
                starts[data["action_id"]] = arguments if isinstance(arguments, dict) else {}
                if data.get("name") == "run_command":
                    command_starts[data["action_id"]] = starts[data["action_id"]]
            except (KeyError, TypeError, ValueError):
                pass
        elif kind == "artifact" and data.get("record_type") == "command":
            records[data.get("action_id")] = data.get("record_id")
        elif kind == "tool_result":
            name, result = data.get("name"), data.get("result", {})
            if name == "run_command":
                completed.add(data.get("action_id"))
                args = starts.get(data.get("action_id"), {})
                argv = args.get("argv", [])
                argv = argv if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv) else []
                commands.append({"argv": argv, "cwd": args.get("cwd", "."),
                                 "returncode": result.get("returncode"), "ok": result.get("ok", False),
                                 "error": result.get("error", ""), "record_id": result.get("record_id")})
            if name not in {"edit_file", "edit_files", "restore_file", "undo_edit"}:
                continue
            changed = [item for item in result.get("files", [result]) if item.get("ok", True) and
                       (item.get("changed") or item.get("restored")) and isinstance(item.get("path"), str)]
            if not changed:
                continue
            restored = name in {"restore_file", "undo_edit"}
            for item in changed:
                files[item["path"]] = {"path": item["path"], "restored": restored}
            group = result.get("group_id") or result.get("backup_id")
            groups.append({"id": group, "files": [item["path"] for item in changed],
                           "undoable": bool(group and not restored), "restored": restored,
                           "diff": result.get("diff", ""), "truncated": result.get("diff_truncated", False)})
    for action_id, args in command_starts.items():
        if action_id not in completed:
            argv = args.get("argv", [])
            argv = argv if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv) else []
            commands.append({"argv": argv, "cwd": args.get("cwd", "."), "returncode": None,
                             "ok": False, "error": "结果未知，请核对命令记录；不会自动重放", "record_id": records.get(action_id)})
    previous = {item["path"] for item in (git or {}).get("files", [])}
    known = git is not None and git.get("dirty") is not None and not git.get("files_truncated")
    for path, item in files.items():
        item["preexisting"] = True if path in previous else (False if known else None)
    return {"git": git, "files": list(files.values()), "groups": groups, "commands": commands}


def git_summary(snapshot):
    if snapshot is None or snapshot.get("dirty") is None:
        return "Git 状态不可用"
    entries = snapshot.get("files", [])
    lines = [f"{item['status']} {item['path']}" for item in entries]
    if snapshot.get("files_truncated"):
        lines.append("…起始路径列表已截断")
    return "\n".join(lines) or ("开始时已有改动，未记录路径" if snapshot["dirty"] else "开始时工作区干净")


def files_summary(report):
    labels = {True: "开始时已有改动", False: "开始时 Git 未标记改动", None: "起始 Git 状态未知"}
    return "\n".join(f"{item['path']} · {'已恢复' if item['restored'] else '已编辑'} · {labels[item['preexisting']]}"
                     for item in report["files"]) or "暂无编辑工具修改"


def commands_summary(report):
    lines = []
    for command in report["commands"]:
        outcome = f"退出码 {command['returncode']}" if command["returncode"] is not None else "未获得退出码"
        if not command["ok"]:
            outcome += " · " + (command["error"] or "未正常完成")
        shown = list2cmdline(command["argv"]) if os.name == "nt" else shlex.join(command["argv"])
        lines.append(f"{shown} · {outcome}")
    return "\n".join(lines) or "本轮未执行命令"


def short_change_report(report):
    groups = [group for group in report["groups"] if group["undoable"]]
    lines = [f"本轮记录：{len(report['files'])} 个编辑 / 恢复文件，{len(report['commands'])} 条命令；/diff 查看差异与结果。"]
    if groups:
        lines.append("撤销最近操作组：/undo；选择指定组：/undo 组ID。")
    return "\n".join(lines)


def show_changes(ui, tasks, task_id=None):
    if task_id is None:
        recent = tasks.list_tasks(limit=1)
        if not recent:
            ui.info("暂无任务修改记录。")
            return
        task_id = recent[0]["id"]
    task = tasks.get(task_id)
    report = change_report(task)
    ui.info(f"任务 {task_id}\n开始时的 Git 状态（左列暂存区，右列工作区）：\n" + git_summary(report["git"]))
    ui.info("编辑工具记录的本轮文件：\n" + files_summary(report))
    for group in report["groups"]:
        label = ("恢复" if group["restored"] else "编辑") + "操作组"
        ui.info(f"{label} · /undo {group['id']}" if group["undoable"] else label + "：" + (group["id"] or "未记录"))
        ui.info(group["diff"] or "未保留差异正文，请核对任务中的备份记录。")
        if group["truncated"]:
            ui.info("差异已截断，不能据此判断完整修改范围。")
    ui.info("本轮命令记录：\n" + commands_summary(report))
    ui.info("差异与撤销覆盖编辑工具的记录；命令造成的文件变化需另行核对。命令退出码不等于完整任务验收。")
