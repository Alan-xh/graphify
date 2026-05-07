"""Deterministic structural extraction from source code using tree-sitter. Outputs nodes+edges dicts."""
from __future__ import annotations
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any
from .cache import load_cached, save_cached


def _make_id(*parts: str) -> str:
    """生成稳定的节点标识符。
    
    将多个名称部分拼接成一个规范的标识符，用于在图中唯一标识一个节点。
    会自动去除下划线、清理特殊字符，并转换为小写。
    
    Args:
        *parts: 一个或多个名称片段，如文件名、类名、函数名等
        
    Returns:
        str: 清理后的标识符，只包含字母、数字和下划线，全部小写
    """
    combined = "_".join(p.strip("_.") for p in parts if p)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", combined)
    return cleaned.strip("_").lower()


# ── LanguageConfig dataclass ─────────────────────────────────────────────────

@dataclass
class LanguageConfig:
    """编程语言配置类，定义特定语言的 AST 解析规则。
    
    用于配置 tree-sitter 如何解析特定编程语言的源代码，包括：
    节点类型、字段名称、回调函数等。
    
    Attributes:
        ts_module: tree-sitter 语言模块名称，如 "tree_sitter_python"
        ts_language_fn: 获取语言对象的函数名，默认 "language"
        class_types: 类定义节点的类型集合
        function_types: 函数定义节点的类型集合
        import_types: 导入语句节点的类型集合
        call_types: 函数调用节点的类型集合
        static_prop_types: 静态属性访问节点的类型集合
        helper_fn_names: 辅助函数名称集合（如 config、env）
        container_bind_methods: 服务容器绑定方法名称集合
        event_listener_properties: 事件监听器属性名称集合
        name_field: 存储名称的字段名，默认 "name"
        name_fallback_child_types: 备用的名称子节点类型元组
        body_field: 存储函数体的字段名，默认 "body"
        body_fallback_child_types: 备用的函数体子节点类型元组
        call_function_field: 调用节点中函数字段名
        call_accessor_node_types: 成员访问节点类型集合
        call_accessor_field: 访问器中方法名字段名
        function_boundary_types: 函数边界类型，用于停止递归
        import_handler: 导入节点的自定义处理函数
        resolve_function_name_fn: 函数名解析函数（用于 C/C++）
        function_label_parens: 函数标签是否添加括号，默认 True
        extra_walk_fn: 额外的遍历钩子函数
    """
    ts_module: str                                   # e.g. "tree_sitter_python"
    ts_language_fn: str = "language"                 # attr to call: e.g. tslang.language()

    class_types: frozenset = frozenset()
    function_types: frozenset = frozenset()
    import_types: frozenset = frozenset()
    call_types: frozenset = frozenset()
    static_prop_types: frozenset = frozenset()
    helper_fn_names: frozenset = frozenset()
    container_bind_methods: frozenset = frozenset()
    event_listener_properties: frozenset = frozenset()

    # Name extraction
    name_field: str = "name"
    name_fallback_child_types: tuple = ()

    # Body detection
    body_field: str = "body"
    body_fallback_child_types: tuple = ()   # e.g. ("declaration_list", "compound_statement")

    # Call name extraction
    call_function_field: str = "function"           # field on call node for callee
    call_accessor_node_types: frozenset = frozenset()  # member/attribute nodes
    call_accessor_field: str = "attribute"          # field on accessor for method name

    # Stop recursion at these types in walk_calls
    function_boundary_types: frozenset = frozenset()

    # Import handler: called for import nodes instead of generic handling
    import_handler: Callable | None = None

    # Optional custom name resolver for functions (C, C++ declarator unwrapping)
    resolve_function_name_fn: Callable | None = None

    # Extra label formatting for functions: if True, functions get "name()" label
    function_label_parens: bool = True

    # Extra walk hook called after generic dispatch (for JS arrow functions, C# namespaces, etc.)
    extra_walk_fn: Callable | None = None


# ── Generic helpers ───────────────────────────────────────────────────────────

def _read_text(node, source: bytes) -> str:
    """从 AST 节点读取源代码文本。
    
    Args:
        node: tree-sitter AST 节点
        source: 原始源代码字节数据
        
    Returns:
        str: 节点对应的源代码文本
    """
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _resolve_name(node, source: bytes, config: LanguageConfig) -> str | None:
    """从节点中提取名称。
    
    优先使用配置的 name_field，失败时尝试备用的子节点类型。
    
    Args:
        node: AST 节点
        source: 原始源代码
        config: 语言配置
        
    Returns:
        str | None: 节点名称，如果无法提取则返回 None
    """
    if config.resolve_function_name_fn is not None:
        # For C/C++ where the name is inside a declarator
        return None  # caller handles this separately
    n = node.child_by_field_name(config.name_field)
    if n:
        return _read_text(n, source)
    for child in node.children:
        if child.type in config.name_fallback_child_types:
            return _read_text(child, source)
    return None


def _find_body(node, config: LanguageConfig):
    """查找函数/类的函数体节点。
    
    优先使用配置的 body_field，失败时尝试备用的子节点类型。
    
    Args:
        node: 函数或类定义节点
        config: 语言配置
        
    Returns:
        函数体节点，如果找不到则返回 None
    """
    b = node.child_by_field_name(config.body_field)
    if b:
        return b
    for child in node.children:
        if child.type in config.body_fallback_child_types:
            return child
    return None


# ── Import handlers ───────────────────────────────────────────────────────────

