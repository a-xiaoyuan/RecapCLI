"""模块只能依赖同层或更低层，避免循环依赖和隐藏的函数内导入。"""

import ast
from pathlib import Path
import unittest

import recapcli

ROOT = Path(recapcli.__file__).parent
# 数字越小越靠底层。storage 依赖 Agent 的记忆与摘要校验，所以在 core 之上。
LAYERS = {"base": 0, "config": 0, "llm": 1, "tools": 2, "core": 3, "mcp": 3, "storage": 4,
          "application": 5, "ui": 6, "tui": 7, "cli": 7, "__main__": 8}


def layer_of(path):
    return path.relative_to(ROOT).parts[0].removesuffix(".py")


def imported_layers(path):
    package = path.relative_to(ROOT).parts[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base = package[:len(package) - node.level + 1]
        names = (node.module or "").split(".") if node.module else [item.name for item in node.names]
        target = [*base, *names]
        if target:
            yield node.lineno, target[0]


class LayerTests(unittest.TestCase):
    def test_imports_point_down(self):
        violations = []
        for path in ROOT.rglob("*.py"):
            if path.name == "__init__.py" and path.parent == ROOT:
                continue
            own = LAYERS[layer_of(path)]
            for line, target in imported_layers(path):
                if LAYERS[target] > own:
                    violations.append(f"{path.relative_to(ROOT)}:{line} -> {target}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
