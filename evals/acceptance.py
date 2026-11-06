"""External acceptance. Never included in the Agent's workspace or prompt."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

category, workspace = sys.argv[1], Path(sys.argv[2]).resolve()
sys.path.insert(0, str(workspace))
scratch = workspace / "_testtmp" / "external-acceptance"
scratch.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(scratch)
checks = []


def check(name, fn):
    try:
        fn()
        checks.append({"name": name, "passed": True})
    except (Exception, SystemExit) as error:
        checks.append({"name": name, "passed": False,
                       "error": f"{type(error).__name__}: {error}"})


def require(condition, message):
    if not condition:
        raise AssertionError(message)


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
            layers = record.get("layers", [])
            loaded = [item for item in layers if item.get("status") == "loaded"]
            require(len(loaded) == expected_loaded, f"unexpected actual loaded count: {len(loaded)}")
            if expected_loaded:
                require("已加载" in text, f"loaded rules reported incorrectly: {text}")
                require("未找到" not in text and "是空文件" not in text and "未加载" not in text,
                        f"root status masks loaded rules: {text}")
                require("pkg/AGENTS.md" in requests[0]["messages"][0]["content"], "nested content missing")
            else:
                require("已加载" not in text, f"empty/missing rules reported as loaded: {text}")
            if root_content and len(root_content.encode()) > 8192:
                require(root_content not in requests[0]["messages"][0]["content"], "oversized root injected")

    check("missing root with loaded nested rule", lambda: rule_case(None, "只对 pkg 生效", 1))
    check("empty root with loaded nested rule", lambda: rule_case("", "子目录需要 lint", 1))
    check("oversized root with loaded nested rule", lambda: rule_case("x" * 8300, "子目录约定", 1))
    check("root and nested both loaded", lambda: rule_case("根约定", "子约定", 2))
    check("no root or nested rule", lambda: rule_case(None, None, 0))
    check("empty root and no nested rule", lambda: rule_case("", None, 0))

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
    check("storage filter before limit and backward compatibility", store_filter)

    def invalid_store():
        try:
            tasks.list_tasks(status="bogus")
        except ValueError:
            return
        raise AssertionError("invalid status did not raise ValueError")
    check("invalid status rejected in storage", invalid_store)

    def cli_filter():
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--workspace", str(fixture), "--tasks", "--task-status", "failed", "--json"])
        require(code == 0, f"CLI returned {code}")
        rows = json.loads(output.getvalue())
        require({row["id"] for row in rows} == set(wanted), "CLI filter not applied or old tasks lost")
    check("CLI JSON status filter", cli_filter)

    def slash_filter():
        ui = UI()
        handle_command("/tasks failed", ui, store, session, "offline")
        text = "\n".join(ui.lines)
        require(not ui.errors, f"slash errors: {ui.errors}")
        require(all(identity in text for identity in wanted), "slash omitted matching tasks")
        require("returned" not in text and running not in text, "slash did not filter")
    check("slash command filters history", slash_filter)

    def slash_invalid():
        ui = UI()
        handle_command("/tasks nonsense", ui, store, session, "offline")
        require(bool(ui.errors), "invalid slash status silently ignored")
    check("invalid slash status reports error", slash_invalid)

    def cli_requires_tasks():
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main(["--workspace", str(fixture), "--task-status", "failed"])
            except SystemExit as error:
                code = error.code
        require(code == 2, f"standalone --task-status should fail before model startup: {code}")
    check("CLI filter cannot start a model task", cli_requires_tasks)
    check("slash help advertises optional filter", lambda: require(
        any("/tasks" in line and ("状态" in line or "status" in line) for line in help_text("tasks").splitlines()),
        "filter absent from command help"))

else:
    raise ValueError(category)

print(json.dumps({"category": category, "checks": checks,
                  "passed": bool(checks) and all(item["passed"] for item in checks)}, ensure_ascii=False))
sys.exit(0 if checks and all(item["passed"] for item in checks) else 1)