def _import_python(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 Python 的 import 语句。
    
    支持 import_statement 和 import_from_statement 两种导入形式，
    包括相对导入和绝对导入。
    
    Args:
        node: import 语句的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    t = node.type
    if t == "import_statement":
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                raw = _read_text(child, source)
                module_name = raw.split(" as ")[0].strip().lstrip(".")
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
    elif t == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        if module_node:
            raw = _read_text(module_node, source)
            if raw.startswith("."):
                # Relative import - resolve to full path so IDs match file node IDs
                dots = len(raw) - len(raw.lstrip("."))
                module_name = raw.lstrip(".")
                base = Path(str_path).parent
                for _ in range(dots - 1):
                    base = base.parent
                rel = (module_name.replace(".", "/") + ".py") if module_name else "__init__.py"
                tgt_nid = _make_id(str(base / rel))
            else:
                tgt_nid = _make_id(raw)
            edges.append({
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports_from",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })


def _import_js(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 JavaScript/TypeScript 的 import 语句。
    
    支持相对路径和 npm 包名的导入，相对路径会被解析为实际文件路径。
    
    Args:
        node: import 语句的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    for child in node.children:
        if child.type == "string":
            raw = _read_text(child, source).strip("'\"` ")
            if not raw:
                break
            if raw.startswith("."):
                # Relative import - resolve to full path so IDs match target file's own node ID
                # normpath removes ".." segments so the ID matches the target file's own node ID
                resolved = Path(os.path.normpath(Path(str_path).parent / raw))
                # TypeScript ESM: imports written as .js but actual file is .ts/.tsx
                if resolved.suffix == ".js":
                    resolved = resolved.with_suffix(".ts")
                elif resolved.suffix == ".jsx":
                    resolved = resolved.with_suffix(".tsx")
                tgt_nid = _make_id(str(resolved))
            else:
                # Bare/scoped import (node_modules) - use last segment; dropped as external
                module_name = raw.split("/")[-1]
                if not module_name:
                    break
                tgt_nid = _make_id(module_name)
            edges.append({
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports_from",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })
            break


def _import_java(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 Java 的 import 语句。
    
    支持通配符导入（import java.util.*）和单类导入。
    
    Args:
        node: import 语句的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    def _walk_scoped(n) -> str:
        parts: list[str] = []
        cur = n
        while cur:
            if cur.type == "scoped_identifier":
                name_node = cur.child_by_field_name("name")
                if name_node:
                    parts.append(_read_text(name_node, source))
                cur = cur.child_by_field_name("scope")
            elif cur.type == "identifier":
                parts.append(_read_text(cur, source))
                break
            else:
                break
        parts.reverse()
        return ".".join(parts)

    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            path_str = _walk_scoped(child)
            module_name = path_str.split(".")[-1].strip("*").strip(".") or (
                path_str.split(".")[-2] if len(path_str.split(".")) > 1 else path_str
            )
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_c(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 C/C++ 的预处理 include 指令。
    
    支持系统库（<...>）和用户头文件（"..."）两种形式。
    
    Args:
        node: include 指令的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    for child in node.children:
        if child.type in ("string_literal", "system_lib_string", "string"):
            raw = _read_text(child, source).strip('"<> ')
            module_name = raw.split("/")[-1].split(".")[0]
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_csharp(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 C# 的 using 指令。
    
    支持命名空间导入和别名导入。
    
    Args:
        node: using 指令的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    for child in node.children:
        if child.type in ("qualified_name", "identifier", "name_equals"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip()
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_kotlin(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 Kotlin 的 import 语句。
    
    支持路径导入和单个标识符导入。
    
    Args:
        node: import 语句的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    path_node = node.child_by_field_name("path")
    if path_node:
        raw = _read_text(path_node, source)
        module_name = raw.split(".")[-1].strip()
        if module_name:
            tgt_nid = _make_id(module_name)
            edges.append({
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })
        return
    # Fallback: find identifier child
    for child in node.children:
        if child.type == "identifier":
            raw = _read_text(child, source)
            tgt_nid = _make_id(raw)
            edges.append({
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })
            break


def _import_scala(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 Scala 的 import 语句。
    
    支持稳定标识符和通配符导入。
    
    Args:
        node: import 语句的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    for child in node.children:
        if child.type in ("stable_id", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split(".")[-1].strip("{} ")
            if module_name and module_name != "_":
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


def _import_php(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 PHP 的 namespace use 语句。
    
    支持命名空间别名和分组导入。
    
    Args:
        node: use 语句的 AST 节点
        source: 源代码字节数据
        file_nid: 当前文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表，用于添加新边
        str_path: 文件路径字符串
    """
    for child in node.children:
        if child.type in ("qualified_name", "name", "identifier"):
            raw = _read_text(child, source)
            module_name = raw.split("\\")[-1].strip()
            if module_name:
                tgt_nid = _make_id(module_name)
                edges.append({
                    "source": file_nid,
                    "target": tgt_nid,
                    "relation": "imports",
                    "confidence": "EXTRACTED",
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                    "weight": 1.0,
                })
            break


# ── C/C++ function name helpers ───────────────────────────────────────────────

def _get_c_func_name(node, source: bytes) -> str | None:
    """从 C 函数声明符中递归提取函数名。
    
    处理函数指针、数组声明等复杂声明符，找到最内层的标识符。
    
    Args:
        node: 函数声明符节点
        source: 源代码字节数据
        
    Returns:
        str | None: 函数名，如果找不到则返回 None
    """
    if node.type == "identifier":
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_c_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


def _get_cpp_func_name(node, source: bytes) -> str | None:
    """从 C++ 函数声明符中递归提取函数名。
    
    处理限定标识符和复杂声明符，找到最内层的函数名。
    
    Args:
        node: 函数声明符节点
        source: 源代码字节数据
        
    Returns:
        str | None: 函数名，如果找不到则返回 None
    """
    if node.type == "identifier":
        return _read_text(node, source)
    if node.type == "qualified_identifier":
        name_node = node.child_by_field_name("name")
        if name_node:
            return _read_text(name_node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_cpp_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


# ── JS/TS extra walk for arrow functions ──────────────────────────────────────

def _js_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                   nodes: list, edges: list, seen_ids: set, function_bodies: list,
                   parent_class_nid: str | None, add_node_fn, add_edge_fn) -> bool:
    """处理 JavaScript/TypeScript 的箭头函数。
    
    从 lexical_declaration 节点中提取箭头函数并添加到 AST 图中。
    
    Args:
        node: 当前遍历的 AST 节点
        source: 源代码字节数据
        file_nid: 文件的节点 ID
        stem: 文件名（不含扩展名）
        str_path: 文件路径字符串
        nodes: 节点列表
        edges: 边列表
        seen_ids: 已见节点 ID 集合
        function_bodies: 函数体列表（用于后续调用图分析）
        parent_class_nid: 父类节点 ID
        add_node_fn: 添加节点函数
        add_edge_fn: 添加边函数
        
    Returns:
        bool: 如果节点已处理则返回 True，否则返回 False
    """
    if node.type == "lexical_declaration":
        for child in node.children:
            if child.type == "variable_declarator":
                value = child.child_by_field_name("value")
                if value and value.type == "arrow_function":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        func_name = _read_text(name_node, source)
                        line = child.start_point[0] + 1
                        func_nid = _make_id(stem, func_name)
                        add_node_fn(func_nid, f"{func_name}()", line)
                        add_edge_fn(file_nid, func_nid, "contains", line)
                        body = value.child_by_field_name("body")
                        if body:
                            function_bodies.append((func_nid, body))
        return True
    return False


# ── C# extra walk for namespace declarations ──────────────────────────────────

def _csharp_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                       nodes: list, edges: list, seen_ids: set, function_bodies: list,
                       parent_class_nid: str | None, add_node_fn, add_edge_fn,
                       walk_fn) -> bool:
    """处理 C# 的命名空间声明。
    
    提取命名空间节点并递归遍历其内部的代码元素。
    
    Args:
        node: 当前遍历的 AST 节点
        source: 源代码字节数据
        file_nid: 文件的节点 ID
        stem: 文件名（不含扩展名）
        str_path: 文件路径字符串
        nodes: 节点列表
        edges: 边列表
        seen_ids: 已见节点 ID 集合
        function_bodies: 函数体列表
        parent_class_nid: 父类节点 ID
        add_node_fn: 添加节点函数
        add_edge_fn: 添加边函数
        walk_fn: 递归遍历函数
        
    Returns:
        bool: 如果节点已处理则返回 True，否则返回 False
    """
    if node.type == "namespace_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            ns_name = _read_text(name_node, source)
            ns_nid = _make_id(stem, ns_name)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_name, line)
            add_edge_fn(file_nid, ns_nid, "contains", line)
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                walk_fn(child, parent_class_nid)
        return True
    return False


# ── Swift extra walk for enum cases ──────────────────────────────────────────

def _swift_extra_walk(node, source: bytes, file_nid: str, stem: str, str_path: str,
                      nodes: list, edges: list, seen_ids: set, function_bodies: list,
                      parent_class_nid: str | None, add_node_fn, add_edge_fn) -> bool:
    """处理 Swift 的枚举 case。
    
    提取枚举 case 并创建 case_of 边连接到父枚举节点。
    
    Args:
        node: 当前遍历的 AST 节点
        source: 源代码字节数据
        file_nid: 文件的节点 ID
        stem: 文件名（不含扩展名）
        str_path: 文件路径字符串
        nodes: 节点列表
        edges: 边列表
        seen_ids: 已见节点 ID 集合
        function_bodies: 函数体列表
        parent_class_nid: 父枚举节点 ID
        add_node_fn: 添加节点函数
        add_edge_fn: 添加边函数
        
    Returns:
        bool: 如果节点已处理则返回 True，否则返回 False
    """
    if node.type == "enum_entry" and parent_class_nid:
        for child in node.children:
            if child.type == "simple_identifier":
                case_name = _read_text(child, source)
                case_nid = _make_id(parent_class_nid, case_name)
                line = node.start_point[0] + 1
                add_node_fn(case_nid, case_name, line)
                add_edge_fn(parent_class_nid, case_nid, "case_of", line)
        return True
    return False


# ── Language configs ──────────────────────────────────────────────────────────

# Python language configuration
_PYTHON_CONFIG = LanguageConfig(
    ts_module="tree_sitter_python",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_statement", "import_from_statement"}),
    call_types=frozenset({"call"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"attribute"}),
    call_accessor_field="attribute",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_python,
)

# JavaScript language configuration
_JS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_javascript",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    function_boundary_types=frozenset({"function_declaration", "arrow_function", "method_definition"}),
    import_handler=_import_js,
)

# TypeScript language configuration
_TS_CONFIG = LanguageConfig(
    ts_module="tree_sitter_typescript",
    ts_language_fn="language_typescript",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_declaration", "method_definition"}),
    import_types=frozenset({"import_statement"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_expression"}),
    call_accessor_field="property",
    function_boundary_types=frozenset({"function_declaration", "arrow_function", "method_definition"}),
    import_handler=_import_js,
)

# Java language configuration
_JAVA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_java",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"method_invocation"}),
    call_function_field="name",
    call_accessor_node_types=frozenset(),
    function_boundary_types=frozenset({"method_declaration", "constructor_declaration"}),
    import_handler=_import_java,
)

# C language configuration
_C_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c",
    class_types=frozenset(),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_c,
    resolve_function_name_fn=_get_c_func_name,
)

# C++ language configuration
_CPP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_cpp",
    class_types=frozenset({"class_specifier"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"preproc_include"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"field_expression", "qualified_identifier"}),
    call_accessor_field="field",
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_c,
    resolve_function_name_fn=_get_cpp_func_name,
)

# Ruby language configuration
_RUBY_CONFIG = LanguageConfig(
    ts_module="tree_sitter_ruby",
    class_types=frozenset({"class"}),
    function_types=frozenset({"method", "singleton_method"}),
    import_types=frozenset(),
    call_types=frozenset({"call"}),
    call_function_field="method",
    call_accessor_node_types=frozenset(),
    name_fallback_child_types=("constant", "scope_resolution", "identifier"),
    body_fallback_child_types=("body_statement",),
    function_boundary_types=frozenset({"method", "singleton_method"}),
)

# C# language configuration
_CSHARP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_c_sharp",
    class_types=frozenset({"class_declaration", "interface_declaration"}),
    function_types=frozenset({"method_declaration"}),
    import_types=frozenset({"using_directive"}),
    call_types=frozenset({"invocation_expression"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_access_expression"}),
    call_accessor_field="name",
    body_fallback_child_types=("declaration_list",),
    function_boundary_types=frozenset({"method_declaration"}),
    import_handler=_import_csharp,
)

# Kotlin language configuration
_KOTLIN_CONFIG = LanguageConfig(
    ts_module="tree_sitter_kotlin",
    class_types=frozenset({"class_declaration", "object_declaration"}),
    function_types=frozenset({"function_declaration"}),
    import_types=frozenset({"import_header"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    name_fallback_child_types=("simple_identifier",),
    body_fallback_child_types=("function_body", "class_body"),
    function_boundary_types=frozenset({"function_declaration"}),
    import_handler=_import_kotlin,
)

# Scala language configuration
_SCALA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_scala",
    class_types=frozenset({"class_definition", "object_definition"}),
    function_types=frozenset({"function_definition"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"field_expression"}),
    call_accessor_field="field",
    name_fallback_child_types=("identifier",),
    body_fallback_child_types=("template_body",),
    function_boundary_types=frozenset({"function_definition"}),
    import_handler=_import_scala,
)

# PHP language configuration
_PHP_CONFIG = LanguageConfig(
    ts_module="tree_sitter_php",
    ts_language_fn="language_php",
    class_types=frozenset({"class_declaration"}),
    function_types=frozenset({"function_definition", "method_declaration"}),
    import_types=frozenset({"namespace_use_clause"}),
    call_types=frozenset({"function_call_expression", "member_call_expression", "scoped_call_expression", "class_constant_access_expression"}),
    static_prop_types=frozenset({"scoped_property_access_expression"}),
    helper_fn_names=frozenset({"config"}),
    container_bind_methods=frozenset({"bind", "singleton", "scoped", "instance"}),
    event_listener_properties=frozenset({"listen", "subscribe"}),
    call_function_field="function",
    call_accessor_node_types=frozenset({"member_call_expression"}),
    call_accessor_field="name",
    name_fallback_child_types=("name",),
    body_fallback_child_types=("declaration_list", "compound_statement"),
    function_boundary_types=frozenset({"function_definition", "method_declaration"}),
    import_handler=_import_php,
)


def _import_lua(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 Lua 的 require 导入语句。
    
    从 variable_declaration 节点中提取 require('module') 形式的导入。
    
    Args:
        node: 变量声明节点
        source: 源代码字节数据
        file_nid: 文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表
        str_path: 文件路径字符串
    """
    text = _read_text(node, source)
    import re
    m = re.search(r"""require\s*[\('"]\s*['"]?([^'")\s]+)""", text)
    if m:
        module_name = m.group(1).split(".")[-1]
        if module_name:
            edges.append({
                "source": file_nid,
                "target": module_name,
                "relation": "imports",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": str_path,
                "source_location": str(node.start_point[0] + 1),
                "weight": 1.0,
            })


# Lua language configuration
_LUA_CONFIG = LanguageConfig(
    ts_module="tree_sitter_lua",
    ts_language_fn="language",
    class_types=frozenset(),
    function_types=frozenset({"function_declaration"}),
    import_types=frozenset({"variable_declaration"}),
    call_types=frozenset({"function_call"}),
    call_function_field="name",
    call_accessor_node_types=frozenset({"method_index_expression"}),
    call_accessor_field="name",
    name_fallback_child_types=("identifier", "method_index_expression"),
    body_fallback_child_types=("block",),
    function_boundary_types=frozenset({"function_declaration"}),
    import_handler=_import_lua,
)


def _import_swift(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str) -> None:
    """处理 Swift 的 import 语句。
    
    从标识符节点中提取导入的模块名。
    
    Args:
        node: import 语句节点
        source: 源代码字节数据
        file_nid: 文件的节点 ID
        stem: 文件名（不含扩展名）
        edges: 边列表
        str_path: 文件路径字符串
    """
    for child in node.children:
        if child.type == "identifier":
            raw = _read_text(child, source)
            tgt_nid = _make_id(raw)
            edges.append({
                "source": file_nid,
                "target": tgt_nid,
                "relation": "imports",
                "confidence": "EXTRACTED",
                "source_file": str_path,
                "source_location": f"L{node.start_point[0] + 1}",
                "weight": 1.0,
            })
            break


# Swift language configuration
_SWIFT_CONFIG = LanguageConfig(
    ts_module="tree_sitter_swift",
    class_types=frozenset({"class_declaration", "protocol_declaration"}),
    function_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}),
    import_types=frozenset({"import_declaration"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="",
    call_accessor_node_types=frozenset({"navigation_expression"}),
    call_accessor_field="",
    name_fallback_child_types=("simple_identifier", "type_identifier", "user_type"),
    body_fallback_child_types=("class_body", "protocol_body", "function_body", "enum_class_body"),
    function_boundary_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}),
    import_handler=_import_swift,
)


# ── Generic extractor ─────────────────────────────────────────────────────────

def _extract_generic(path: Path, config: LanguageConfig) -> dict:
    """通用的 AST 提取器，通过 LanguageConfig 驱动。
    
    根据语言配置解析源代码，提取类、函数、导入语句和调用关系。
    
    Args:
        path: 源代码文件路径
        config: 语言配置对象
        
    Returns:
        dict: 包含 nodes、edges 和 raw_calls 的字典
    """
    try:
        mod = importlib.import_module(config.ts_module)
        from tree_sitter import Language, Parser
        lang_fn = getattr(mod, config.ts_language_fn, None)
        if lang_fn is None:
            # Fallback for PHP: try "language_php" then "language"
            lang_fn = getattr(mod, "language", None)
        if lang_fn is None:
            return {"nodes": [], "edges": [], "error": f"No language function in {config.ts_module}"}
        language = Language(lang_fn())
    except ImportError:
        return {"nodes": [], "edges": [], "error": f"{config.ts_module} not installed"}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    try:
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object]] = []
    pending_listen_edges: list[tuple[str, str, int]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        """添加节点到图中。"""
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        """添加边到图中。"""
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        })

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def walk(node, parent_class_nid: str | None = None) -> None:
        """递归遍历 AST 节点。"""
        t = node.type

        # Import types
        if t in config.import_types:
            if config.import_handler:
                config.import_handler(node, source, file_nid, stem, edges, str_path)
            return

        # Class types
        if t in config.class_types:
            # Resolve class name
            name_node = node.child_by_field_name(config.name_field)
            if name_node is None:
                for child in node.children:
                    if child.type in config.name_fallback_child_types:
                        name_node = child
                        break
            if not name_node:
                return
            class_name = _read_text(name_node, source)
            class_nid = _make_id(stem, class_name)
            line = node.start_point[0] + 1
            add_node(class_nid, class_name, line)
            add_edge(file_nid, class_nid, "contains", line)

            # Python-specific: inheritance
            if config.ts_module == "tree_sitter_python":
                args = node.child_by_field_name("superclasses")
                if args:
                    for arg in args.children:
                        if arg.type == "identifier":
                            base = _read_text(arg, source)
                            base_nid = _make_id(stem, base)
                            if base_nid not in seen_ids:
                                base_nid = _make_id(base)
                                if base_nid not in seen_ids:
                                    nodes.append({
                                        "id": base_nid,
                                        "label": base,
                                        "file_type": "code",
                                        "source_file": "",
                                        "source_location": "",
                                    })
                                    seen_ids.add(base_nid)
                            add_edge(class_nid, base_nid, "inherits", line)

            # Swift-specific: conformance / inheritance
            if config.ts_module == "tree_sitter_swift":
                for child in node.children:
                    if child.type == "inheritance_specifier":
                        for sub in child.children:
                            if sub.type in ("user_type", "type_identifier"):
                                base = _read_text(sub, source)
                                base_nid = _make_id(stem, base)
                                if base_nid not in seen_ids:
                                    base_nid = _make_id(base)
                                    if base_nid not in seen_ids:
                                        nodes.append({
                                            "id": base_nid,
                                            "label": base,
                                            "file_type": "code",
                                            "source_file": "",
                                            "source_location": "",
                                        })
                                        seen_ids.add(base_nid)
                                add_edge(class_nid, base_nid, "inherits", line)

            # C#-specific: inheritance / interface implementation via base_list
            if config.ts_module == "tree_sitter_c_sharp":
                for child in node.children:
                    if child.type == "base_list":
                        for sub in child.children:
                            if sub.type in ("identifier", "generic_name"):
                                if sub.type == "generic_name":
                                    name_child = sub.child_by_field_name("name")
                                    base = _read_text(name_child, source) if name_child else _read_text(sub.children[0], source)
                                else:
                                    base = _read_text(sub, source)
                                base_nid = _make_id(stem, base)
                                if base_nid not in seen_ids:
                                    base_nid = _make_id(base)
                                    if base_nid not in seen_ids:
                                        nodes.append({
                                            "id": base_nid,
                                            "label": base,
                                            "file_type": "code",
                                            "source_file": "",
                                            "source_location": "",
                                        })
                                        seen_ids.add(base_nid)
                                add_edge(class_nid, base_nid, "inherits", line)

            # Find body and recurse
            body = _find_body(node, config)
            if body:
                for child in body.children:
                    walk(child, parent_class_nid=class_nid)
            return

        # Event listener property arrays: $listen = [Event::class => [Listener::class]]
        if (t == "property_declaration"
                and parent_class_nid
                and config.event_listener_properties):
            for element in node.children:
                if element.type != "property_element":
                    continue
                prop_name: str | None = None
                array_node = None
                for c in element.children:
                    if c.type == "variable_name":
                        for sc in c.children:
                            if sc.type == "name":
                                prop_name = _read_text(sc, source)
                                break
                    elif c.type == "array_creation_expression":
                        array_node = c
                if (prop_name is None
                        or prop_name not in config.event_listener_properties
                        or array_node is None):
                    continue
                for entry in array_node.children:
                    if entry.type != "array_element_initializer":
                        continue
                    event_cls: str | None = None
                    listener_arr = None
                    for sub in entry.children:
                        if sub.type == "class_constant_access_expression" and event_cls is None:
                            for sc in sub.children:
                                if sc.is_named and sc.type in ("name", "qualified_name"):
                                    event_cls = _read_text(sc, source)
                                    break
                        elif sub.type == "array_creation_expression":
                            listener_arr = sub
                    if not event_cls or listener_arr is None:
                        continue
                    for listener_entry in listener_arr.children:
                        if listener_entry.type != "array_element_initializer":
                            continue
                        for item in listener_entry.children:
                            if item.type != "class_constant_access_expression":
                                continue
                            for sc in item.children:
                                if sc.is_named and sc.type in ("name", "qualified_name"):
                                    listener_cls = _read_text(sc, source)
                                    line_no = item.start_point[0] + 1
                                    pending_listen_edges.append((event_cls, listener_cls, line_no))
                                    break
                            break
            return

        # Function types
        if t in config.function_types:
            # Swift deinit/subscript have no name field — resolve before generic fallback
            if t == "deinit_declaration":
                func_name: str | None = "deinit"
            elif t == "subscript_declaration":
                func_name = "subscript"
            elif config.resolve_function_name_fn is not None:
                # C/C++ style: use declarator
                declarator = node.child_by_field_name("declarator")
                func_name = None
                if declarator:
                    func_name = config.resolve_function_name_fn(declarator, source)
            else:
                name_node = node.child_by_field_name(config.name_field)
                if name_node is None:
                    for child in node.children:
                        if child.type in config.name_fallback_child_types:
                            name_node = child
                            break
                func_name = _read_text(name_node, source) if name_node else None

            if not func_name:
                return

            line = node.start_point[0] + 1
            if parent_class_nid:
                func_nid = _make_id(parent_class_nid, func_name)
                add_node(func_nid, f".{func_name}()", line)
                add_edge(parent_class_nid, func_nid, "method", line)
            else:
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)

            body = _find_body(node, config)
            if body:
                function_bodies.append((func_nid, body))
            return

        # JS/TS arrow functions and C# namespaces — language-specific extra handling
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            if _js_extra_walk(node, source, file_nid, stem, str_path,
                              nodes, edges, seen_ids, function_bodies,
                              parent_class_nid, add_node, add_edge):
                return

        if config.ts_module == "tree_sitter_c_sharp":
            if _csharp_extra_walk(node, source, file_nid, stem, str_path,
                                   nodes, edges, seen_ids, function_bodies,
                                   parent_class_nid, add_node, add_edge, walk):
                return

        if config.ts_module == "tree_sitter_swift":
            if _swift_extra_walk(node, source, file_nid, stem, str_path,
                                  nodes, edges, seen_ids, function_bodies,
                                  parent_class_nid, add_node, add_edge):
                return

        # Default: recurse
        for child in node.children:
            walk(child, parent_class_nid=None)

    walk(root)

    # ── Call-graph pass ───────────────────────────────────────────────────────
    label_to_nid: dict[str, str] = {}
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised.lower()] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    seen_static_ref_pairs: set[tuple[str, str, str]] = set()
    seen_helper_ref_pairs: set[tuple[str, str, str]] = set()
    seen_bind_pairs: set[tuple[str, str, str]] = set()
    raw_calls: list[dict] = []  # unresolved calls for cross-file resolution in extract()

    def _php_class_const_scope(n) -> str | None:
        """从 PHP 类常量访问表达式中提取类名。"""
        scope = n.child_by_field_name("scope")
        if scope is None:
            for c in n.children:
                if c.is_named and c.type in ("name", "qualified_name", "identifier"):
                    scope = c
                    break
        if scope is None:
            return None
        return _read_text(scope, source)

    def walk_calls(node, caller_nid: str) -> None:
        """递归遍历函数调用节点，构建调用关系图。"""
        if node.type in config.function_boundary_types:
            return

        if node.type in config.call_types:
            callee_name: str | None = None

            # Special handling per language
            if config.ts_module == "tree_sitter_swift":
                # Swift: first child may be simple_identifier or navigation_expression
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "simple_identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "navigation_expression":
                        for child in first.children:
                            if child.type == "navigation_suffix":
                                for sc in child.children:
                                    if sc.type == "simple_identifier":
                                        callee_name = _read_text(sc, source)
            elif config.ts_module == "tree_sitter_kotlin":
                # Kotlin: first child may be simple_identifier or navigation_expression
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "simple_identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "navigation_expression":
                        for child in reversed(first.children):
                            if child.type == "simple_identifier":
                                callee_name = _read_text(child, source)
                                break
            elif config.ts_module == "tree_sitter_scala":
                # Scala: first child
                first = node.children[0] if node.children else None
                if first:
                    if first.type == "identifier":
                        callee_name = _read_text(first, source)
                    elif first.type == "field_expression":
                        field = first.child_by_field_name("field")
                        if field:
                            callee_name = _read_text(field, source)
                        else:
                            for child in reversed(first.children):
                                if child.type == "identifier":
                                    callee_name = _read_text(child, source)
                                    break
            elif config.ts_module == "tree_sitter_c_sharp" and node.type == "invocation_expression":
                # C#: try name field, then first named child
                name_node = node.child_by_field_name("name")
                if name_node:
                    callee_name = _read_text(name_node, source)
                else:
                    for child in node.children:
                        if child.is_named:
                            raw = _read_text(child, source)
                            if "." in raw:
                                callee_name = raw.split(".")[-1]
                            else:
                                callee_name = raw
                            break
            elif config.ts_module == "tree_sitter_php":
                # PHP: distinguish call expression subtypes
                if node.type == "function_call_expression":
                    func_node = node.child_by_field_name("function")
                    if func_node:
                        callee_name = _read_text(func_node, source)
                elif node.type == "scoped_call_expression":
                    # Static method call: Helper::format() → callee = "Helper"
                    scope_node = node.child_by_field_name("scope")
                    if scope_node:
                        callee_name = _read_text(scope_node, source)
                else:
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        callee_name = _read_text(name_node, source)
            elif config.ts_module == "tree_sitter_cpp":
                # C++: function field, then field_expression/qualified_identifier
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type in ("field_expression", "qualified_identifier"):
                        name = func_node.child_by_field_name("field") or func_node.child_by_field_name("name")
                        if name:
                            callee_name = _read_text(name, source)
            else:
                # Generic: get callee from call_function_field
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type in config.call_accessor_node_types:
                        if config.call_accessor_field:
                            attr = func_node.child_by_field_name(config.call_accessor_field)
                            if attr:
                                callee_name = _read_text(attr, source)
                    else:
                        # Try reading the node directly (e.g. Java name field is the callee)
                        callee_name = _read_text(func_node, source)

            if callee_name:
                tgt_nid = label_to_nid.get(callee_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })
                elif callee_name and not tgt_nid:
                    # Callee not in this file — save for cross-file resolution in extract()
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })

            # Helper function calls: config('foo.bar') → uses_config edge to "foo"
            if (callee_name and callee_name in config.helper_fn_names):
                args_node = node.child_by_field_name("arguments")
                first_key: str | None = None
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "string":
                                for sc in inner.children:
                                    if sc.type == "string_content":
                                        first_key = _read_text(sc, source)
                                        break
                                break
                        if first_key:
                            break
                if first_key:
                    segment = first_key.split(".")[0]
                    tgt_nid = (label_to_nid.get(segment.lower())
                               or label_to_nid.get(f"{segment}.php".lower()))
                    if tgt_nid and tgt_nid != caller_nid:
                        relation = f"uses_{callee_name}"
                        pair3 = (caller_nid, tgt_nid, relation)
                        if pair3 not in seen_helper_ref_pairs:
                            seen_helper_ref_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({
                                "source": caller_nid,
                                "target": tgt_nid,
                                "relation": relation,
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            })

            # Service container bindings: $this->app->bind(Foo::class, Bar::class)
            if (node.type == "member_call_expression"
                    and callee_name
                    and callee_name in config.container_bind_methods):
                args_node = node.child_by_field_name("arguments")
                class_args: list[str] = []
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "class_constant_access_expression":
                                cls = _php_class_const_scope(inner)
                                if cls:
                                    class_args.append(cls)
                                break
                        if len(class_args) >= 2:
                            break
                if len(class_args) == 2:
                    contract_name, impl_name = class_args
                    contract_nid = label_to_nid.get(contract_name.lower())
                    impl_nid = label_to_nid.get(impl_name.lower())
                    if contract_nid and impl_nid and contract_nid != impl_nid:
                        pair3 = (contract_nid, impl_nid, "bound_to")
                        if pair3 not in seen_bind_pairs:
                            seen_bind_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({
                                "source": contract_nid,
                                "target": impl_nid,
                                "relation": "bound_to",
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            })

        # Static property access: Foo::$bar → uses_static_prop edge
        if node.type in config.static_prop_types:
            scope_node = node.child_by_field_name("scope")
            if scope_node is None:
                for child in node.children:
                    if child.is_named and child.type in ("name", "qualified_name", "identifier"):
                        scope_node = child
                        break
            if scope_node is not None:
                class_name = _read_text(scope_node, source)
                tgt_nid = label_to_nid.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "uses_static_prop")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "uses_static_prop",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })

        # PHP class constant access: Foo::BAR → references_constant edge
        if config.ts_module == "tree_sitter_php" and node.type == "class_constant_access_expression":
            class_name = _php_class_const_scope(node)
            if class_name:
                tgt_nid = label_to_nid.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "references_constant")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "references_constant",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })

        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    # ── Event listener pass ───────────────────────────────────────────────────
    seen_listen_pairs: set[tuple[str, str]] = set()
    for event_name, listener_name, line in pending_listen_edges:
        event_nid = label_to_nid.get(event_name.lower())
        listener_nid = label_to_nid.get(listener_name.lower())
        if not event_nid or not listener_nid or event_nid == listener_nid:
            continue
        pair2 = (event_nid, listener_nid)
        if pair2 in seen_listen_pairs:
            continue
        seen_listen_pairs.add(pair2)
        edges.append({
            "source": event_nid,
            "target": listener_nid,
            "relation": "listened_by",
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    # ── Clean edges ───────────────────────────────────────────────────────────
    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from")):
            clean_edges.append(edge)

    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}


# ── Python rationale extraction ───────────────────────────────────────────────

_RATIONALE_PREFIXES = ("# NOTE:", "# IMPORTANT:", "# HACK:", "# WHY:", "# RATIONALE:", "# TODO:", "# FIXME:")


def _extract_python_rationale(path: Path, result: dict) -> None:
    """提取 Python 代码中的文档字符串和理性注释。
    
    后处理步骤：从 Python 源文件中提取模块级、类级、函数级的文档字符串，
    以及特定前缀的注释（如 # NOTE:、# IMPORTANT: 等），
    将它们作为独立的"理性"节点添加到图中。
    
    Args:
        path: Python 源文件路径
        result: 包含 nodes 和 edges 的字典，会被就地修改
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
        language = Language(tspython.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception:
        return

    stem = path.stem
    str_path = str(path)
    nodes = result["nodes"]
    edges = result["edges"]
    seen_ids = {n["id"] for n in nodes}
    file_nid = _make_id(str(path))

    def _get_docstring(body_node) -> tuple[str, int] | None:
        """从函数体或类体中提取文档字符串。"""
        if not body_node:
            return None
        for child in body_node.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type in ("string", "concatenated_string"):
                        text = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="replace")
                        text = text.strip("\"'").strip('"""').strip("'''").strip()
                        if len(text) > 20:
                            return text, child.start_point[0] + 1
            break
        return None

    def _add_rationale(text: str, line: int, parent_nid: str) -> None:
        """添加理性注释节点和边。"""
        label = text[:80].replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
        rid = _make_id(stem, "rationale", str(line))
        if rid not in seen_ids:
            seen_ids.add(rid)
            nodes.append({
                "id": rid,
                "label": label,
                "file_type": "rationale",
                "source_file": str_path,
                "source_location": f"L{line}",
            })
        edges.append({
            "source": rid,
            "target": parent_nid,
            "relation": "rationale_for",
            "confidence": "EXTRACTED",
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": 1.0,
        })

    # Module-level docstring
    ds = _get_docstring(root)
    if ds:
        _add_rationale(ds[0], ds[1], file_nid)

    # Class and function docstrings
    def walk_docstrings(node, parent_nid: str) -> None:
        """递归遍历类和方法，提取文档字符串。"""
        t = node.type
        if t == "class_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node and body:
                class_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                nid = _make_id(stem, class_name)
                ds = _get_docstring(body)
                if ds:
                    _add_rationale(ds[0], ds[1], nid)
                for child in body.children:
                    walk_docstrings(child, nid)
            return
        if t == "function_definition":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node and body:
                func_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                nid = _make_id(parent_nid, func_name) if parent_nid != file_nid else _make_id(stem, func_name)
                ds = _get_docstring(body)
                if ds:
                    _add_rationale(ds[0], ds[1], nid)
            return
        for child in node.children:
            walk_docstrings(child, parent_nid)

    walk_docstrings(root, file_nid)

    # Rationale comments (# NOTE:, # IMPORTANT:, etc.)
    source_text = source.decode("utf-8", errors="replace")
    for lineno, line_text in enumerate(source_text.splitlines(), start=1):
        stripped = line_text.strip()
        if any(stripped.startswith(p) for p in _RATIONALE_PREFIXES):
            _add_rationale(stripped, lineno, file_nid)


# ── Public API ────────────────────────────────────────────────────────────────

def extract_python(path: Path) -> dict:
    """从 Python 文件中提取类、函数和导入语句。
    
    使用 tree-sitter AST 解析，同时提取文档字符串和理性注释。
    
    Args:
        path: Python 文件路径（.py）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    result = _extract_generic(path, _PYTHON_CONFIG)
    if "error" not in result:
        _extract_python_rationale(path, result)
    return result


def extract_js(path: Path) -> dict:
    """从 JavaScript/TypeScript 文件中提取类、函数、箭头函数和导入语句。
    
    根据文件扩展名自动选择 JS 或 TS 解析器。
    
    Args:
        path: JS/TS 文件路径（.js/.ts/.tsx/.jsx/.mjs）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    config = _TS_CONFIG if path.suffix in (".ts", ".tsx") else _JS_CONFIG
    return _extract_generic(path, config)


def extract_java(path: Path) -> dict:
    """从 Java 文件中提取类、接口、方法、构造函数和导入语句。
    
    Args:
        path: Java 文件路径（.java）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _JAVA_CONFIG)


def extract_c(path: Path) -> dict:
    """从 C 文件中提取函数和 include 指令。
    
    Args:
        path: C 文件路径（.c/.h）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _C_CONFIG)


