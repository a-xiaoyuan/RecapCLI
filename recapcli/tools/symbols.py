"""多语言语法节点定位；不把名称索引当作编译器类型或引用分析。"""

import ast
from pathlib import Path


LANGUAGES = {
    ".py": "python", ".pyi": "python", ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".kt": "kotlin", ".kts": "kotlin", ".rb": "ruby", ".php": "php",
}
KINDS = {"class", "function", "async_function", "method", "constructor", "interface", "struct", "enum", "trait", "type", "namespace"}
NODE_KINDS = {
    "class_declaration": "class", "class_definition": "class", "class_specifier": "class", "class": "class",
    "interface_declaration": "interface", "trait_declaration": "trait", "trait_item": "trait",
    "struct_item": "struct", "struct_specifier": "struct", "struct_declaration": "struct",
    "enum_item": "enum", "enum_declaration": "enum", "enum_specifier": "enum",
    "type_alias_declaration": "type", "type_item": "type",
    "function_declaration": "function", "function_definition": "function", "function_item": "function",
    "generator_function_declaration": "function", "method_declaration": "method", "method_definition": "method",
    "method": "method", "singleton_method": "method", "constructor_declaration": "constructor",
    "namespace_definition": "namespace", "namespace_declaration": "namespace", "module": "namespace",
}


def _name(node):
    named = node.child_by_field_name("name")
    if named is not None:
        return named
    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        while declarator.child_by_field_name("declarator") is not None:
            declarator = declarator.child_by_field_name("declarator")
        return declarator
    return next((child for child in node.named_children if child.type in {
        "identifier", "simple_identifier", "type_identifier", "constant"}), None)


def extract_symbols(path, text, check):
    language = LANGUAGES.get(Path(path).suffix.lower())
    if language is None:
        return
    if language == "python":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return
        for node in ast.walk(tree):
            check()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                if node.end_lineno is not None:
                    yield node.name, kind, node.lineno, node.end_lineno
        return

    from tree_sitter_language_pack import get_parser
    tree = get_parser(language).parse(text.encode("utf-8"))
    stack = [tree.root_node]
    while stack:
        check()
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        kind = NODE_KINDS.get(node.type)
        if node.type == "type_spec":
            value = node.child_by_field_name("type")
            kind = {"struct_type": "struct", "interface_type": "interface"}.get(value.type if value else "", "type")
        if node.type in {"variable_declarator", "public_field_definition", "property_declaration"}:
            value = node.child_by_field_name("value")
            if value is not None and value.type in {"arrow_function", "function_expression", "generator_function"}:
                kind = "function"
        if kind is None:
            continue
        name = _name(node)
        if name is None or name.is_missing:
            continue
        if kind == "function" and any(child.type == "async" for child in node.children):
            kind = "async_function"
        start = node.start_point[0] + 1
        end = max(start, node.end_point[0] + int(node.end_point[1] > 0))
        yield name.text.decode("utf-8"), kind, start, end
