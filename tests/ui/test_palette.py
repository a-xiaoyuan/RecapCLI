"""Ctrl+K 命令面板：条目来源、过滤排序，以及在真实界面里的执行与插入。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recapcli.tui.palette import Entry, filter_entries, palette_entries

ENTRIES = [
    Entry("命令", "/help [分类]", "查看命令", "insert", "/help "),
    Entry("命令", "/sessions", "列出已保存会话", "submit", "/sessions"),
    Entry("会话", "s-001", "4 条消息 · 恢复", "submit", "/open s-001"),
    Entry("技能", "review", "审查 /help 相关代码", "insert", "请使用技能「review」："),
]


class FilterTests(unittest.TestCase):
    def test_empty_query_keeps_everything_in_order(self):
        self.assertEqual(filter_entries(ENTRIES, ""), ENTRIES)

    def test_all_words_must_match(self):
        self.assertEqual([e.label for e in filter_entries(ENTRIES, "会话 恢复")], ["s-001"])
        self.assertEqual(filter_entries(ENTRIES, "不存在的词"), [])

    def test_label_hits_come_before_description_hits(self):
        labels = [e.label for e in filter_entries(ENTRIES, "help")]
        self.assertEqual(labels, ["/help [分类]", "review"])

    def test_kind_is_searchable(self):
        self.assertEqual([e.kind for e in filter_entries(ENTRIES, "技能")], ["技能"])


class EntriesTests(unittest.TestCase):
    def test_commands_with_arguments_are_inserted_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = palette_entries(Path(directory))
        by_label = {e.label.split(" ")[0]: e for e in entries if e.kind == "命令"}
        self.assertEqual(by_label["/help"].action, "insert")
        self.assertEqual(by_label["/help"].text, "/help ")
        self.assertEqual(by_label["/sessions"].action, "submit")

    def test_sessions_and_skills_are_listed(self):
        class Skill:
            name, description = "demo", "演示技能"

        class Catalog:
            skills = [Skill()]

        with patch("recapcli.tui.palette._sessions", return_value=[{"id": "s-9", "messages": 2}]), \
                patch("recapcli.tui.palette.discover_skills", return_value=Catalog()):
            entries = palette_entries(Path("."))
        session = next(e for e in entries if e.kind == "会话")
        skill = next(e for e in entries if e.kind == "技能")
        self.assertEqual((session.action, session.text), ("submit", "/open s-9"))
        self.assertEqual((skill.action, skill.text), ("insert", "请使用技能「demo」："))

    def test_broken_sources_do_not_hide_commands(self):
        with patch("recapcli.tui.palette._sessions", side_effect=OSError), \
                patch("recapcli.tui.palette.discover_skills", side_effect=ValueError):
            entries = palette_entries(Path("."))
        self.assertTrue(entries and all(e.kind == "命令" for e in entries))


if __name__ == "__main__":
    unittest.main()