def extract_cpp(path: Path) -> dict:
    """从 C++ 文件中提取函数、类和 include 指令。
    
    Args:
        path: C++ 文件路径（.cpp/.cc/.cxx/.hpp）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _CPP_CONFIG)


def extract_ruby(path: Path) -> dict:
    """从 Ruby 文件中提取类、方法、单例方法和调用。
    
    Args:
        path: Ruby 文件路径（.rb）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _RUBY_CONFIG)


def extract_csharp(path: Path) -> dict:
    """从 C# 文件中提取类、接口、方法、命名空间和 using 语句。
    
    Args:
        path: C# 文件路径（.cs）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _CSHARP_CONFIG)


def extract_kotlin(path: Path) -> dict:
    """从 Kotlin 文件中提取类、对象、函数和导入语句。
    
    Args:
        path: Kotlin 文件路径（.kt/.kts）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _KOTLIN_CONFIG)


def extract_scala(path: Path) -> dict:
    """从 Scala 文件中提取类、对象、函数和导入语句。
    
    Args:
        path: Scala 文件路径（.scala）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _SCALA_CONFIG)


def extract_php(path: Path) -> dict:
    """从 PHP 文件中提取类、函数、方法、命名空间 use 和调用。
    
    Args:
        path: PHP 文件路径（.php）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _PHP_CONFIG)


