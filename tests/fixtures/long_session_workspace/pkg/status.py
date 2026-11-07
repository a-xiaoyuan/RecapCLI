"""规则摘要的显示。"""


def format_status(root_loaded, nested_name):
    """根规则没加载时，写出实际加载的子目录规则名。"""
    if nested_name and not root_loaded:
        return f"{nested_name} · 已加载"
    if root_loaded:
        return "AGENTS.md · 已加载"
    return "未加载规则"
