"""执行一轮任务，持久化事实并维护可恢复的会话。"""

from dataclasses import asdict, replace
from pathlib import Path
import time
import json

from openai import APIError

from ..core.agent import AgentLimits, DEFAULT_STEPS, run_agent
from ..core.recovery import TurnRecovery
from ..llm.client import ModelClient
from ..storage.tasks import TaskStore, project_version
from ..tools import execute_tool
from .changes import change_report, short_change_report


def save_session(store, session, ui, *, allow_empty=False):
    if not allow_empty and not session["messages"]:
        return True
    try:
        store.save(session)
    except (OSError, ValueError) as error:
        ui.error(f"会话未保存：{error}")
        return False
    except KeyboardInterrupt:
        ui.error("会话保存已取消，消息尚未保存。")
        return False
    return True


def _publish_task(tasks, task_id, ui, *, show_changes=False):
    try:
        task = tasks.get(task_id)
    except (OSError, ValueError):
        return
    receiver = getattr(ui, "task_update", None)
    if receiver is not None:
        receiver(task)
    if show_changes:
        report = change_report(task)
        if report["files"] or report["commands"]:
            ui.info(short_change_report(report))


def run_turn(client, model, workspace, store, ui, session, prompt, *,
             max_steps=None, memory_enabled=True, limits=None, capabilities=("read",), mode="react",
             auto_compact=True, compact_only=False, cancel_event=None, approver=None):
    """先落任务，再执行；失败轨迹独立于成功会话，核心不依赖存储。"""
    limits = limits or AgentLimits()
    model_config = client.metadata() if isinstance(client, ModelClient) else None
    if model_config is not None:
        limits = replace(limits, max_context_bytes=min(limits.max_context_bytes, model_config["context_bytes"]))
    capabilities = tuple(sorted(capabilities))
    tasks = TaskStore(workspace)
    if compact_only:
        max_steps = 1
    task_id = tasks.start(session["id"], prompt, model, "compact" if compact_only else mode, {
        "history_messages": len(session["messages"]), "capabilities": capabilities,
        "limits": asdict(limits), "max_steps": max_steps if max_steps is not None else DEFAULT_STEPS[mode],
        "provider_host": str(getattr(getattr(client, "base_url", None), "host", "unknown")),
        "experience_versions": [], "skill_versions": [],
        "auto_compact": auto_compact, "summary_covered_messages": (session.get("summary") or {}).get("covered_messages", 0),
        "model_config": model_config,
    })
    started = time.monotonic()
    recovery = TurnRecovery(limits.max_tool_bytes)

    def trace(kind, data):
        recovery.observe(kind, data)
        try:
            tasks.append(task_id, kind, data)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"任务 {task_id} 的记录失败，停止后续执行：{error}") from error
        if kind == "project_rules" and not data.get("agent"):
            publish_task()

    def publish_task(show_changes=False):
        _publish_task(tasks, task_id, ui, show_changes=show_changes)

    try:
        publish_task()
        ui.info(f"任务 ID：{task_id}（/task ID 查看事实）")
        trace("project_version", project_version(Path(workspace).resolve()))
        memory_text = ""
        memory_ids = []
        memory_sources = []
        if memory_enabled and not compact_only:
            memory = store.memory_context(limit=min(3000, limits.max_context_bytes // 12), query=prompt)
            memory_text = memory.text
            memory_ids = list(memory.selected_ids)
            memory_sources = list(memory.sources)
            if memory.selected_ids or memory.omitted_count:
                ui.info(f"长期记忆：本轮选用 {len(memory.selected_ids)} 条，省略 {memory.omitted_count} 条。")
        trace("context", {"memory_ids": memory_ids, "memory_text": memory_text, "memory_sources": memory_sources})
        result = run_agent(client, model, prompt, workspace, history=session["messages"],
                           memory_text=memory_text, max_steps=max_steps, limits=limits, on_event=ui.event,
                           capabilities=capabilities, mode=mode, on_trace=trace,
                           summary=session.get("summary"), auto_compact=auto_compact, compact_only=compact_only,
                           cancel_event=cancel_event, approver=approver,
                           on_stream=getattr(ui, "stream_update", None) if isinstance(client, ModelClient) else None)
    except BaseException as error:
        if getattr(ui, "stream_update", None) is not None:
            ui.stream_update("abort", "")
        status = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        reason = f"模型请求失败（{type(error).__name__}）" if isinstance(error, APIError) else f"{type(error).__name__}: {error}"
        try:
            trace("error", {"error_type": type(error).__name__, "reason": reason})
            tasks.finish(task_id, status, reason, duration=time.monotonic() - started)
        except (OSError, ValueError, RuntimeError) as record_error:
            ui.error(f"任务 {task_id} 的终态未保存：{record_error}；请核对现有操作记录，勿重复执行。")
        publish_task(show_changes=True)
        recovered_messages = recovery.finish(task_id)
        if recovered_messages:
            session["messages"].extend(recovered_messages)
            session["model"] = model
            if model_config is not None:
                session["model_profile"] = model_config["profile"]
            saved = save_session(store, session, ui)
            ui.info(f"已保留本轮工具事实{'并保存会话' if saved else '在内存中，请用 /save 重试'}；任务未完成，未重放操作。")
        raise
    try:
        tasks.finish(task_id, result.status, result.stop_reason, answer=result.answer,
                     duration=time.monotonic() - started)
    except (OSError, ValueError) as error:
        ui.error(f"任务 {task_id} 的终态未保存：{error}；回答仍保留，请勿重复执行。")
    publish_task(show_changes=True)
    session["messages"].extend(result.messages)
    session["model"] = model
    if model_config is not None:
        session["model_profile"] = model_config["profile"]
    if result.summary is not None:
        session["summary"] = {**result.summary, "task_id": task_id}
    return result.answer


def run_undo(store, ui, session, model, *, group_id=None, capabilities=("read",), approver=None):
    """手动撤销走现有工具权限和审批，不请求模型；操作事实独立落任务。"""
    tasks = TaskStore(store.workspace)
    prompt = "/undo" + (" " + group_id if group_id else "")
    task_id = tasks.start(session.get("id", ""), prompt, model, "undo", {"capabilities": list(capabilities)})
    ui.info(f"任务 ID：{task_id}（本地撤销，不请求模型）")
    started = time.monotonic()
    params = {"group_id": group_id} if group_id else {}
    action = {"action_id": task_id, "call_id": "manual-undo", "name": "undo_edit"}
    try:
        tasks.append(task_id, "project_version", project_version(Path(store.workspace).resolve()))
        tasks.append(task_id, "tool_start", {**action, "arguments": json.dumps(params)})
        _publish_task(tasks, task_id, ui)
        result = execute_tool("undo_edit", params, store.workspace, capabilities=capabilities, approver=approver,
                              on_approval=lambda payload: tasks.append(task_id, "approval", {**action, **payload}))
        tasks.append(task_id, "tool_result", {**action, "result": result})
        tasks.finish(task_id, "returned" if result["ok"] else "blocked",
                     result.get("error") or "本地撤销完成，未调用模型", answer=result.get("summary", ""),
                     duration=time.monotonic() - started)
    except BaseException as error:
        try:
            tasks.append(task_id, "tool_interrupted", {**action, "error_type": type(error).__name__, "outcome": "unknown"})
            tasks.finish(task_id, "cancelled" if isinstance(error, KeyboardInterrupt) else "failed", str(error),
                         duration=time.monotonic() - started)
        except (OSError, ValueError) as record_error:
            ui.error(f"撤销任务 {task_id} 的终态未保存：{record_error}；请先核对文件和备份。")
        _publish_task(tasks, task_id, ui, show_changes=True)
        raise
    _publish_task(tasks, task_id, ui, show_changes=True)
    if session.get("id"):
        session["messages"].extend([{"role": "user", "content": prompt},
                                    {"role": "assistant", "content": "本地操作事实：" + json.dumps(result, ensure_ascii=False)}])
        save_session(store, session, ui)
    return result
