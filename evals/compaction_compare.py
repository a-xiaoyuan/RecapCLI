"""固定长会话的压缩前后对比。不调用真实模型。

token 用引擎自己的 json_tokens 估算，不是服务端 prompt_tokens。
上下文减少比例看同一次追问在压缩前、压缩后实际发出的 messages。

    python -X utf8 evals/compaction_compare.py
    python -X utf8 evals/compaction_compare.py --json docs/compaction-2026-11-07.json
"""

import argparse
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

import httpx2
from openai import OpenAI

from recapcli.base.tokens import json_tokens
from recapcli.core.agent import run_agent
from recapcli.core.compaction import summary_text

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "long_session_2026-11.json"
WORKSPACE = ROOT / "tests" / "fixtures" / "long_session_workspace"
FACTS = ("pkg/status.py", "format_status", "bak-7f3a91")
FOLLOW_UP = "根据会话记录，说明这次修复改了哪个文件的哪个函数，以及编辑备份编号。只根据上下文回答。"
FILLER = "这是会话里的调试日志，只为占满上下文，不包含修复标识。"


def build_fixture_messages():
    """生成入库用的固定会话。关键事实只出现在会被压缩的旧轮次里。"""
    fact = "已修改 pkg/status.py 的函数 format_status，备份编号 bak-7f3a91。根规则缺失时不能写成未找到。"
    filler = (FILLER * 200) + "\n"
    messages = []
    for index in range(8):
        call_id = f"hist-{index}"
        early = index < 6
        tool_body = (filler + (fact if index == 2 else "本段没有修复标识。") + "\n") if early else "没有匹配的临时日志。\n"
        assistant = fact if index == 2 else ("旧轮次已记录，未再改代码。" if early else "未再改代码。")
        user = f"第 {index + 1} 轮：核对当时的记录。" if early else "把临时日志收起来，不要改代码。"
        path = "pkg/status.py" if early else "logs/temporary.txt"
        messages.extend([
            {"role": "user", "content": user},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": path}, ensure_ascii=False)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": tool_body},
            {"role": "assistant", "content": assistant},
        ])
    return messages


def load_fixture(path=FIXTURE):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _client(handler):
    return OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                  http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def _completion(content):
    return httpx2.Response(200, json={
        "id": "offline", "object": "chat.completion", "created": 0, "model": "compaction-compare",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}]})


def _summary_content(payload_text):
    found = [fact for fact in FACTS if fact in payload_text]
    listed = " ".join(found)
    return {
        "goal": "修复规则摘要：根目录没有规则时仍要显示已经加载的子目录规则。",
        "constraints": "不扩大读取范围，不把模型自述当成验收。",
        "decisions": ("上下文里看到的标识：" + listed) if found else "压缩输入里没有看到修复标识。",
        "actions": ("保留这些标识：" + listed) if found else "没有记录到编辑标识。",
        "verification": "会话中的命令输出不能单独当成验收通过。",
        "pending": "继续改代码前要重新读取文件，摘要不能代替当前内容。",
    }


def _compact_handler(captured):
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        payload = body["messages"][-1]["content"]
        return _completion(json.dumps(_summary_content(payload), ensure_ascii=False))
    return handler


def _answer_from_messages(messages):
    blob = json.dumps(messages, ensure_ascii=False)
    missing = [fact for fact in FACTS if fact not in blob]
    if missing:
        return "无法从当前上下文确认：" + "、".join(missing)
    return "修复位于 pkg/status.py 的函数 format_status，备份编号 bak-7f3a91。"


def _follow_handler(captured):
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        return _completion(_answer_from_messages(body["messages"]))
    return handler


