"""扩展套件的外部验收。模型由 MockTransport 模拟，MCP 服务仅监听回环地址。"""

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from openai import APIStatusError, OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import check, main as m2_acceptance, require  # noqa: E402

M2_TASKS = {"bugfix", "feature"}
SKILL_MARKER = "证据核对的专用步骤_加载后可见"


def completion(content=None, *, tool=None, arguments=None, call_id="fixture-call"):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": call_id, "type": "function",
                                  "function": {"name": tool, "arguments": json.dumps(arguments or {})}}]
    return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
                           "model": "offline", "choices": [{"index": 0, "message": message,
                           "finish_reason": "tool_calls" if tool else "stop"}]})


def edit(before, after):
    return completion(tool="edit_file", call_id=f"edit-{after}",
                      arguments={"path": "state.txt", "old_text": before, "new_text": after})


def outage():
    return httpx2.Response(502, json={"error": {"message": "fixture gateway failure"}})


@contextmanager
def offline_model(replies):
    requests = []
    queue = iter(replies)

    def handle(request):
        requests.append(json.loads(request.content))
        response = next(queue)
        if response is None:
            raise httpx2.ConnectError("fixture disconnect", request=request)
        return response

    with OpenAI(api_key="offline-fixture", base_url="https://offline.invalid/v1", max_retries=0,
                http_client=httpx2.Client(transport=httpx2.MockTransport(handle))) as client:
        yield client, requests


def run_turn(workspace, client, traces, **options):
    from recapcli.core.agent import AgentLimits, run_agent
    from recapcli.tools.approval import approve_all

    return run_agent(client, "offline", "处理状态文件", workspace, capabilities=("read", "write"),
                     approver=approve_all, limits=AgentLimits(max_retries=0), on_event=lambda _: None,
                     on_trace=lambda kind, data: traces.append((kind, data)), **options)


def outage_after_tool(replies, max_steps=3):
    with tempfile.TemporaryDirectory() as directory, offline_model(replies) as (client, requests):
        workspace = Path(directory)
        (workspace / "state.txt").write_text("old", encoding="utf-8")
        traces = []
        result = run_turn(workspace, client, traces, max_steps=max_steps)
        require(result.status == "incomplete" and bool(result.answer.strip()), "故障后缺少未完成说明")
        require((workspace / "state.txt").read_text(encoding="utf-8") == "saved", "编辑结果丢失")
        require(sum(kind == "tool_result" for kind, _ in traces) == 1, "工具结果缺失或被重复执行")
        require(2 <= len(requests) <= max_steps, "请求次数超出场景或预算")
        if len(requests) == 3:
            require(not requests[-1].get("tools"), "汇总请求仍带工具")


def outage_before_tool():
    with tempfile.TemporaryDirectory() as directory, offline_model([outage()]) as (client, _):
        try:
            run_turn(Path(directory), client, [])
        except APIStatusError as error:
            require(error.status_code == 502, "错误状态码发生变化")
            return
    raise AssertionError("工具执行前的故障未向上报告")


def check_outage(checks):
    check(checks, "编辑后网关故障收尾，汇总请求不带工具",
          lambda: outage_after_tool([edit("old", "saved"), outage(), completion("文件已修改，后续未完成")]))
    check(checks, "编辑后断连且汇总失败时用已存事实收尾",
          lambda: outage_after_tool([edit("old", "saved"), None, None]))
    check(checks, "没有汇总请求额度时以已有事实收尾",
          lambda: outage_after_tool([edit("old", "saved"), outage()], max_steps=2))
    check(checks, "工具执行前的网关故障仍报告错误", outage_before_tool)