def extract_blade(path: Path) -> dict:
    """从 Blade 模板中提取 @include、<livewire:> 组件和 wire:click 绑定。
    
    使用正则表达式匹配 Blade 特有的语法元素。
    
    Args:
        path: Blade 模板文件路径（.blade.php）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    import re
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"error": f"cannot read {path}"}

    file_nid = _make_id(str(path))
    nodes = [{"id": file_nid, "label": path.name, "file_type": "code",
              "source_file": str(path), "source_location": None}]
    edges = []

    # @include('path.to.partial') or @include("path.to.partial")
    for m in re.finditer(r"@include\(['\"]([^'\"]+)['\"]", src):
        tgt = m.group(1).replace(".", "/")
        tgt_nid = _make_id(tgt)
        if tgt_nid not in {n["id"] for n in nodes}:
            nodes.append({"id": tgt_nid, "label": m.group(1), "file_type": "code",
                          "source_file": str(path), "source_location": None})
        edges.append({"source": file_nid, "target": tgt_nid, "relation": "includes",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": str(path), "source_location": None, "weight": 1.0})

    # <livewire:component.name /> or <livewire:component.name>
    for m in re.finditer(r"<livewire:([\w.\-]+)", src):
        tgt_nid = _make_id(m.group(1))
        if tgt_nid not in {n["id"] for n in nodes}:
            nodes.append({"id": tgt_nid, "label": m.group(1), "file_type": "code",
                          "source_file": str(path), "source_location": None})
        edges.append({"source": file_nid, "target": tgt_nid, "relation": "uses_component",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": str(path), "source_location": None, "weight": 1.0})

    # wire:click="methodName"
    for m in re.finditer(r'wire:click=["\']([^"\']+)["\']', src):
        tgt_nid = _make_id(m.group(1))
        if tgt_nid not in {n["id"] for n in nodes}:
            nodes.append({"id": tgt_nid, "label": m.group(1), "file_type": "code",
                          "source_file": str(path), "source_location": None})
        edges.append({"source": file_nid, "target": tgt_nid, "relation": "binds_method",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": str(path), "source_location": None, "weight": 1.0})

    return {"nodes": nodes, "edges": edges}


def extract_dart(path: Path) -> dict:
    """从 Dart 文件中提取类、混入、函数、导入和调用（使用正则表达式）。
    
    注意：由于 tree-sitter-dart 可能不可用，这里使用正则表达式作为备选方案。
    
    Args:
        path: Dart 文件路径（.dart）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"error": f"cannot read {path}"}

    file_nid = _make_id(str(path))
    nodes = [{"id": file_nid, "label": path.name, "file_type": "code",
              "source_file": str(path), "source_location": None}]
    edges = []
    defined: set[str] = set()

    # Classes and mixins
    for m in re.finditer(r"^\s*(?:abstract\s+)?(?:class|mixin)\s+(\w+)", src, re.MULTILINE):
        nid = _make_id(str(path), m.group(1))
        if nid not in defined:
            nodes.append({"id": nid, "label": m.group(1), "file_type": "code",
                          "source_file": str(path), "source_location": None})
            edges.append({"source": file_nid, "target": nid, "relation": "defines",
                          "confidence": "EXTRACTED", "confidence_score": 1.0,
                          "source_file": str(path), "source_location": None, "weight": 1.0})
            defined.add(nid)

    # Top-level and member functions/methods
    for m in re.finditer(r"^\s*(?:static\s+|async\s+)?(?:\w+\s+)+(\w+)\s*\(", src, re.MULTILINE):
        name = m.group(1)
        if name in {"if", "for", "while", "switch", "catch", "return"}:
            continue
        nid = _make_id(str(path), name)
        if nid not in defined:
            nodes.append({"id": nid, "label": name, "file_type": "code",
                          "source_file": str(path), "source_location": None})
            edges.append({"source": file_nid, "target": nid, "relation": "defines",
                          "confidence": "EXTRACTED", "confidence_score": 1.0,
                          "source_file": str(path), "source_location": None, "weight": 1.0})
            defined.add(nid)

    # import 'package:...' or import '...'
    for m in re.finditer(r"""^import\s+['"]([^'"]+)['"]""", src, re.MULTILINE):
        pkg = m.group(1)
        tgt_nid = _make_id(pkg)
        if tgt_nid not in defined:
            nodes.append({"id": tgt_nid, "label": pkg, "file_type": "code",
                          "source_file": str(path), "source_location": None})
            defined.add(tgt_nid)
        edges.append({"source": file_nid, "target": tgt_nid, "relation": "imports",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": str(path), "source_location": None, "weight": 1.0})

    return {"nodes": nodes, "edges": edges}


def extract_verilog(path: Path) -> dict:
    """从 Verilog/SystemVerilog 文件中提取模块、函数、任务、包导入和实例化。
    
    Args:
        path: Verilog 文件路径（.v/.sv）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_verilog as tsverilog
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_verilog not installed"}

    try:
        language = Language(tsverilog.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}",
                          "confidence_score": 1.0})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", score: float = 1.0) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": confidence, "confidence_score": score,
                      "source_file": str_path, "source_location": f"L{line}", "weight": 1.0})

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def walk(node, module_nid: str | None = None) -> None:
        t = node.type

        if t == "module_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                mod_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                nid = _make_id(stem, mod_name)
                add_node(nid, mod_name, line)
                add_edge(file_nid, nid, "defines", line)
                for child in node.children:
                    walk(child, nid)
                return

        elif t in ("function_declaration", "function_prototype"):
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                parent = module_nid or file_nid
                nid = _make_id(parent, func_name)
                add_node(nid, f"{func_name}()", line)
                add_edge(parent, nid, "contains", line)

        elif t == "task_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                task_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                parent = module_nid or file_nid
                nid = _make_id(parent, task_name)
                add_node(nid, task_name, line)
                add_edge(parent, nid, "contains", line)

        elif t == "package_import_declaration":
            for child in node.children:
                if child.type == "package_import_item":
                    pkg_text = _read_text(child, source)
                    pkg_name = pkg_text.split("::")[0].strip()
                    if pkg_name:
                        line = node.start_point[0] + 1
                        tgt_nid = _make_id(pkg_name)
                        add_node(tgt_nid, pkg_name, line)
                        src = module_nid or file_nid
                        add_edge(src, tgt_nid, "imports_from", line)

        elif t == "module_instantiation":
            # module_type instantiates another module
            type_node = node.child_by_field_name("module_type")
            if type_node and module_nid:
                inst_type = _read_text(type_node, source).strip()
                if inst_type:
                    line = node.start_point[0] + 1
                    tgt_nid = _make_id(inst_type)
                    add_node(tgt_nid, inst_type, line)
                    add_edge(module_nid, tgt_nid, "instantiates", line)

        for child in node.children:
            walk(child, module_nid)

    walk(root)
    return {"nodes": nodes, "edges": edges}


def extract_lua(path: Path) -> dict:
    """从 Lua 文件中提取函数、方法、require() 导入和调用。
    
    Args:
        path: Lua 文件路径（.lua/.toc）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _LUA_CONFIG)


def extract_swift(path: Path) -> dict:
    """从 Swift 文件中提取类、结构体、协议、函数、导入和调用。
    
    Args:
        path: Swift 文件路径（.swift）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    return _extract_generic(path, _SWIFT_CONFIG)


# ── Julia extractor (custom walk) ────────────────────────────────────────────

def extract_julia(path: Path) -> dict:
    """从 Julia 文件中提取模块、结构体、函数、导入和调用。
    
    使用自定义 AST 遍历，因为 Julia 的函数定义语法较为特殊。
    
    Args:
        path: Julia 文件路径（.jl）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_julia as tsjulia
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-julia not installed"}

    try:
        language = Language(tsjulia.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        })

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def _func_name_from_signature(sig_node) -> str | None:
        """从 Julia 签名节点中提取函数名。"""
        for child in sig_node.children:
            if child.type == "call_expression":
                callee = child.children[0] if child.children else None
                if callee and callee.type == "identifier":
                    return _read_text(callee, source)
        return None

    def walk_calls(body_node, func_nid: str) -> None:
        """递归遍历函数体，提取调用关系。"""
        if body_node is None:
            return
        t = body_node.type
        if t in ("function_definition", "short_function_definition"):
            return
        if t == "call_expression" and body_node.children:
            callee = body_node.children[0]
            # Direct call: foo(...)
            if callee.type == "identifier":
                callee_name = _read_text(callee, source)
                target_nid = _make_id(stem, callee_name)
                add_edge(func_nid, target_nid, "calls", body_node.start_point[0] + 1,
                         confidence="EXTRACTED")
            # Method call: obj.method(...)
            elif callee.type == "field_expression" and len(callee.children) >= 3:
                method_node = callee.children[-1]
                method_name = _read_text(method_node, source)
                target_nid = _make_id(stem, method_name)
                add_edge(func_nid, target_nid, "calls", body_node.start_point[0] + 1,
                         confidence="EXTRACTED")
        for child in body_node.children:
            walk_calls(child, func_nid)

    def walk(node, scope_nid: str) -> None:
        """递归遍历 Julia AST。"""
        t = node.type

        # Module
        if t == "module_definition":
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node:
                mod_name = _read_text(name_node, source)
                mod_nid = _make_id(stem, mod_name)
                line = node.start_point[0] + 1
                add_node(mod_nid, mod_name, line)
                add_edge(file_nid, mod_nid, "defines", line)
                for child in node.children:
                    walk(child, mod_nid)
            return

        # Struct (struct / mutable struct — both map to struct_definition in tree-sitter-julia)
        if t == "struct_definition":
            # type_head may contain: identifier (simple) or binary_expression (Foo <: Bar)
            type_head = next((c for c in node.children if c.type == "type_head"), None)
            if type_head:
                bin_expr = next((c for c in type_head.children if c.type == "binary_expression"), None)
                if bin_expr:
                    # First identifier is the struct name, last is the supertype
                    identifiers = [c for c in bin_expr.children if c.type == "identifier"]
                    if identifiers:
                        struct_name = _read_text(identifiers[0], source)
                        struct_nid = _make_id(stem, struct_name)
                        line = node.start_point[0] + 1
                        add_node(struct_nid, struct_name, line)
                        add_edge(scope_nid, struct_nid, "defines", line)
                        if len(identifiers) >= 2:
                            super_name = _read_text(identifiers[-1], source)
                            add_edge(struct_nid, _make_id(stem, super_name), "inherits",
                                     line, confidence="EXTRACTED")
                else:
                    name_node = next((c for c in type_head.children if c.type == "identifier"), None)
                    if name_node:
                        struct_name = _read_text(name_node, source)
                        struct_nid = _make_id(stem, struct_name)
                        line = node.start_point[0] + 1
                        add_node(struct_nid, struct_name, line)
                        add_edge(scope_nid, struct_nid, "defines", line)
            return

        # Abstract type
        if t == "abstract_definition":
            # type_head > identifier
            type_head = next((c for c in node.children if c.type == "type_head"), None)
            if type_head:
                name_node = next((c for c in type_head.children if c.type == "identifier"), None)
                if name_node:
                    abs_name = _read_text(name_node, source)
                    abs_nid = _make_id(stem, abs_name)
                    line = node.start_point[0] + 1
                    add_node(abs_nid, abs_name, line)
                    add_edge(scope_nid, abs_nid, "defines", line)
            return

        # Function: function foo(...) ... end
        if t == "function_definition":
            sig_node = next((c for c in node.children if c.type == "signature"), None)
            if sig_node:
                func_name = _func_name_from_signature(sig_node)
                if func_name:
                    func_nid = _make_id(stem, func_name)
                    line = node.start_point[0] + 1
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(scope_nid, func_nid, "defines", line)
                    function_bodies.append((func_nid, node))
            return

        # Short function: foo(x) = expr
        if t == "assignment":
            lhs = node.children[0] if node.children else None
            if lhs and lhs.type == "call_expression" and lhs.children:
                callee = lhs.children[0]
                if callee.type == "identifier":
                    func_name = _read_text(callee, source)
                    func_nid = _make_id(stem, func_name)
                    line = node.start_point[0] + 1
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(scope_nid, func_nid, "defines", line)
                    # Only walk the RHS (index 2 after lhs and operator) to avoid self-loops
                    rhs = node.children[-1] if len(node.children) >= 3 else None
                    if rhs:
                        function_bodies.append((func_nid, rhs))
            return

        # Using / Import
        if t in ("using_statement", "import_statement"):
            line = node.start_point[0] + 1
            for child in node.children:
                if child.type == "identifier":
                    mod_name = _read_text(child, source)
                    imp_nid = _make_id(mod_name)
                    add_node(imp_nid, mod_name, line)
                    add_edge(scope_nid, imp_nid, "imports", line)
                elif child.type == "selected_import":
                    identifiers = [c for c in child.children if c.type == "identifier"]
                    if identifiers:
                        pkg_name = _read_text(identifiers[0], source)
                        pkg_nid = _make_id(pkg_name)
                        add_node(pkg_nid, pkg_name, line)
                        add_edge(scope_nid, pkg_nid, "imports", line)
            return

        for child in node.children:
            walk(child, scope_nid)

    walk(root, file_nid)

    for func_nid, body_node in function_bodies:
        # For function_definition nodes, walk children directly to avoid
        # the boundary check returning early on the top-level node itself.
        # Skip the "signature" child — it contains the function's own call_expression
        # which would create a self-loop.
        if body_node.type == "function_definition":
            for child in body_node.children:
                if child.type != "signature":
                    walk_calls(child, func_nid)
        else:
            walk_calls(body_node, func_nid)

    return {"nodes": nodes, "edges": edges}


