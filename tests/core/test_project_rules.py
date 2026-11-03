import hashlib
import tempfile
import unittest
from pathlib import Path

import recapcli.core.project_rules as project_rules
from recapcli.core.project_rules import (
    load_rule_layers,
    project_rules_description,
    summarize_rule_layers,
)


class ProjectRuleSummaryTests(unittest.TestCase):
    def test_single_file_loader_is_not_part_of_the_api(self):
        self.assertFalse(hasattr(project_rules, "load_project_rules"))
        for name in ("load_rule_layers", "summarize_rule_layers", "project_rules_description"):
            self.assertTrue(callable(getattr(project_rules, name)))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def write(self, relative, text):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path.stat().st_size

    def summary(self, max_bytes=None):
        kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
        layers = load_rule_layers(self.workspace, **kwargs)
        before = [dict(layer) for layer in layers]
        record = summarize_rule_layers(layers)
        self.assertEqual(layers, before)
        self.assertNotIn("text", record)
        self.assertTrue(all("text" not in layer for layer in record["layers"]))
        return record

    def test_root_only_stays_loaded_with_root_fingerprint(self):
        size = self.write("AGENTS.md", "只使用根约定")
        record = self.summary()
        digest = hashlib.sha256((self.workspace / "AGENTS.md").read_bytes()).hexdigest()
        self.assertEqual(record["status"], "loaded")
        self.assertEqual(record["source"], "AGENTS.md")
        self.assertEqual(record["bytes"], size)
        self.assertEqual(record["sha256"], digest)
        self.assertEqual([layer["source"] for layer in record["layers"]], ["AGENTS.md"])
        self.assertEqual(project_rules_description(record), f"AGENTS.md · 已加载 {size} 字节")

    def test_subdirectory_only_is_loaded_instead_of_reported_missing(self):
        (self.root / "AGENTS.md").write_text("父目录不应进入摘要", encoding="utf-8")
        size = self.write("src/AGENTS.md", "只在子目录生效")
        record = self.summary()
        digest = hashlib.sha256((self.workspace / "src" / "AGENTS.md").read_bytes()).hexdigest()
        self.assertEqual(record["status"], "loaded")
        self.assertEqual(record["source"], "src/AGENTS.md")
        self.assertEqual(record["bytes"], size)
        self.assertEqual(record["sha256"], digest)
        self.assertEqual(record["layers"][0]["status"], "loaded")
        self.assertEqual(record["layers"][0]["scope"], "src")
        text = project_rules_description(record)
        self.assertEqual(text, f"src/AGENTS.md · 已加载 {size} 字节")
        self.assertNotIn("根目录未找到", text)
        self.assertNotIn("父目录", text)

    def test_root_and_subdirectory_report_every_loaded_file(self):
        root_size = self.write("AGENTS.md", "根约定")
        sub_size = self.write("src/AGENTS.md", "子约定")
        record = self.summary()
        root_digest = hashlib.sha256((self.workspace / "AGENTS.md").read_bytes()).hexdigest()
        self.assertEqual(record["status"], "loaded")
        self.assertEqual(record["source"], "AGENTS.md")
        self.assertEqual(record["bytes"], root_size + sub_size)
        self.assertEqual(record["sha256"], root_digest)
        self.assertEqual([layer["status"] for layer in record["layers"]], ["loaded", "loaded"])
        self.assertEqual(
            project_rules_description(record),
            f"AGENTS.md · 已加载 {root_size + sub_size} 字节 · 含 2 个规则文件",
        )

    def test_empty_root_does_not_hide_loaded_subdirectory_rules(self):
        self.write("AGENTS.md", " \n\t")
        size = self.write("lib/AGENTS.md", "空根之外的约定")
        record = self.summary()
        self.assertEqual(record["layers"][0]["status"], "empty")
        self.assertEqual(record["status"], "loaded")
        self.assertEqual(record["source"], "lib/AGENTS.md")
        self.assertEqual(record["bytes"], size)
        text = project_rules_description(record)
        self.assertEqual(text, f"lib/AGENTS.md · 已加载 {size} 字节 · 根目录 AGENTS.md 为空")
        self.assertNotEqual(text, "AGENTS.md 是空文件")
        self.assertNotIn("根目录未找到", text)

    def test_missing_rules_stay_missing(self):
        record = self.summary()
        self.assertEqual(record["status"], "missing")
        self.assertEqual(record["source"], "AGENTS.md")
        self.assertEqual(record["layers"], [])
        self.assertEqual(project_rules_description(record), "根目录未找到 AGENTS.md")

    def test_empty_root_without_children_stays_empty(self):
        self.write("AGENTS.md", "\n\n")
        record = self.summary()
        self.assertEqual(record["status"], "empty")
        self.assertEqual(record["source"], "AGENTS.md")
        self.assertEqual(project_rules_description(record), "AGENTS.md 是空文件")

    def test_oversized_root_is_skipped_when_nothing_else_loads(self):
        (self.workspace / "AGENTS.md").write_bytes(b"x" * 32)
        record = self.summary(max_bytes=16)
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(record["source"], "AGENTS.md")
        self.assertEqual(record["reason"], "超过 16 字节的规则预算，未加载")
        self.assertEqual(record["layers"][0]["status"], "skipped")
        text = project_rules_description(record)
        self.assertEqual(text, "AGENTS.md 未加载 · 超过 16 字节的规则预算，未加载")
        self.assertNotIn("已加载", text)
        self.assertNotIn("根目录未找到", text)

    def test_loaded_child_is_reported_when_root_is_skipped(self):
        (self.workspace / "AGENTS.md").write_bytes(b"x" * 32)
        size = self.write("pkg/AGENTS.md", "子规则")
        record = self.summary(max_bytes=16)
        self.assertEqual(record["status"], "loaded")
        self.assertEqual(record["source"], "pkg/AGENTS.md")
        self.assertEqual(record["bytes"], size)
        self.assertEqual([layer["status"] for layer in record["layers"]], ["skipped", "loaded"])
        text = project_rules_description(record)
        self.assertEqual(text, f"pkg/AGENTS.md · 已加载 {size} 字节 · 1 个未加载")
        self.assertNotIn("根目录未找到", text)

    def test_multiple_subdirectory_rules_keep_loaded_status(self):
        shallow = self.write("src/AGENTS.md", "浅层")
        nested = self.write("src/pkg/AGENTS.md", "深层")
        record = self.summary()
        self.assertEqual(record["status"], "loaded")
        self.assertEqual(record["source"], "src/AGENTS.md")
        self.assertNotIn("sha256", record)
        self.assertEqual(record["bytes"], shallow + nested)
        self.assertEqual([layer["source"] for layer in record["layers"]], ["src/AGENTS.md", "src/pkg/AGENTS.md"])
        self.assertTrue(all(layer["status"] == "loaded" and layer["sha256"] for layer in record["layers"]))
        self.assertEqual(
            project_rules_description(record),
            f"src/AGENTS.md · 已加载 {shallow + nested} 字节 · 含 2 个规则文件",
        )

    def test_description_keeps_wording_for_records_without_layers(self):
        self.assertEqual(
            project_rules_description({"source": "AGENTS.md", "status": "loaded", "bytes": 80}),
            "AGENTS.md · 已加载 80 字节",
        )
        self.assertEqual(
            project_rules_description({"source": "AGENTS.md", "status": "missing"}),
            "根目录未找到 AGENTS.md",
        )
        self.assertEqual(
            project_rules_description({"source": "AGENTS.md", "status": "empty"}),
            "AGENTS.md 是空文件",
        )
        self.assertEqual(
            project_rules_description({"source": "AGENTS.md", "status": "skipped", "reason": "历史压缩不加载项目开发约定"}),
            "AGENTS.md 未加载 · 历史压缩不加载项目开发约定",
        )
        self.assertEqual(
            project_rules_description(None),
            "本轮未记录项目规则；普通提问时读取根目录 AGENTS.md",
        )


if __name__ == "__main__":
    unittest.main()
