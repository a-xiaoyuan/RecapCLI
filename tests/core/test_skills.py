"""Skills：提示词只放名称和描述，全文按需加载，坏技能跳过并说明。"""

from contextlib import suppress
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import httpx2
from openai import OpenAI

from recapcli.application.commands import handle_command
from recapcli.application.help import help_text
from recapcli.application.runtime import run_turn
from recapcli.core.agent import AgentLimits, run_agent
from recapcli.core.hooks import HookSet
from recapcli.core.skills import MAX_SKILL_BYTES, Skill, SkillCatalog, discover_skills, parse_skill_file
from recapcli.storage.session import SessionStore
from recapcli.storage.tasks import TaskStore


def completion(message, finish_reason):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


def load(name):
    return completion({"role": "assistant", "content": None, "tool_calls": [{
        "id": "call", "type": "function",
        "function": {"name": "load_skill", "arguments": json.dumps({"name": name})}}]}, "tool_calls")


def text(content):
    return completion({"role": "assistant", "content": content}, "stop")


def skill_file(name="review", description="审查代码时使用", body="第一步：看差异"):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


class FakeUI:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)

    def error(self, message):
        self.lines.append(message)

    def event(self, message):
        pass

    def task_update(self, task):
        pass


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.user_dir = self.root / "user-skills"

    def write(self, root, folder, content):
        path = Path(root) / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def project(self, folder, content, root=".agents/skills"):
        return self.write(self.workspace / root, folder, content)

    def discover(self):
        return discover_skills(self.workspace, self.user_dir)