# ── Go extractor (custom walk) ────────────────────────────────────────────────

def extract_go(path: Path) -> dict:
    """从 Go 文件中提取函数、方法、类型声明和导入语句。
    
    使用自定义 AST 遍历处理 Go 特有的方法接收者语法和包范围。
    
    Args:
        path: Go 文件路径（.go）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_go as tsgo
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-go not installed"}

    try:
        language = Language(tsgo.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    # Use directory name as package scope so methods on the same type across
    # multiple files in a package share one canonical type node.
    pkg_scope = path.parent.name or stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        })

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def walk(node) -> None:
        """递归遍历 Go AST。"""
        t = node.type

        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            receiver_type: str | None = None
            if receiver:
                for param in receiver.children:
                    if param.type == "parameter_declaration":
                        type_node = param.child_by_field_name("type")
                        if type_node:
                            raw = _read_text(type_node, source).lstrip("*").strip()
                            receiver_type = raw
                        break
            name_node = node.child_by_field_name("name")
            if name_node:
                method_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if receiver_type:
                    parent_nid = _make_id(pkg_scope, receiver_type)
                    add_node(parent_nid, receiver_type, line)
                    method_nid = _make_id(parent_nid, method_name)
                    add_node(method_nid, f".{method_name}()", line)
                    add_edge(parent_nid, method_nid, "method", line)
                else:
                    method_nid = _make_id(stem, method_name)
                    add_node(method_nid, f"{method_name}()", line)
                    add_edge(file_nid, method_nid, "contains", line)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((method_nid, body))
            return

        if t == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        type_name = _read_text(name_node, source)
                        line = child.start_point[0] + 1
                        type_nid = _make_id(pkg_scope, type_name)
                        add_node(type_nid, type_name, line)
                        add_edge(file_nid, type_nid, "contains", line)
            return

        if t == "import_declaration":
            for child in node.children:
                if child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            path_node = spec.child_by_field_name("path")
                            if path_node:
                                raw = _read_text(path_node, source).strip('"')
                                # Prefix with go_pkg_ so stdlib names (e.g. "context")
                                # don't collide with local files of the same basename.
                                tgt_nid = _make_id("go", "pkg", raw)
                                add_edge(file_nid, tgt_nid, "imports_from", spec.start_point[0] + 1)
                elif child.type == "import_spec":
                    path_node = child.child_by_field_name("path")
                    if path_node:
                        raw = _read_text(path_node, source).strip('"')
                        tgt_nid = _make_id("go", "pkg", raw)
                        add_edge(file_nid, tgt_nid, "imports_from", child.start_point[0] + 1)
            return

        for child in node.children:
            walk(child)

    walk(root)

    label_to_nid: dict[str, str] = {}
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised.lower()] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def walk_calls(node, caller_nid: str) -> None:
        """递归遍历函数调用，构建调用关系图。"""
        if node.type in ("function_declaration", "method_declaration"):
            return
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            callee_name: str | None = None
            if func_node:
                if func_node.type == "identifier":
                    callee_name = _read_text(func_node, source)
                elif func_node.type == "selector_expression":
                    field = func_node.child_by_field_name("field")
                    if field:
                        callee_name = _read_text(field, source)
            if callee_name:
                tgt_nid = label_to_nid.get(callee_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })
                elif callee_name:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })
        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from")):
            clean_edges.append(edge)

    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}


# ── Rust extractor (custom walk) ──────────────────────────────────────────────

def extract_rust(path: Path) -> dict:
    """从 Rust 文件中提取函数、结构体、枚举、trait、impl 方法和 use 声明。
    
    使用自定义 AST 遍历处理 Rust 特有的 impl 块和关联方法。
    
    Args:
        path: Rust 文件路径（.rs）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_rust as tsrust
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-rust not installed"}

    try:
        language = Language(tsrust.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, object]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({
                "id": nid,
                "label": label,
                "file_type": "code",
                "source_file": str_path,
                "source_location": f"L{line}",
            })

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        })

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def walk(node, parent_impl_nid: str | None = None) -> None:
        """递归遍历 Rust AST。"""
        t = node.type

        if t == "function_item":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if parent_impl_nid:
                    func_nid = _make_id(parent_impl_nid, func_name)
                    add_node(func_nid, f".{func_name}()", line)
                    add_edge(parent_impl_nid, func_nid, "method", line)
                else:
                    func_nid = _make_id(stem, func_name)
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(file_nid, func_nid, "contains", line)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t in ("struct_item", "enum_item", "trait_item"):
            name_node = node.child_by_field_name("name")
            if name_node:
                item_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                item_nid = _make_id(stem, item_name)
                add_node(item_nid, item_name, line)
                add_edge(file_nid, item_nid, "contains", line)
            return

        if t == "impl_item":
            type_node = node.child_by_field_name("type")
            impl_nid: str | None = None
            if type_node:
                type_name = _read_text(type_node, source).strip()
                impl_nid = _make_id(stem, type_name)
                add_node(impl_nid, type_name, node.start_point[0] + 1)
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    walk(child, parent_impl_nid=impl_nid)
            return

        if t == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg:
                raw = _read_text(arg, source)
                clean = raw.split("{")[0].rstrip(":").rstrip("*").rstrip(":")
                module_name = clean.split("::")[-1].strip()
                if module_name:
                    tgt_nid = _make_id(module_name)
                    add_edge(file_nid, tgt_nid, "imports_from", node.start_point[0] + 1)
            return

        for child in node.children:
            walk(child, parent_impl_nid=None)

    walk(root)

    label_to_nid: dict[str, str] = {}
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised.lower()] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def walk_calls(node, caller_nid: str) -> None:
        """递归遍历函数调用，构建调用关系图。"""
        if node.type == "function_item":
            return
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            callee_name: str | None = None
            if func_node:
                if func_node.type == "identifier":
                    callee_name = _read_text(func_node, source)
                elif func_node.type == "field_expression":
                    field = func_node.child_by_field_name("field")
                    if field:
                        callee_name = _read_text(field, source)
                elif func_node.type == "scoped_identifier":
                    name = func_node.child_by_field_name("name")
                    if name:
                        callee_name = _read_text(name, source)
            if callee_name:
                tgt_nid = label_to_nid.get(callee_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "confidence": "EXTRACTED",
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "weight": 1.0,
                        })
                else:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })
        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (tgt in valid_ids or edge["relation"] in ("imports", "imports_from")):
            clean_edges.append(edge)

    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}


