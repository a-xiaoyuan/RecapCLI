"""External acceptance. Never included in the Agent's workspace or prompt."""
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace

ACCEPTANCE_VERSION = 2


def check(checks, name, fn):
    try:
        fn()
        checks.append({"name": name, "passed": True})
    except (Exception, SystemExit) as error:
        checks.append({"name": name, "passed": False,
                       "error": f"{type(error).__name__}: {error}"})


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def validate_rule_summary(record, text, expected_loaded):
    """核对实际加载状态与数量；根规则的准确缺失/跳过说明不构成失败。"""
    loaded = [item for item in record.get("layers", []) if item.get("status") == "loaded"]
    require(len(loaded) == expected_loaded, f"实际加载规则数量不符：{len(loaded)}")
    if not expected_loaded:
        require(record.get("status") != "loaded" and "已加载" not in text,
                f"没有有效规则却报告已加载：{text}")
        return
    require(record.get("status") == "loaded", f"规则总状态未反映已加载的规则：{record.get('status')}")
    size = re.search(r"已加载\s*(\d+)\s*字节", text)
    require(size is not None, f"摘要未正确报告已加载的规则：{text}")
    sizes = {sum(item.get("bytes", 0) for item in loaded)}
    # 摘要明确以文件名开头时，字节数也可以指该文件；不能强迫旧实现改用总字节数。
    prefix = text[:size.start()]
    for item in loaded:
        source = item.get("source", "")
        if source and re.search(r"(?<![\w/\\])" + re.escape(source) + r"\s*[·:：]?\s*$", prefix):
            sizes.add(item.get("bytes", 0))
    require(int(size[1]) in sizes,
            f"摘要中的已加载字节数不符：{text}")


def main(argv=None):
    category, location = sys.argv[1:] if argv is None else argv
    workspace = Path(location).resolve()
    sys.path.insert(0, str(workspace))
    scratch = workspace / "_testtmp" / "external-acceptance"
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    checks = []
    if category == "bugfix":
        import httpx2
        from openai import OpenAI
        from recapcli.core.agent import run_agent
        from recapcli.core.project_rules import project_rules_description

        def rule_case(root_content, nested_content, expected_loaded):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "pkg").mkdir()
                if root_content is not None:
                    (root / "AGENTS.md").write_text(root_content, encoding="utf-8")
                if nested_content is not None:
                    (root / "pkg" / "AGENTS.md").write_text(nested_content, encoding="utf-8")
                requests, events = [], []

                def handler(request):
                    requests.append(json.loads(request.content))
                    return httpx2.Response(200, json={"id": "acceptance", "object": "chat.completion",
                        "created": 0, "model": "offline", "choices": [{"index": 0,
                        "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

                with OpenAI(api_key="acceptance-placeholder", base_url="https://offline.invalid/v1",
                            max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))) as client:
                    run_agent(client, "offline", "说明项目规则", root, max_steps=2,
                              on_event=lambda event: None, on_trace=lambda kind, data: events.append((kind, data)))
                record = next(data for kind, data in events if kind == "project_rules")
                text = project_rules_description(record)
                tools = {item["function"]["name"] for item in requests[0].get("tools", [])}
                require("run_command" not in tools and "edit_file" not in tools, "rules enlarged capabilities")
                validate_rule_summary(record, text, expected_loaded)
                if expected_loaded:
                    require("pkg/AGENTS.md" in requests[0]["messages"][0]["content"], "nested content missing")
                if root_content and len(root_content.encode()) > 8192:
                    require(root_content not in requests[0]["messages"][0]["content"], "oversized root injected")

        check(checks, "missing root with loaded nested rule", lambda: rule_case(None, "只对 pkg 生效", 1))
        check(checks, "empty root with loaded nested rule", lambda: rule_case("", "子目录需要 lint", 1))
        check(checks, "oversized root with loaded nested rule", lambda: rule_case("x" * 8300, "子目录约定", 1))
        check(checks, "root and nested both loaded", lambda: rule_case("根约定", "子约定", 2))
        check(checks, "no root or nested rule", lambda: rule_case(None, None, 0))
        check(checks, "empty root and no nested rule", lambda: rule_case("", None, 0))

    elif category == "feature":
        from recapcli.storage.tasks import TaskStore
        from recapcli.storage.session import SessionStore
        from recapcli.application.commands import handle_command
        from recapcli.application.help import help_text
        from recapcli.cli import main

        fixture = scratch / "history"
        fixture.mkdir(exist_ok=True)
        tasks = TaskStore(fixture)
        wanted = []
        for index in range(5):
            identity = tasks.start("fixture", f"older failed {index}", "offline", "react", {})
            tasks.finish(identity, "failed", "known fixture failure")
            wanted.append(identity)
        for index in range(25):
            identity = tasks.start("fixture", f"newer returned {index}", "offline", "react", {})
            tasks.finish(identity, "returned", "known fixture success")
        running = tasks.start("fixture", "running", "offline", "react", {})
        store = SessionStore(fixture)
        session = store.new_session("offline")

        class UI:
            def __init__(self):
                self.lines, self.errors = [], []
            def info(self, text):
                self.lines.append(str(text))
            def error(self, text):
                self.errors.append(str(text))

        def store_filter():
            rows = tasks.list_tasks(limit=2, status="failed")
            require([row["id"] for row in rows] == list(reversed(wanted))[:2], "must filter before applying limit")
            require(len(tasks.list_tasks()) == 20, "default list limit changed")
            require([row["id"] for row in tasks.list_tasks(status="running")] == [running], "running status unsupported")
        check(checks, "storage filter before limit and backward compatibility", store_filter)

        def invalid_store():
            try:
                tasks.list_tasks(status="bogus")
            except ValueError:
                return
            raise AssertionError("invalid status did not raise ValueError")
        check(checks, "invalid status rejected in storage", invalid_store)

        def cli_filter():
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--workspace", str(fixture), "--tasks", "--task-status", "failed", "--json"])
            require(code == 0, f"CLI returned {code}")
            rows = json.loads(output.getvalue())
            require({row["id"] for row in rows} == set(wanted), "CLI filter not applied or old tasks lost")
        check(checks, "CLI JSON status filter", cli_filter)

        def slash_filter():
            ui = UI()
            handle_command("/tasks failed", ui, store, session, "offline")
            text = "\n".join(ui.lines)
            require(not ui.errors, f"slash errors: {ui.errors}")
            require(all(identity in text for identity in wanted), "slash omitted matching tasks")
            require("returned" not in text and running not in text, "slash did not filter")
        check(checks, "slash command filters history", slash_filter)

        def slash_invalid():
            ui = UI()
            handle_command("/tasks nonsense", ui, store, session, "offline")
            require(bool(ui.errors), "invalid slash status silently ignored")
        check(checks, "invalid slash status reports error", slash_invalid)

        def cli_requires_tasks():
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = main(["--workspace", str(fixture), "--task-status", "failed"])
                except SystemExit as error:
                    code = error.code
            require(code == 2, f"standalone --task-status should fail before model startup: {code}")
        check(checks, "CLI filter cannot start a model task", cli_requires_tasks)
        check(checks, "slash help advertises optional filter", lambda: require(
            any("/tasks" in line and ("状态" in line or "status" in line) for line in help_text("tasks").splitlines()),
            "filter absent from command help"))

    else:
        raise ValueError(category)

    print(json.dumps({"version": ACCEPTANCE_VERSION, "category": category, "checks": checks,
                      "passed": bool(checks) and all(item["passed"] for item in checks)}, ensure_ascii=False))
    return 0 if checks and all(item["passed"] for item in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
