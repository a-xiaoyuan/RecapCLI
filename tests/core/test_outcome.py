"""终态记录的文句与现有行为一致：写入、验证和声称完成不能各说各话。"""

import json
from pathlib import Path
import tempfile
import unittest

import httpx2
from openai import OpenAI

from recapcli.config import Config
from recapcli.core.acceptance import acceptance_label
from recapcli.core.agent import run_agent
from recapcli.core.outcome import finish_outcome, judge_claim
from recapcli.llm.client import ModelClient
from recapcli.tools.approval import approve_all


class OutcomeContractTests(unittest.TestCase):
    def test_returned_without_checks_keeps_the_old_sentence(self):
        outcome = finish_outcome("看完了", "returned", "")
        self.assertEqual(outcome.status, "returned")
        self.assertEqual(outcome.stop_reason, "模型已返回回答；任务验收未验证")
        self.assertEqual(outcome.verification, "absent")
        self.assertEqual(outcome.acceptance, "absent")
        self.assertEqual(outcome.files_written, ())
        self.assertEqual(outcome.claim, "none")

    def test_passed_verification_and_acceptance_share_one_record(self):
        note = "；" + acceptance_label(2, 0, 0)
        outcome = finish_outcome("改好了", "returned", "", files_written=("app.py",),
                                 verification="passed", acceptance="passed", acceptance_note=note)
        self.assertEqual(outcome.files_written, ("app.py",))
        self.assertEqual(outcome.verification, "passed")
        self.assertEqual(outcome.acceptance, "passed")
        self.assertEqual(outcome.stop_reason,
                         "模型已返回回答；自动验证通过；需求验收 2/2 通过，0 项失败，0 项未运行；人工验收未登记")

    def test_incomplete_without_acceptance_only_adds_the_unverified_clause(self):
        outcome = finish_outcome("还没改", "incomplete", "自动验证未通过（pytest）", verification="failed")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(outcome.verification, "failed")
        self.assertEqual(outcome.stop_reason, "自动验证未通过（pytest）；任务验收未验证")

    def test_acceptance_not_run_adds_the_zero_label_and_pending_manual(self):
        outcome = finish_outcome("中断了", "incomplete", "时间不足", acceptance="not_run", acceptance_total=3)
        self.assertEqual(outcome.acceptance, "not_run")
        self.assertEqual(outcome.stop_reason,
                         "时间不足；需求验收 0/3 通过，0 项失败，3 项未运行；人工验收未登记")

    def test_failed_acceptance_does_not_add_a_second_zero_label(self):
        label = acceptance_label(1, 1, 0)
        outcome = finish_outcome("没过", "incomplete", label, acceptance="failed")
        self.assertEqual(outcome.stop_reason, f"{label}；人工验收未登记")
        self.assertNotIn("0/2", outcome.stop_reason)

    def test_unbacked_claim_annotates_without_changing_status(self):
        claim, answer, reason = judge_claim("已完成修改。", "模型已返回回答；任务验收未验证",
                                            claim_applies=True, files_written=())
        self.assertEqual(claim, "unbacked")
        self.assertTrue(answer.endswith("上面描述的改动并未写入工作区。"))
        self.assertIn("回答声称已修改，但本轮未通过编辑工具写入文件", reason)
        again, _, again_reason = judge_claim(answer, reason, claim_applies=True, files_written=())
        self.assertEqual(again, "unbacked")
        self.assertEqual(answer.count("并未写入工作区"), 1)
        self.assertEqual(again_reason, reason)

    def test_written_files_count_as_backed_and_skip_the_note(self):
        claim, answer, reason = judge_claim("已修复 app.py。", "模型已返回回答；任务验收未验证",
                                            claim_applies=False, files_written=("app.py",))
        self.assertEqual((claim, answer, reason),
                         ("backed", "已修复 app.py。", "模型已返回回答；任务验收未验证"))

    def test_readonly_answer_is_not_judged_even_if_it_says_done(self):
        claim, answer, reason = judge_claim("分析已完成。", "模型已返回回答；任务验收未验证",
                                            claim_applies=False, files_written=())
        self.assertEqual(claim, "not_applicable")
        self.assertEqual(answer, "分析已完成。")
        self.assertNotIn("并未写入", reason)


def reply(content, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"id": "offline", "object": "chat.completion", "created": 1, "model": "offline",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}]}


class OutcomeOnRunTests(unittest.TestCase):
    def test_run_records_the_write_and_does_not_flag_a_real_edit(self):
        workspace = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(workspace, ignore_errors=True))
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        arguments = json.dumps({"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
                               ensure_ascii=False)
        replies = [
            reply(None, [{"id": "e", "type": "function", "function": {"name": "edit_file", "arguments": arguments}}]),
            reply("已修复 app.py。"),
        ]

        def handler(request):
            return httpx2.Response(200, json=replies.pop(0))

        def factory(**options):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **options)

        config = Config("offline-key", "offline", "https://offline.invalid/v1",
                        protocol="chat_completions", streaming=False, tools=True)
        with ModelClient(config, client_factory=factory) as client:
            result = run_agent(client, "offline", "把 value 改成 2", workspace,
                               capabilities=("read", "write"), approver=approve_all, mode="react", max_steps=4)
        self.assertEqual(result.status, "returned")
        self.assertEqual(result.outcome.files_written, ("app.py",))
        self.assertEqual(result.outcome.claim, "backed")
        self.assertNotIn("并未写入工作区", result.answer)
        self.assertEqual((workspace / "app.py").read_text(encoding="utf-8"), "value = 2\n")

    def test_run_flags_a_claim_when_nothing_was_written(self):
        workspace = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(workspace, ignore_errors=True))
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

        def handler(request):
            return httpx2.Response(200, json=reply("已完成修改，改动如下。"))

        def factory(**options):
            return OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **options)

        config = Config("offline-key", "offline", "https://offline.invalid/v1",
                        protocol="chat_completions", streaming=False, tools=True)
        with ModelClient(config, client_factory=factory) as client:
            result = run_agent(client, "offline", "修改 app.py", workspace,
                               capabilities=("read", "write", "exec"), approver=approve_all, mode="react",
                               max_steps=4)
        self.assertEqual(result.status, "returned")
        self.assertEqual(result.outcome.files_written, ())
        self.assertEqual(result.outcome.claim, "unbacked")
        self.assertEqual(result.outcome.verification, "absent")
        self.assertIn("并未写入工作区", result.answer)
        self.assertIn("未通过编辑工具写入文件", result.stop_reason)
        self.assertEqual((workspace / "app.py").read_text(encoding="utf-8"), "value = 1\n")


if __name__ == "__main__":
    unittest.main()