# ── Zig ───────────────────────────────────────────────────────────────────────

def extract_zig(path: Path) -> dict:
    """从 Zig 文件中提取函数、结构体、枚举、联合体和导入语句。
    
    Args:
        path: Zig 文件路径（.zig）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_zig as tszig
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_zig not installed"}

    try:
        language = Language(tszig.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, Any]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": confidence, "source_file": str_path,
                      "source_location": f"L{line}", "weight": weight})

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def _extract_import(node) -> None:
        """从 Zig 节点中提取 @import 语句。"""
        for child in node.children:
            if child.type == "builtin_function":
                bi = None
                args = None
                for c in child.children:
                    if c.type == "builtin_identifier":
                        bi = _read_text(c, source)
                    elif c.type == "arguments":
                        args = c
                if bi in ("@import", "@cImport") and args:
                    for arg in args.children:
                        if arg.type in ("string_literal", "string"):
                            raw = _read_text(arg, source).strip('"')
                            module_name = raw.split("/")[-1].split(".")[0]
                            if module_name:
                                tgt_nid = _make_id(module_name)
                                add_edge(file_nid, tgt_nid, "imports_from",
                                         node.start_point[0] + 1)
                            return
            elif child.type == "field_expression":
                _extract_import(child)
                return

    def walk(node, parent_struct_nid: str | None = None) -> None:
        """递归遍历 Zig AST。"""
        t = node.type

        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if parent_struct_nid:
                    func_nid = _make_id(parent_struct_nid, func_name)
                    add_node(func_nid, f".{func_name}()", line)
                    add_edge(parent_struct_nid, func_nid, "method", line)
                else:
                    func_nid = _make_id(stem, func_name)
                    add_node(func_nid, f"{func_name}()", line)
                    add_edge(file_nid, func_nid, "contains", line)
                body = node.child_by_field_name("body")
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t == "variable_declaration":
            name_node = None
            value_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                elif child.type in ("struct_declaration", "enum_declaration",
                                    "union_declaration", "builtin_function",
                                    "field_expression"):
                    value_node = child

            if value_node and value_node.type == "struct_declaration":
                if name_node:
                    struct_name = _read_text(name_node, source)
                    line = node.start_point[0] + 1
                    struct_nid = _make_id(stem, struct_name)
                    add_node(struct_nid, struct_name, line)
                    add_edge(file_nid, struct_nid, "contains", line)
                    for child in value_node.children:
                        walk(child, parent_struct_nid=struct_nid)
                return

            if value_node and value_node.type in ("enum_declaration", "union_declaration"):
                if name_node:
                    type_name = _read_text(name_node, source)
                    line = node.start_point[0] + 1
                    type_nid = _make_id(stem, type_name)
                    add_node(type_nid, type_name, line)
                    add_edge(file_nid, type_nid, "contains", line)
                return

            if value_node and value_node.type in ("builtin_function", "field_expression"):
                _extract_import(node)
            return

        for child in node.children:
            walk(child, parent_struct_nid)

    walk(root)

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def walk_calls(node, caller_nid: str) -> None:
        """递归遍历函数调用，构建调用关系图。"""
        if node.type == "function_declaration":
            return
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn:
                callee = _read_text(fn, source).split(".")[-1]
                tgt_nid = next((n["id"] for n in nodes if n["label"] in
                                (f"{callee}()", f".{callee}()")), None)
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        add_edge(caller_nid, tgt_nid, "calls",
                                 node.start_point[0] + 1,
                                 confidence="EXTRACTED", weight=1.0)
                elif callee:
                    raw_calls.append({
                        "caller_nid": caller_nid,
                        "callee": callee,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                    })
        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    clean_edges = [e for e in edges if e["source"] in seen_ids and
                   (e["target"] in seen_ids or e["relation"] == "imports_from")]
    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}


# ── PowerShell ────────────────────────────────────────────────────────────────

def extract_powershell(path: Path) -> dict:
    """从 PowerShell 文件中提取函数、类、方法和 using 语句。
    
    Args:
        path: PowerShell 文件路径（.ps1）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_powershell as tsps
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_powershell not installed"}

    try:
        language = Language(tsps.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, Any]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": confidence, "source_file": str_path,
                      "source_location": f"L{line}", "weight": weight})

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    _PS_SKIP = frozenset({
        "using", "return", "if", "else", "elseif", "foreach", "for",
        "while", "do", "switch", "try", "catch", "finally", "throw",
        "break", "continue", "exit", "param", "begin", "process", "end",
    })

    def _find_script_block_body(node):
        """查找脚本块的函数体。"""
        for child in node.children:
            if child.type == "script_block":
                for sc in child.children:
                    if sc.type == "script_block_body":
                        return sc
                return child
        return None

    def walk(node, parent_class_nid: str | None = None) -> None:
        """递归遍历 PowerShell AST。"""
        t = node.type

        if t == "function_statement":
            name_node = next((c for c in node.children if c.type == "function_name"), None)
            if name_node:
                func_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)
                body = _find_script_block_body(node)
                if body:
                    function_bodies.append((func_nid, body))
            return

        if t == "class_statement":
            name_node = next((c for c in node.children if c.type == "simple_name"), None)
            if name_node:
                class_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                class_nid = _make_id(stem, class_name)
                add_node(class_nid, class_name, line)
                add_edge(file_nid, class_nid, "contains", line)
                for child in node.children:
                    walk(child, parent_class_nid=class_nid)
            return

        if t == "class_method_definition":
            name_node = next((c for c in node.children if c.type == "simple_name"), None)
            if name_node:
                method_name = _read_text(name_node, source)
                line = node.start_point[0] + 1
                if parent_class_nid:
                    method_nid = _make_id(parent_class_nid, method_name)
                    add_node(method_nid, f".{method_name}()", line)
                    add_edge(parent_class_nid, method_nid, "method", line)
                else:
                    method_nid = _make_id(stem, method_name)
                    add_node(method_nid, f"{method_name}()", line)
                    add_edge(file_nid, method_nid, "contains", line)
                body = _find_script_block_body(node)
                if body:
                    function_bodies.append((method_nid, body))
            return

        if t == "command":
            cmd_name_node = next((c for c in node.children if c.type == "command_name"), None)
            if cmd_name_node:
                cmd_text = _read_text(cmd_name_node, source).lower()
                if cmd_text == "using":
                    tokens = []
                    for child in node.children:
                        if child.type == "command_elements":
                            for el in child.children:
                                if el.type == "generic_token":
                                    tokens.append(_read_text(el, source))
                    module_tokens = [t for t in tokens
                                     if t.lower() not in ("namespace", "module", "assembly")]
                    if module_tokens:
                        module_name = module_tokens[-1].split(".")[-1]
                        add_edge(file_nid, _make_id(module_name), "imports_from",
                                 node.start_point[0] + 1)
            return

        for child in node.children:
            walk(child, parent_class_nid)

    walk(root)

    label_to_nid = {n["label"].strip("()").lstrip(".").lower(): n["id"] for n in nodes}
    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def walk_calls(node, caller_nid: str) -> None:
        """递归遍历函数调用，构建调用关系图。"""
        if node.type in ("function_statement", "class_statement"):
            return
        if node.type == "command":
            cmd_name_node = next((c for c in node.children if c.type == "command_name"), None)
            if cmd_name_node:
                cmd_text = _read_text(cmd_name_node, source)
                if cmd_text.lower() not in _PS_SKIP:
                    tgt_nid = label_to_nid.get(cmd_text.lower())
                    if tgt_nid and tgt_nid != caller_nid:
                        pair = (caller_nid, tgt_nid)
                        if pair not in seen_call_pairs:
                            seen_call_pairs.add(pair)
                            add_edge(caller_nid, tgt_nid, "calls",
                                     node.start_point[0] + 1,
                                     confidence="EXTRACTED", weight=1.0)
                    elif cmd_text:
                        raw_calls.append({
                            "caller_nid": caller_nid,
                            "callee": cmd_text,
                            "source_file": str_path,
                            "source_location": f"L{node.start_point[0] + 1}",
                        })
        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    clean_edges = [e for e in edges if e["source"] in seen_ids and
                   (e["target"] in seen_ids or e["relation"] == "imports_from")]
    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}