def workspace_task_passes(workspace):
    """固定题目：根规则缺失、子规则有效时，摘要要写出子规则名，不能说未找到。"""
    path = Path(workspace) / "pkg" / "status.py"
    spec = importlib.util.spec_from_file_location("long_session_status", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    nested = module.format_status(False, "pkg/AGENTS.md")
    return (nested == "pkg/AGENTS.md · 已加载" and "未找到" not in nested
            and module.format_status(False, "") == "未加载规则"
            and module.format_status(True, "") == "AGENTS.md · 已加载")


def _run(handler, workspace, history, prompt, *, summary=None, compact_only=False, max_steps=1):
    client = _client(handler)
    with client:
        return run_agent(client, "compaction-compare", prompt, workspace, history=history, summary=summary,
                         compact_only=compact_only, max_steps=max_steps, on_event=lambda _message: None,
                         capabilities=("read",))


def compare(fixture_path=FIXTURE, workspace_source=WORKSPACE):
    history = load_fixture(fixture_path)
    compact_requests = []
    with tempfile.TemporaryDirectory() as directory:
        empty = Path(directory) / "empty"
        empty.mkdir()
        code = Path(directory) / "code"
        shutil.copytree(workspace_source, code)
        digest_before = (code / "pkg" / "status.py").read_bytes()
        task_before = workspace_task_passes(code)
        compact = _run(_compact_handler(compact_requests), empty, history, "压缩这段历史", compact_only=True)
        digest_after_compact = (code / "pkg" / "status.py").read_bytes()
        # 压缩发生在另一个目录；再在代码目录上走一遍同样的压缩，确认源文件不被改写。
        _run(_compact_handler([]), code, history, "压缩这段历史", compact_only=True)
        digest_after = (code / "pkg" / "status.py").read_bytes()
        task_after = workspace_task_passes(code)
        follow_requests = []
        follow = _run(_follow_handler(follow_requests), empty, history, FOLLOW_UP,
                      summary=compact.summary, max_steps=1)
        plain_requests = []
        plain = _run(_follow_handler(plain_requests), empty, history, FOLLOW_UP, max_steps=1)
    summary = compact.summary
    covered = summary["covered_messages"]
    slice_before = json_tokens(history[:covered])
    slice_after = json_tokens(summary_text(summary))
    request_before = json_tokens(plain_requests[0]["messages"])
    request_after = json_tokens(follow_requests[0]["messages"])
    payload = compact_requests[0]["messages"][-1]["content"]
    follow_blob = json.dumps(follow_requests[0]["messages"], ensure_ascii=False)
    answer = follow.answer or ""
    return {
        "fixture": str(fixture_path.relative_to(ROOT)),
        "estimator": "recapcli.base.tokens.json_tokens",
        "messages": len(history),
        "covered_messages": covered,
        "slice_tokens_before": slice_before,
        "slice_tokens_after": slice_after,
        "slice_tokens_removed": slice_before - slice_after,
        "request_tokens_before": request_before,
        "request_tokens_after": request_after,
        "request_tokens_removed": request_before - request_after,
        "engine_before_bytes": summary["before_bytes"],
        "engine_after_bytes": summary["after_bytes"],
        "workspace_digest_unchanged": digest_before == digest_after == digest_after_compact,
        "task_pass_before": task_before,
        "task_pass_after": task_after,
        "facts_in_compact_payload": all(fact in payload for fact in FACTS),
        "facts_in_followup_request": all(fact in follow_blob for fact in FACTS),
        "continuation_answer": answer,
        "continuation_pass": all(fact in answer for fact in FACTS),
        "uncompacted_continuation_pass": all(fact in (plain.answer or "") for fact in FACTS),
        "compaction_status": compact.status,
    }


def format_report(result):
    def percent(removed, before):
        return removed / before * 100

    lines = [
        f"会话消息 {result['messages']} 条，压缩覆盖前 {result['covered_messages']} 条。",
        f"被覆盖片段：{result['slice_tokens_before']} → {result['slice_tokens_after']} tokens，"
        f"减少 {percent(result['slice_tokens_removed'], result['slice_tokens_before']):.2f}%。",
        f"同一次追问的请求：{result['request_tokens_before']} → {result['request_tokens_after']} tokens，"
        f"减少 {percent(result['request_tokens_removed'], result['request_tokens_before']):.2f}%。",
        f"引擎记录的字节：{result['engine_before_bytes']} → {result['engine_after_bytes']}。",
        f"代码题压缩前通过：{result['task_pass_before']}；压缩后通过：{result['task_pass_after']}；"
        f"文件字节未变：{result['workspace_digest_unchanged']}。",
        f"压缩后追问通过：{result['continuation_pass']}；未压缩对照通过：{result['uncompacted_continuation_pass']}。",
        f"压缩输入含三处事实：{result['facts_in_compact_payload']}；"
        f"追问请求含三处事实：{result['facts_in_followup_request']}。",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="比较固定长会话压缩前后的上下文")
    parser.add_argument("--json", type=Path, help="把结果写到这个路径")
    args = parser.parse_args(argv)
    result = compare()
    print(format_report(result))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