class ParseTests(unittest.TestCase):
    def test_common_frontmatter_forms(self):
        cases = {
            'description: "带: 冒号 \\"引号\\""': '带: 冒号 "引号"',
            "description: '它''s 好'": "它's 好",
            "description: >-\n  第一行\n  第二行": "第一行 第二行",
            "description: |\n  第一行\n  第二行": "第一行 第二行",
            "description: 普通 # 注释": "普通",
            "description: 普通\n  续行": "普通 续行",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                fields, body = parse_skill_file(f"---\nname: a\n{source}\n---\n正文")
                self.assertEqual(fields["description"], expected)
                self.assertEqual(body, "正文")

    def test_nested_and_unknown_fields_do_not_leak(self):
        fields, _ = parse_skill_file("---\nmetadata:\n  name: 错的\n  description: 错的\nname: 对的\n---\n正文")
        self.assertEqual(fields["name"], "对的")
        self.assertNotIn("description", fields)

    def test_missing_or_unclosed_header_is_rejected(self):
        for source in ("正文", "---\nname: a\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_skill_file(source)


class DiscoverTests(WorkspaceCase):
    def test_no_directories_means_empty_catalog(self):
        catalog = self.discover()
        self.assertEqual((catalog.skills, catalog.skipped), ((), ()))

    def test_scans_all_roots_sorted_with_crlf_and_bom(self):
        self.project("b", skill_file("b"), ".claude/skills")
        self.project("a", ("﻿" + skill_file("a")).replace("\n", "\r\n").encode("utf-8"))
        self.write(self.user_dir, "c", skill_file("c"))
        catalog = self.discover()
        self.assertEqual([item.name for item in catalog.skills], ["a", "b", "c"])
        self.assertEqual(catalog.skills[0].source, ".agents/skills/a/SKILL.md")
        self.assertEqual(catalog.skills[0].description, "审查代码时使用")

    def test_earlier_root_wins_and_loser_is_reported(self):
        self.project("x", skill_file("x", "项目版"))
        self.write(self.user_dir, "x", skill_file("x", "用户版"))
        catalog = self.discover()
        self.assertEqual([item.description for item in catalog.skills], ["项目版"])
        self.assertIn("同名", catalog.skipped[0][1])

    def test_missing_name_falls_back_to_folder(self):
        self.project("from-folder", "---\ndescription: 描述\n---\n正文")
        self.assertEqual(self.discover().skills[0].name, "from-folder")

    def test_bad_skills_are_skipped_with_reason_and_good_ones_stay(self):
        self.project("good", skill_file("good"))
        self.project("no-description", "---\nname: no-description\n---\n正文")
        self.project("bad-name", skill_file("Bad_Name"))
        self.project("no-header", "只有正文")
        self.project("empty-body", skill_file("empty-body", body=""))
        self.project("binary", b"\xff\xfe\x00")
        self.project("huge", skill_file("huge", body="x" * MAX_SKILL_BYTES))
        (self.workspace / ".agents/skills/not-a-skill").mkdir()
        catalog = self.discover()
        self.assertEqual([item.name for item in catalog.skills], ["good"])
        reasons = dict(catalog.skipped)
        self.assertEqual(len(reasons), 6)
        self.assertIn("description", reasons[".agents/skills/no-description/SKILL.md"])
        self.assertIn("不合法", reasons[".agents/skills/bad-name/SKILL.md"])
        self.assertIn("字节", reasons[".agents/skills/huge/SKILL.md"])

    def test_project_symlink_cannot_pull_in_outside_file(self):
        outside = self.write(self.root / "outside", "leak", skill_file("leak")).parent
        link = self.workspace / ".agents/skills/leak"
        link.parent.mkdir(parents=True)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            # Windows 普通用户不能建符号链接，目录联接不需要管理员权限。
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        catalog = self.discover()
        self.assertEqual(catalog.skills, ())
        self.assertIn("项目之外", catalog.skipped[0][1])


class CatalogTests(unittest.TestCase):
    def catalog(self, count=1):
        return SkillCatalog(tuple(Skill(f"s{index}", "描述" * 20, "来源", Path("目录"), "正文", "hash")
                                  for index in range(count)))

    def test_prompt_respects_budget_and_says_what_was_omitted(self):
        text_ = self.catalog(50).prompt_text(600)
        self.assertLessEqual(len(text_.encode("utf-8")), 700)
        self.assertIn("- s0：", text_)
        self.assertNotIn("- s49：", text_)
        self.assertRegex(text_, r"另有 \d+ 个技能")

    def test_load_rejects_bad_input(self):
        catalog = self.catalog()
        for arguments, expected in (("不是 JSON", "JSON"), ("{}", "name"), ('{"name": "nope"}', "s0")):
            with self.subTest(arguments=arguments):
                result = catalog.load(arguments)
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["error"])


class AgentTests(WorkspaceCase):
    def run_turn(self, replies, *, hooks=None, mode="react", skills=None):
        self.requests = []
        self.events = []

        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[min(len(self.requests), len(replies)) - 1])

        client = OpenAI(api_key="offline-test-key", base_url="https://offline.invalid/v1", max_retries=0,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with client:
            return run_agent(client, "test-model", "审查", self.workspace, on_event=self.events.append, hooks=hooks,
                             mode=mode, skills=self.discover() if skills is None else skills)

    def tool_names(self, index=0):
        return [item["function"]["name"] for item in self.requests[index].get("tools", [])]

    def test_body_reaches_model_only_after_load_skill(self):
        self.project("review", skill_file())
        result = self.run_turn([load("review"), text("按技能做完了")])
        self.assertEqual(result.answer, "按技能做完了")
        first = json.dumps(self.requests[0]["messages"], ensure_ascii=False)
        self.assertIn("review：审查代码时使用", first)
        self.assertNotIn("第一步：看差异", first)
        self.assertIn("load_skill", self.tool_names())
        self.assertIn("第一步：看差异", json.dumps(self.requests[1]["messages"], ensure_ascii=False))
        self.assertIn("[操作] 已加载技能 review", self.events)

    def test_no_skills_adds_nothing(self):
        self.run_turn([text("好")])
        self.assertNotIn("load_skill", self.tool_names())
        self.assertNotIn("可用技能", json.dumps(self.requests[0]["messages"], ensure_ascii=False))

    def test_unknown_skill_goes_back_to_model(self):
        self.project("review", skill_file())
        self.run_turn([load("nope"), text("没有这个技能")])
        self.assertIn("没有技能 nope", json.dumps(self.requests[1]["messages"], ensure_ascii=False))

    def test_pre_tool_hook_can_block_load_skill(self):
        self.project("review", skill_file())
        path = self.root / "hooks.json"
        block = [sys.executable, "-c", "import sys; print('不允许'); sys.exit(2)"]
        path.write_text(json.dumps({"pre_tool": [{"match": "load_skill", "command": block}]}), encoding="utf-8")
        self.run_turn([load("review"), text("被挡住了")], hooks=HookSet.load(path))
        second = json.dumps(self.requests[1]["messages"], ensure_ascii=False)
        self.assertIn("blocked_by_hook", second)
        self.assertNotIn("第一步：看差异", second)

    def test_plan_mode_does_not_offer_skills(self):
        self.project("review", skill_file())
        with suppress(Exception):
            self.run_turn([text("好")], mode="plan")
        self.assertTrue(self.requests)
        self.assertNotIn("load_skill", self.tool_names())


class EntryTests(WorkspaceCase):
    def test_task_records_skill_versions(self):
        self.project("review", skill_file())
        store = SessionStore(self.workspace)
        session = store.new_session("offline")

        def reply(request):
            return httpx2.Response(200, json=text("好"))

        with OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(reply))) as client:
            run_turn(client, "offline", self.workspace, store, FakeUI(), session, "审查", memory_enabled=False,
                     limits=AgentLimits(max_retries=0))
        tasks = TaskStore(self.workspace, create=False)
        versions = tasks.get(tasks.list_tasks(limit=1)[0]["id"])["metadata"]["skill_versions"]
        expected = self.discover().skills[0]
        self.assertEqual(versions, [{"name": "review", "source": expected.source, "sha256": expected.sha256}])

    def skills_output(self):
        ui = FakeUI()
        session = {"messages": []}
        result = handle_command("/skills", ui, SimpleNamespace(workspace=self.workspace), session, "")
        self.assertIs(result, session)
        return "\n".join(ui.lines)

    def test_command_lists_found_and_skipped(self):
        self.project("review", skill_file())
        self.project("broken", "只有正文")
        output = self.skills_output()
        self.assertIn("review · .agents/skills/review/SKILL.md", output)
        self.assertIn("已跳过 .agents/skills/broken/SKILL.md", output)

    def test_command_explains_where_to_put_skills(self):
        self.assertIn(".agents/skills/技能名/", self.skills_output())

    def test_help_lists_command(self):
        self.assertIn("/skills", help_text())


if __name__ == "__main__":
    unittest.main()