# ── Cross-file import resolution ──────────────────────────────────────────────

def _resolve_cross_file_imports(
    per_file: list[dict],
    paths: list[Path],
) -> list[dict]:
    """解析跨文件的导入关系，将文件级导入转换为类级边。
    
    两遍处理：
    1. 建立全局映射：类名/函数名 → 节点 ID（按文件名分段）
    2. 对于每个 `from .module import Name`，在全局映射中查找 Name，
       并添加从导入文件中的每个类到导入实体的 INFERRED 边。
    
    这将会把：
        auth.py --imports_from--> models.py          （被过滤掉的明显关系）
    转换为：
        DigestAuth --uses--> Response  [INFERRED]    （跨文件的关联！）
        BasicAuth  --uses--> Request   [INFERRED]
    
    Args:
        per_file: 每个文件的提取结果列表
        paths: 对应的文件路径列表
        
    Returns:
        list[dict]: 新添加的边列表
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        return []

    language = Language(tspython.language())
    parser = Parser(language)

    # Pass 1: name → node_id across all files
    # Map: stem → {ClassName: node_id}
    stem_to_entities: dict[str, dict[str, str]] = {}
    for file_result in per_file:
        for node in file_result.get("nodes", []):
            src = node.get("source_file", "")
            if not src:
                continue
            stem = Path(src).stem
            label = node.get("label", "")
            nid = node.get("id", "")
            # Only index real classes/functions (not file nodes, not method stubs)
            if label and not label.endswith((")", ".py")) and "_" not in label[:1]:
                stem_to_entities.setdefault(stem, {})[label] = nid

    # Pass 2: for each file, find `from .X import A, B, C` and resolve
    new_edges: list[dict] = []
    stem_to_path: dict[str, Path] = {p.stem: p for p in paths}

    for file_result, path in zip(per_file, paths):
        stem = path.stem
        str_path = str(path)

        # Find all classes defined in this file (the importers)
        local_classes = [
            n["id"] for n in file_result.get("nodes", [])
            if n.get("source_file") == str_path
            and not n["label"].endswith((")", ".py"))
            and n["id"] != _make_id(stem)  # exclude file-level node
        ]
        if not local_classes:
            continue

        # Parse imports from this file
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue

        def walk_imports(node) -> None:
            """递归遍历 import 节点。"""
            if node.type == "import_from_statement":
                # Find the module name - handles both absolute and relative imports.
                # Relative: `from .models import X` → relative_import → dotted_name
                # Absolute: `from models import X`  → module_name field
                target_stem: str | None = None
                for child in node.children:
                    if child.type == "relative_import":
                        # Dig into relative_import → dotted_name → identifier
                        for sub in child.children:
                            if sub.type == "dotted_name":
                                raw = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="replace")
                                target_stem = raw.split(".")[-1]
                                break
                        break
                    if child.type == "dotted_name" and target_stem is None:
                        raw = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        target_stem = raw.split(".")[-1]

                if not target_stem or target_stem not in stem_to_entities:
                    return

                # Collect imported names: dotted_name children of import_from_statement
                # that come AFTER the 'import' keyword token.
                imported_names: list[str] = []
                past_import_kw = False
                for child in node.children:
                    if child.type == "import":
                        past_import_kw = True
                        continue
                    if not past_import_kw:
                        continue
                    if child.type == "dotted_name":
                        imported_names.append(
                            source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        )
                    elif child.type == "aliased_import":
                        # `import X as Y` - take the original name
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            imported_names.append(
                                source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                            )

                line = node.start_point[0] + 1
                for name in imported_names:
                    tgt_nid = stem_to_entities[target_stem].get(name)
                    if tgt_nid:
                        for src_class_nid in local_classes:
                            new_edges.append({
                                "source": src_class_nid,
                                "target": tgt_nid,
                                "relation": "uses",
                                "confidence": "INFERRED",
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 0.8,
                            })
            for child in node.children:
                walk_imports(child)

        walk_imports(tree.root_node)

    return new_edges


def extract_objc(path: Path) -> dict:
    """从 Objective-C 文件中提取接口、实现、协议、方法和导入语句。
    
    支持 .m、.mm 和 .h 文件。
    
    Args:
        path: Objective-C 文件路径（.m/.mm/.h）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_objc as tsobjc
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_objc not installed"}

    try:
        language = Language(tsobjc.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    method_bodies: list[tuple[str, Any]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": confidence, "source_file": str_path,
                      "source_location": f"L{line}", "weight": weight})

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def _read(node) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _get_name(node, field: str) -> str | None:
        n = node.child_by_field_name(field)
        return _read(n) if n else None

    def walk(node, parent_nid: str | None = None) -> None:
        """递归遍历 Objective-C AST。"""
        t = node.type
        line = node.start_point[0] + 1

        if t == "preproc_include":
            # #import <Foundation/Foundation.h> or #import "MyClass.h"
            for child in node.children:
                if child.type == "system_lib_string":
                    raw = _read(child).strip("<>")
                    module = raw.split("/")[-1].replace(".h", "")
                    if module:
                        tgt_nid = _make_id(module)
                        add_edge(file_nid, tgt_nid, "imports", line)
                elif child.type == "string_literal":
                    # recurse into string_literal to find string_content
                    for sub in child.children:
                        if sub.type == "string_content":
                            raw = _read(sub)
                            module = raw.split("/")[-1].replace(".h", "")
                            if module:
                                tgt_nid = _make_id(module)
                                add_edge(file_nid, tgt_nid, "imports", line)
            return

        if t == "class_interface":
            # @interface ClassName : SuperClass <Protocols>
            # children: @interface, identifier(name), ':', identifier(super), parameterized_arguments, ...
            identifiers = [c for c in node.children if c.type == "identifier"]
            if not identifiers:
                for child in node.children:
                    walk(child, parent_nid)
                return
            name = _read(identifiers[0])
            cls_nid = _make_id(stem, name)
            add_node(cls_nid, name, line)
            add_edge(file_nid, cls_nid, "contains", line)
            # superclass is second identifier after ':'
            colon_seen = False
            for child in node.children:
                if child.type == ":":
                    colon_seen = True
                elif colon_seen and child.type == "identifier":
                    super_nid = _make_id(_read(child))
                    add_edge(cls_nid, super_nid, "inherits", line)
                    colon_seen = False
                elif child.type == "parameterized_arguments":
                    # protocols adopted
                    for sub in child.children:
                        if sub.type == "type_name":
                            for s in sub.children:
                                if s.type == "type_identifier":
                                    proto_nid = _make_id(_read(s))
                                    add_edge(cls_nid, proto_nid, "imports", line)
                elif child.type == "method_declaration":
                    walk(child, cls_nid)
            return

        if t == "class_implementation":
            # @implementation ClassName
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = _read(child)
                    break
            if not name:
                for child in node.children:
                    walk(child, parent_nid)
                return
            impl_nid = _make_id(stem, name)
            if impl_nid not in seen_ids:
                add_node(impl_nid, name, line)
                add_edge(file_nid, impl_nid, "contains", line)
            for child in node.children:
                if child.type == "implementation_definition":
                    for sub in child.children:
                        walk(sub, impl_nid)
            return

        if t == "protocol_declaration":
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = _read(child)
                    break
            if name:
                proto_nid = _make_id(stem, name)
                add_node(proto_nid, f"<{name}>", line)
                add_edge(file_nid, proto_nid, "contains", line)
                for child in node.children:
                    walk(child, proto_nid)
            return

        if t in ("method_declaration", "method_definition"):
            container = parent_nid or file_nid
            # method name is the first identifier child (simple selector)
            # for compound selectors: identifier + method_parameter pairs
            parts = []
            for child in node.children:
                if child.type == "identifier":
                    parts.append(_read(child))
                elif child.type == "method_parameter":
                    for sub in child.children:
                        if sub.type == "identifier":
                            # selector keyword before ':'
                            pass
            method_name = "".join(parts) if parts else None
            if method_name:
                method_nid = _make_id(container, method_name)
                add_node(method_nid, f"-{method_name}", line)
                add_edge(container, method_nid, "method", line)
                if t == "method_definition":
                    method_bodies.append((method_nid, node))
            return

        for child in node.children:
            walk(child, parent_nid)

    walk(root)

    # Second pass: resolve calls inside method bodies
    all_method_nids = {n["id"] for n in nodes if n["id"] != file_nid}
    seen_calls: set[tuple[str, str]] = set()
    for caller_nid, body_node in method_bodies:
        def walk_calls(n) -> None:
            """递归遍历方法体中的消息表达式。"""
            if n.type == "message_expression":
                # [receiver selector]
                for child in n.children:
                    if child.type in ("selector", "keyword_argument_list"):
                        sel = []
                        if child.type == "selector":
                            sel.append(_read(child))
                        else:
                            for sub in child.children:
                                if sub.type == "keyword_argument":
                                    for s in sub.children:
                                        if s.type == "selector":
                                            sel.append(_read(s))
                        method_name = "".join(sel)
                        for candidate in all_method_nids:
                            if candidate.endswith(_make_id("", method_name).lstrip("_")):
                                pair = (caller_nid, candidate)
                                if pair not in seen_calls and caller_nid != candidate:
                                    seen_calls.add(pair)
                                    add_edge(caller_nid, candidate, "calls", body_node.start_point[0] + 1,
                                             confidence="EXTRACTED", weight=1.0)
            for child in n.children:
                walk_calls(child)
        walk_calls(body_node)

    return {"nodes": nodes, "edges": edges, "input_tokens": 0, "output_tokens": 0}


