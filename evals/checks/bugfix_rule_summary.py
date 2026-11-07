"""运行时公开检查，直接读取目标工作区生成的 CLI 任务详情。"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SCENARIOS = {
    "nested-detail": (
        {"services/api/AGENTS.md": "API 修改后运行接口测试", "tools/quality/AGENTS.md": "检查工具退出码"},
        True, "多个深层子目录提供有效规则时，任务详情显示实际加载的范围",
    ),
    "skipped-nested": (
        {"legacy/deep/AGENTS.md": "x" * 8300},
        False, "工作区没有有效规则时，任务详情不声称已加载",
    ),
}


def write_config(path):
    checks = [{"id": scenario, "description": values[2],
               "argv": [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), scenario]}
              for scenario, values in SCENARIOS.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump({"checks": checks}, file, ensure_ascii=False, indent=2)
    return path.resolve()


def check(scenario):
    import httpx2
    from openai import OpenAI

    target = Path.cwd()
    sys.path.insert(0, str(target))
    from recapcli.core.agent import run_agent
    from recapcli.storage.tasks import TaskStore

    rule_files, expect_loaded, _ = SCENARIOS[scenario]
    scratch = target / "_testtmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        workspace = Path(directory)
        for relative, content in rule_files.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        tasks = TaskStore(workspace)
        task_id = tasks.start("offline", "显示项目规则", "offline", "react", {})

        def fake(_):
            return httpx2.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
                                   "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
                                   "message": {"role": "assistant", "content": "ok"}}]})

        with OpenAI(api_key="offline-fixture", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(fake))) as client:
            result = run_agent(client, "offline", "显示项目规则", workspace, max_steps=2, on_event=lambda _: None,
                               on_trace=lambda kind, data: tasks.append(task_id, kind, data))
        tasks.finish(task_id, result.status, result.stop_reason)
        shown = subprocess.run([sys.executable, "-X", "utf8", "-m", "recapcli.cli", "--task", task_id,
                                "--workspace", str(workspace), "--banner", "off", "--color", "never"],
                               check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
    summary = next((line for line in shown.stdout.splitlines() if line.startswith("项目规则：")), "")
    print(summary or "任务详情没有项目规则摘要")
    if not summary:
        return False
    if not expect_loaded:
        return "已加载" not in summary
    return ("已加载" in summary and ("子目录" in summary or any(path in summary for path in rule_files))
            and not re.search(r"(?<![\w/\\.-])AGENTS\.md\s*[·:：]?\s*已加载", summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS)
    parser.add_argument("--write-config", type=Path)
    args = parser.parse_args()
    if bool(args.scenario) == bool(args.write_config):
        parser.error("选择一个检查场景，或用 --write-config 生成验收配置")
    if args.write_config:
        print(write_config(args.write_config))
        return 0
    return 0 if check(args.scenario) else 1


if __name__ == "__main__":
    sys.exit(main())