def verification_cli():
    from recapcli import cli
    from recapcli.config import Config

    with tempfile.TemporaryDirectory() as directory, offline_model([
            edit("old", "fixed"), completion("已修改"),
    ]) as (client, _):
        workspace = Path(directory)
        (workspace / "state.txt").write_text("old", encoding="utf-8")
        script = workspace / "validator.py"
        script.write_text('from pathlib import Path\nPath("validated").write_bytes(Path("state.txt").read_bytes())\n',
                          encoding="utf-8")
        output, errors = StringIO(), StringIO()
        with (
            patch.object(cli, "load_config", return_value=Config("offline-fixture", "offline")),
            patch.object(cli, "ModelClient", return_value=nullcontext(client)),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = cli.main(["处理状态文件", "--workspace", str(workspace), "--allow-write", "--yes", "--no-memory",
                             "--banner", "off", "--color", "never", "--verify-cmd", f'"{sys.executable}" "{script}"'])
        require(code == 0, f"行式 CLI 运行失败：{errors.getvalue()}")
        require((workspace / "validated").read_bytes() == b"fixed", "CLI 未把验证命令接到编辑后的结束检查")


def verification(repair):
    script = """from pathlib import Path
value = Path("state.txt").read_text(encoding="utf-8")
with Path("verified-values").open("a", encoding="utf-8") as log:
    print(value, file=log)
print("checker-value", value)
raise SystemExit(0 if value == "fixed" else 3)
"""
    replies = [edit("old", "wrong"), completion("完成")]
    replies += [edit("wrong", "fixed"), completion("已修正")] if repair else [completion("完成")] * 2
    with tempfile.TemporaryDirectory() as directory, offline_model(replies) as (client, requests):
        workspace = Path(directory)
        (workspace / "state.txt").write_text("old", encoding="utf-8")
        traces = []
        result = run_turn(workspace, client, traces, max_steps=8, verify_command=[sys.executable, "-c", script])
        require(len(requests) >= 3, "第一次失败后没有反馈给模型")
        require("checker-value wrong" in json.dumps(requests[2]["messages"], ensure_ascii=False),
                "模型没有收到验证命令的实际输出")
        expected = "returned" if repair else "incomplete"
        require(result.status == expected, f"预期 {expected}，实际 {result.status}")
        observed = (workspace / "verified-values").read_text(encoding="utf-8").splitlines()
        require(observed == (["wrong", "fixed"] if repair else ["wrong"] * 3), "实际复验内容或次数不符")


def read_only_verification():
    with tempfile.TemporaryDirectory() as directory, offline_model([completion("只读结论")]) as (client, requests):
        workspace = Path(directory)
        script = 'from pathlib import Path; Path("validator-ran").touch()'
        result = run_turn(workspace, client, [], max_steps=4, verify_command=[sys.executable, "-c", script])
        require(result.status == "returned" and len(requests) == 1, "只读轮次没有正常结束")
        require(not (workspace / "validator-ran").exists(), "没有编辑时运行了验证命令")


def check_verification(checks):
    check(checks, "行式 CLI 把 --verify-cmd 接到编辑后的结束检查", verification_cli)
    check(checks, "持续验证失败时文字答复无法绕过", lambda: verification(False))
    check(checks, "编辑修复后通过复验", lambda: verification(True))
    check(checks, "只读轮次不运行验证命令", read_only_verification)


def skill_files(workspace):
    for source, body in ((".agents", SKILL_MARKER), (".claude", "被覆盖的另一份技能正文")):
        skill = workspace / source / "skills" / "evidence-review" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: evidence-review\ndescription: 检查结论证据\n---\n\n{body}\n", encoding="utf-8")
    invalid = workspace / ".agents" / "skills" / "broken" / "SKILL.md"
    invalid.parent.mkdir()
    invalid.write_text("缺少技能头部的正文", encoding="utf-8")


def skill_run(name):
    from recapcli.application.runtime import run_turn as run_application_turn
    from recapcli.storage.session import SessionStore

    with tempfile.TemporaryDirectory() as directory, offline_model([
            completion(tool="load_skill", arguments={"name": name}), completion("已核对证据"),
    ]) as (client, requests):
        workspace = Path(directory)
        skill_files(workspace)
        store = SessionStore(workspace)
        ui = SimpleNamespace(info=lambda _: None, event=lambda _: None, error=lambda _: None)
        run_application_turn(client, "offline", workspace, store, ui, store.new_session("offline"),
                             "检查项目结论的证据", max_steps=4, memory_enabled=False)
        require(len(requests) == 2, "没有形成一次技能工具往返")
        initial = requests[0]["messages"][0]["content"]
        require("evidence-review" in initial and "检查结论证据" in initial, "初始提示缺少技能目录")
        require(all(secret not in initial for secret in (SKILL_MARKER, "被覆盖的另一份", "缺少技能头部")),
                "初始提示暴露了正文")
        require("load_skill" in {entry["function"]["name"] for entry in requests[0].get("tools", [])},
                "模型看不到技能工具")
        return json.loads(next(item["content"] for item in requests[1]["messages"] if item["role"] == "tool"))


def load_existing_skill():
    result = skill_run("evidence-review")
    require(result.get("ok") and SKILL_MARKER in json.dumps(result, ensure_ascii=False), "正文没有按需返回")
    require("被覆盖的另一份" not in json.dumps(result, ensure_ascii=False), "同名技能优先级不正确")


def load_invalid_skill():
    result = skill_run("broken")
    require(result.get("ok") is False and bool(result.get("error")), "损坏技能应返回可读错误")


def check_skills(checks):
    check(checks, "技能目录、延迟加载和项目目录优先级", load_existing_skill)
    check(checks, "损坏技能跳过且加载失败可见", load_invalid_skill)


class LocalMCP(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture", "version": "1"}}
            return self.reply(request, result, session=True)
        if (self.headers.get("Mcp-Session-Id") != "fixture-session"
                or self.headers.get("MCP-Protocol-Version") != "2025-06-18"):
            self.send_error(400)
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "tools/list":
            tools = [{"name": name, "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}
                     for name in ("lookup", "save")]
            return self.reply(request, {"tools": tools})
        if method == "tools/call":
            self.server.calls.append(request["params"])
            variant = request["params"]["arguments"].get("variant")
            if variant == "gateway":
                self.send_error(502)
                return
            result = {"structuredContent": {"answer": variant}, "content": [{"type": "text", "text": variant or ""}]}
            return self.reply(request, result, sse=variant == "sse")
        self.send_error(400)

    def reply(self, request, result, *, session=False, sse=False):
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode("utf-8")
        self.send_response(200)
        if session:
            self.send_header("Mcp-Session-Id", "fixture-session")
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        if sse:
            body = b": ready\n\ndata: " + body + b"\n\n"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def mcp_fixture():
    from recapcli.mcp.manager import MCPManager

    with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), LocalMCP) as server:
        server.calls = []
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = Path(directory)
            path = root / "mcp.json"
            path.write_text(json.dumps({"servers": {"probe": {"url": f"http://127.0.0.1:{server.server_port}/mcp",
                            "timeout": 3, "tool_permissions": {"lookup": "read", "save": "write"}}}}), encoding="utf-8")
            with MCPManager.from_options(root, config_path=path) as manager:
                yield manager, server.calls
        finally:
            server.shutdown()
            thread.join()


def mcp_responses():
    with mcp_fixture() as (manager, _):
        require("mcp_probe_lookup" in {item["function"]["name"] for item in manager.definitions}, "工具发现失败")
        for variant in ("json", "sse"):
            response = manager.call("mcp_probe_lookup", {"variant": variant})
            visible = [item.get("text") for item in response.get("content", []) if item.get("type") == "text"]
            require(response.get("ok") and variant in [response.get("answer"), *visible],
                    f"{variant} 响应没有到达调用方")


def mcp_permissions():
    from recapcli.tools.approval import approve_all, REJECTED

    with mcp_fixture() as (manager, calls):
        approvals = []
        result = manager.call("mcp_probe_save", {}, capabilities=("read", "write"), approver=lambda _: REJECTED,
                              on_approval=approvals.append)
        require(not result.get("ok") and len(approvals) == 1 and not calls, "写调用未遵循本地拒绝决定")
        result = manager.call("mcp_probe_save", {"variant": "approved"}, capabilities=("read", "write"),
                              approver=approve_all)
        require(result.get("ok") and len(calls) == 1 and calls[0]["name"] == "save", "审批后写调用未正确执行")


def mcp_gateway():
    with mcp_fixture() as (manager, calls):
        result = manager.call("mcp_probe_lookup", {"variant": "gateway"})
        require(result.get("ok") is False and result.get("status") == "unknown", "网关故障没有保留结果未知")
        require(len(calls) == 1, "网关故障后重发了工具调用")


def mcp_stdio():
    from recapcli.mcp import __file__ as package_file
    from recapcli.mcp.manager import MCPManager

    source = Path(package_file).resolve().parents[2]
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "state.txt").write_text("stdio-fixture-value", encoding="utf-8")
        config = workspace / "mcp.json"
        config.write_text(json.dumps({"servers": {"probe": {
            "command": [sys.executable, "-X", "utf8", "-m", "recapcli.mcp.repo_server",
                        "--workspace", str(workspace)],
            "cwd": str(source), "timeout": 8, "tool_permissions": {"read_file": "read"},
        }}}), encoding="utf-8")
        with MCPManager.from_options(workspace, config_path=config) as manager:
            result = manager.call("mcp_probe_read_file", {"path": "state.txt"})
            require(result.get("ok") and "stdio-fixture-value" in json.dumps(result), "command 形式读取失败")


def check_mcp(checks):
    check(checks, "带会话头的工具发现、JSON 与 SSE 调用", mcp_responses)
    check(checks, "远程写入需要本地审批", mcp_permissions)
    check(checks, "网关故障的未知结果与不重发", mcp_gateway)
    check(checks, "保留本地 command 形式的工具调用", mcp_stdio)


CHECKS = {"outage-after-tool": check_outage, "verification-gate": check_verification,
          "skill-loading": check_skills, "mcp-streamable": check_mcp}


def main():
    task, location = sys.argv[1:]
    if task in M2_TASKS:
        return m2_acceptance([task, location])
    workspace = Path(location).resolve()
    sys.path.insert(0, str(workspace))
    # 用短路径放夹具，避免运行目录层级和原子写入临时文件名叠加后超出 Windows 路径限制。
    scratch = Path(__file__).resolve().parents[1] / "_testtmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="eval-") as directory, patch.object(tempfile, "tempdir", directory):
        checks = []
        CHECKS[task](checks)
    passed = all(item["passed"] for item in checks)
    print(json.dumps({"category": task, "passed": passed, "checks": checks}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