def extract_elixir(path: Path) -> dict:
    """从 Elixir 文件中提取模块、函数、导入和调用。
    
    支持 .ex 和 .exs 文件。
    
    Args:
        path: Elixir 文件路径（.ex/.exs）
        
    Returns:
        dict: 包含 nodes、edges 的字典
    """
    try:
        import tree_sitter_elixir as tselixir
        from tree_sitter import Language, Parser
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree_sitter_elixir not installed"}

    try:
        language = Language(tselixir.language())
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = path.stem
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, Any]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 confidence: str = "EXTRACTED", weight: float = 1.0) -> None:
        edges.append({"source": src, "target": tgt, "relation": relation,
                      "confidence": confidence, "source_file": str_path,
                      "source_location": f"L{line}", "weight": weight})

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    _IMPORT_KEYWORDS = frozenset({"alias", "import", "require", "use"})

    def _get_alias_text(node) -> str | None:
        """从节点中提取别名文本。"""
        for child in node.children:
            if child.type == "alias":
                return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
        return None

    def walk(node, parent_module_nid: str | None = None) -> None:
        """递归遍历 Elixir AST。"""
        if node.type != "call":
            for child in node.children:
                walk(child, parent_module_nid)
            return

        identifier_node = None
        arguments_node = None
        do_block_node = None
        for child in node.children:
            if child.type == "identifier":
                identifier_node = child
            elif child.type == "arguments":
                arguments_node = child
            elif child.type == "do_block":
                do_block_node = child

        if identifier_node is None:
            for child in node.children:
                walk(child, parent_module_nid)
            return

        keyword = source[identifier_node.start_byte:identifier_node.end_byte].decode("utf-8", errors="replace")
        line = node.start_point[0] + 1

        if keyword == "defmodule":
            module_name = _get_alias_text(arguments_node) if arguments_node else None
            if not module_name:
                return
            module_nid = _make_id(stem, module_name)
            add_node(module_nid, module_name, line)
            add_edge(file_nid, module_nid, "contains", line)
            if do_block_node:
                for child in do_block_node.children:
                    walk(child, parent_module_nid=module_nid)
            return

        if keyword in ("def", "defp"):
            func_name = None
            if arguments_node:
                for child in arguments_node.children:
                    if child.type == "call":
                        for sub in child.children:
                            if sub.type == "identifier":
                                func_name = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="replace")
                                break
                    elif child.type == "identifier":
                        func_name = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        break
            if not func_name:
                return
            container = parent_module_nid or file_nid
            func_nid = _make_id(container, func_name)
            add_node(func_nid, f"{func_name}()", line)
            if parent_module_nid:
                add_edge(parent_module_nid, func_nid, "method", line)
            else:
                add_edge(file_nid, func_nid, "contains", line)
            if do_block_node:
                function_bodies.append((func_nid, do_block_node))
            return

        if keyword in _IMPORT_KEYWORDS and arguments_node:
            module_name = _get_alias_text(arguments_node)
            if module_name:
                tgt_nid = _make_id(module_name)
                add_edge(file_nid, tgt_nid, "imports", line)
            return

        for child in node.children:
            walk(child, parent_module_nid)

    walk(root)

    label_to_nid: dict[str, str] = {}
    for n in nodes:
        normalised = n["label"].strip("()").lstrip(".")
        label_to_nid[normalised.lower()] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []
    _SKIP_KEYWORDS = frozenset({
        "def", "defp", "defmodule", "defmacro", "defmacrop",
        "defstruct", "defprotocol", "defimpl", "defguard",
        "alias", "import", "require", "use",
        "if", "unless", "case", "cond", "with", "for",
    })

    def walk_calls(node, caller_nid: str) -> None:
        """递归遍历函数调用，构建调用关系图。"""
        if node.type != "call":
            for child in node.children:
                walk_calls(child, caller_nid)
            return
        for child in node.children:
            if child.type == "identifier":
                kw = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                if kw in _SKIP_KEYWORDS:
                    for c in node.children:
                        walk_calls(c, caller_nid)
                    return
                break
        callee_name: str | None = None
        for child in node.children:
            if child.type == "dot":
                dot_text = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                parts = dot_text.rstrip(".").split(".")
                if parts:
                    callee_name = parts[-1]
                break
            if child.type == "identifier":
                callee_name = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                break
        if callee_name:
            tgt_nid = label_to_nid.get(callee_name.lower())
            if tgt_nid and tgt_nid != caller_nid:
                pair = (caller_nid, tgt_nid)
                if pair not in seen_call_pairs:
                    seen_call_pairs.add(pair)
                    add_edge(caller_nid, tgt_nid, "calls",
                             node.start_point[0] + 1, confidence="EXTRACTED", weight=1.0)
            else:
                raw_calls.append({
                    "caller_nid": caller_nid,
                    "callee": callee_name,
                    "source_file": str_path,
                    "source_location": f"L{node.start_point[0] + 1}",
                })
        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body in function_bodies:
        walk_calls(body, caller_nid)

    clean_edges = [e for e in edges if e["source"] in seen_ids and
                   (e["target"] in seen_ids or e["relation"] == "imports")]
    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls, "input_tokens": 0, "output_tokens": 0}


# ── Main extract and collect_files ────────────────────────────────────────────


def _check_tree_sitter_version() -> None:
    """检查 tree-sitter 版本是否满足要求。
    
    新版本的 Language API（v2）需要 tree-sitter >= 0.23.0。
    如果版本过低，抛出明确的错误信息。
    
    Raises:
        ImportError: tree-sitter 未安装
        RuntimeError: tree-sitter 版本过低
    """
    try:
        from tree_sitter import LANGUAGE_VERSION
    except ImportError:
        raise ImportError(
            "tree-sitter is not installed. Run: pip install 'tree-sitter>=0.23.0'"
        )
    # Language API v2 starts at LANGUAGE_VERSION 14
    if LANGUAGE_VERSION < 14:
        import tree_sitter as _ts
        raise RuntimeError(
            f"tree-sitter {getattr(_ts, '__version__', 'unknown')} is too old. "
            f"graphify requires tree-sitter >= 0.23.0 (Language API v2). "
            f"Run: pip install --upgrade tree-sitter"
        )


def extract(paths: list[Path], cache_root: Path | None = None) -> dict:
    """从代码文件列表中提取 AST 节点和边。
    
    两遍处理：
    1. 每个文件的结构化提取（类、函数、导入语句）
    2. 跨文件导入解析：将文件级导入转换为类级的 INFERRED 边
    
    Args:
        paths: 要提取的源代码文件列表
        cache_root: 缓存的根目录，覆盖自动推断的公共路径前缀
                    （在子目录上运行时使用 Path('.')，使缓存保持在 ./graphify-out/cache/）
                    
    Returns:
        dict: 包含所有 nodes、edges 的字典
    """
    _check_tree_sitter_version()
    per_file: list[dict] = []

    # Infer a common root for cache keys
    try:
        if not paths:
            root = Path(".")
        elif len(paths) == 1:
            root = paths[0].parent
        else:
            common_len = sum(
                1 for i in range(min(len(p.parts) for p in paths))
                if len({p.parts[i] for p in paths}) == 1
            )
            root = Path(*paths[0].parts[:common_len]) if common_len else Path(".")
    except Exception:
        root = Path(".")

    _DISPATCH: dict[str, Any] = {
        ".py": extract_python,
        ".js": extract_js,
        ".jsx": extract_js,
        ".mjs": extract_js,
        ".ts": extract_js,
        ".tsx": extract_js,
        ".go": extract_go,
        ".rs": extract_rust,
        ".java": extract_java,
        ".c": extract_c,
        ".h": extract_c,
        ".cpp": extract_cpp,
        ".cc": extract_cpp,
        ".cxx": extract_cpp,
        ".hpp": extract_cpp,
        ".rb": extract_ruby,
        ".cs": extract_csharp,
        ".kt": extract_kotlin,
        ".kts": extract_kotlin,
        ".scala": extract_scala,
        ".php": extract_php,
        ".swift": extract_swift,
        ".lua": extract_lua,
        ".toc": extract_lua,
        ".zig": extract_zig,
        ".ps1": extract_powershell,
        ".ex": extract_elixir,
        ".exs": extract_elixir,
        ".m": extract_objc,
        ".mm": extract_objc,
        ".jl": extract_julia,
        ".vue": extract_js,
        ".svelte": extract_js,
        ".dart": extract_dart,
        ".v": extract_verilog,
        ".sv": extract_verilog,
    }

    total = len(paths)
    _PROGRESS_INTERVAL = 100
    for i, path in enumerate(paths):
        if total >= _PROGRESS_INTERVAL and i % _PROGRESS_INTERVAL == 0 and i > 0:
            print(f"  AST extraction: {i}/{total} files ({i * 100 // total}%)", flush=True)
        # .blade.php must be checked before suffix lookup since Path.suffix returns .php
        if path.name.endswith(".blade.php"):
            extractor = extract_blade
        else:
            extractor = _DISPATCH.get(path.suffix)
        if extractor is None:
            continue
        cached = load_cached(path, cache_root or root)
        if cached is not None:
            per_file.append(cached)
            continue
        result = extractor(path)
        if "error" not in result:
            save_cached(path, result, cache_root or root)
        per_file.append(result)
    if total >= _PROGRESS_INTERVAL:
        print(f"  AST extraction: {total}/{total} files (100%)", flush=True)

    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    for result in per_file:
        all_nodes.extend(result.get("nodes", []))
        all_edges.extend(result.get("edges", []))

    # Add cross-file class-level edges (Python only - uses Python parser internally)
    py_paths = [p for p in paths if p.suffix == ".py"]
    if py_paths:
        py_results = [r for r, p in zip(per_file, paths) if p.suffix == ".py"]
        try:
            cross_file_edges = _resolve_cross_file_imports(py_results, py_paths)
            all_edges.extend(cross_file_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Cross-file import resolution failed, skipping: %s", exc)

    # Cross-file call resolution for all languages
    # Each extractor saved unresolved calls in raw_calls. Now that we have all
    # nodes from all files, resolve any callee that exists in another file.
    global_label_to_nid: dict[str, str] = {}
    for n in all_nodes:
        raw = n.get("label", "")
        normalised = raw.strip("()").lstrip(".")
        if normalised:
            global_label_to_nid[normalised.lower()] = n["id"]

    existing_pairs = {(e["source"], e["target"]) for e in all_edges}
    for result in per_file:
        for rc in result.get("raw_calls", []):
            callee = rc.get("callee", "")
            if not callee:
                continue
            tgt = global_label_to_nid.get(callee.lower())
            caller = rc["caller_nid"]
            if tgt and tgt != caller and (caller, tgt) not in existing_pairs:
                existing_pairs.add((caller, tgt))
                all_edges.append({
                    "source": caller,
                    "target": tgt,
                    "relation": "calls",
                    "confidence": "INFERRED",
                    "confidence_score": 0.8,
                    "source_file": rc.get("source_file", ""),
                    "source_location": rc.get("source_location"),
                    "weight": 1.0,
                })

    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def collect_files(target: Path, *, follow_symlinks: bool = False, root: Path | None = None) -> list[Path]:
    """递归收集目录下所有支持的源代码文件。
    
    支持 .gitignore 风格的忽略规则，可以通过 .graphifyignore 文件配置。
    
    Args:
        target: 要扫描的目录或文件路径
        follow_symlinks: 是否跟踪符号链接（默认 False）
        root: 用于 .graphifyignore 解析的根目录
        
    Returns:
        list[Path]: 收集到的源代码文件路径列表
    """
    if target.is_file():
        return [target]
    _EXTENSIONS = {
        ".py", ".js", ".ts", ".tsx", ".go", ".rs",
        ".java", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
        ".rb", ".cs", ".kt", ".kts", ".scala", ".php", ".swift",
        ".lua", ".toc", ".zig", ".ps1",
        ".m", ".mm",
    }
    from graphify.detect import _load_graphifyignore, _is_ignored
    ignore_root = root if root is not None else target
    patterns = _load_graphifyignore(ignore_root)

    def _ignored(p: Path) -> bool:
        return bool(patterns and _is_ignored(p, ignore_root, patterns))

    if not follow_symlinks:
        results: list[Path] = []
        for ext in sorted(_EXTENSIONS):
            results.extend(
                p for p in target.rglob(f"*{ext}")
                if not any(part.startswith(".") for part in p.parts)
                and not _ignored(p)
            )
        return sorted(results)
    # Walk with symlink following + cycle detection
    results = []
    for dirpath, dirnames, filenames in os.walk(target, followlinks=True):
        if os.path.islink(dirpath):
            real = os.path.realpath(dirpath)
            parent_real = os.path.realpath(os.path.dirname(dirpath))
            if parent_real == real or parent_real.startswith(real + os.sep):
                dirnames.clear()
                continue
        dp = Path(dirpath)
        if any(part.startswith(".") for part in dp.parts):
            dirnames.clear()
            continue
        for fname in filenames:
            p = dp / fname
            if p.suffix in _EXTENSIONS and not fname.startswith(".") and not _ignored(p):
                results.append(p)
    return sorted(results)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m graphify.extract <file_or_dir> ...", file=sys.stderr)
        sys.exit(1)

    paths: list[Path] = []
    for arg in sys.argv[1:]:
        paths.extend(collect_files(Path(arg)))

    result = extract(paths)
    print(json.dumps(result, indent=2))