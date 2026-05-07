# per-file extraction cache - skip unchanged files on re-run
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _body_content(content: bytes) -> bytes:
    """
    从 Markdown 内容中剥离 YAML frontmatter，仅返回正文部分。

    如果内容以 '---' 开头且存在结束标记，则返回结束标记之后的内容；
    否则返回原始内容。此函数用于确保仅正文内容的变化会触发缓存失效，
    而元数据（如 reviewed, status, tags）的修改不影响缓存。

    Args:
        content: 原始文件内容的字节串。

    Returns:
        剥离 frontmatter 后的正文内容字节串；若无 frontmatter 则返回原始内容。
    """
    text = content.decode(errors="replace")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].encode()
    return content


def file_hash(path: Path, root: Path = Path(".")) -> str:
    """
    计算文件的 SHA256 哈希值，用于缓存键生成。

    哈希值基于文件内容（对于 Markdown 文件，仅正文部分）以及相对于根目录的路径。
    使用相对路径而非绝对路径，使得缓存条目在不同机器和检出目录间可移植，
    支持共享缓存和 CI/CD 环境。如果文件在根目录外部，则回退到解析后的绝对路径。

    Args:
        path: 要计算哈希的文件路径。
        root: 项目根目录路径，用于计算相对路径，默认为当前目录。

    Returns:
        文件的 SHA256 哈希值的十六进制字符串。

    Raises:
        OSError: 当文件读取失败时，由调用方处理。
    """
    p = Path(path)
    raw = p.read_bytes()
    content = _body_content(raw) if p.suffix.lower() == ".md" else raw
    h = hashlib.sha256()
    h.update(content)
    h.update(b"\x00")
    try:
        rel = p.resolve().relative_to(Path(root).resolve())
        h.update(str(rel).encode())
    except ValueError:
        h.update(str(p.resolve()).encode())
    return h.hexdigest()


def cache_dir(root: Path = Path(".")) -> Path:
    """
    获取缓存目录路径，如果不存在则创建。

    缓存目录位于项目根目录下的 graphify-out/cache 中。

    Args:
        root: 项目根目录路径，默认为当前目录。

    Returns:
        缓存目录的 Path 对象。
    """
    d = Path(root).resolve() / "graphify-out" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_cached(path: Path, root: Path = Path(".")) -> dict | None:
    """
    从缓存中加载文件的提取结果。

    缓存键为文件内容的 SHA256 哈希值，缓存文件存储在 {hash}.json 中。
    如果缓存不存在或文件内容已发生变化（哈希不匹配），则返回 None。

    Args:
        path: 要加载缓存的文件路径。
        root: 项目根目录路径，默认为当前目录。

    Returns:
        如果缓存命中则返回包含 'nodes' 和 'edges' 列表的字典，否则返回 None。
    """
    try:
        h = file_hash(path, root)
    except OSError:
        return None
    entry = cache_dir(root) / f"{h}.json"
    if not entry.exists():
        return None
    try:
        return json.loads(entry.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_cached(path: Path, result: dict, root: Path = Path(".")) -> None:
    """
    将文件的提取结果保存到缓存。

    缓存文件以文件当前内容的 SHA256 哈希值命名，存储为 {hash}.json。
    使用原子写入策略（先写临时文件再重命名）以避免并发读写的竞争条件。
    在 Windows 平台上，如果重命名失败则回退到复制后删除的方式。

    Args:
        path: 要缓存的文件路径。
        result: 提取结果字典，必须包含 'nodes' 和 'edges' 列表。
        root: 项目根目录路径，默认为当前目录。

    Raises:
        Exception: 当写入失败时会清理临时文件并重新抛出异常。
    """
    h = file_hash(path, root)
    entry = cache_dir(root) / f"{h}.json"
    tmp = entry.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(result), encoding="utf-8")
        try:
            os.replace(tmp, entry)
        except PermissionError:
            # Windows: os.replace 在目标文件被短暂锁定时会失败（错误码 5）
            # 回退到复制后删除的方式
            import shutil
            shutil.copy2(tmp, entry)
            tmp.unlink(missing_ok=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def cached_files(root: Path = Path(".")) -> set[str]:
    """
    获取所有存在有效缓存条目的文件路径集合。

    遍历缓存目录中的所有 JSON 文件，返回对应的文件名（即哈希值）集合。

    Args:
        root: 项目根目录路径，默认为当前目录。

    Returns:
        包含所有有效缓存条目哈希值的字符串集合。
    """
    d = cache_dir(root)
    return {p.stem for p in d.glob("*.json")}


def clear_cache(root: Path = Path(".")) -> None:
    """
    删除所有缓存文件。

    清空 graphify-out/cache 目录下的所有 JSON 缓存文件。

    Args:
        root: 项目根目录路径，默认为当前目录。
    """
    d = cache_dir(root)
    for f in d.glob("*.json"):
        f.unlink()


def check_semantic_cache(
    files: list[str],
    root: Path = Path("."),
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """
    检查多个文件的语义提取缓存状态。

    对给定的文件列表逐一检查缓存有效性，将命中的节点、边和超边合并到返回值中，
    同时返回未命中缓存的文件列表供后续处理。

    Args:
        files: 要检查的绝对路径文件列表。
        root: 项目根目录路径，默认为当前目录。

    Returns:
        一个包含四个元素的元组：
        - cached_nodes: 从缓存合并的所有节点列表
        - cached_edges: 从缓存合并的所有边列表
        - cached_hyperedges: 从缓存合并的所有超边列表
        - uncached_files: 未命中缓存需要重新提取的文件路径列表
    """
    cached_nodes: list[dict] = []
    cached_edges: list[dict] = []
    cached_hyperedges: list[dict] = []
    uncached: list[str] = []

    for fpath in files:
        result = load_cached(Path(fpath), root)
        if result is not None:
            cached_nodes.extend(result.get("nodes", []))
            cached_edges.extend(result.get("edges", []))
            cached_hyperedges.extend(result.get("hyperedges", []))
        else:
            uncached.append(fpath)

    return cached_nodes, cached_edges, cached_hyperedges, uncached


def save_semantic_cache(
    nodes: list[dict],
    edges: list[dict],
    hyperedges: list[dict] | None = None,
    root: Path = Path("."),
) -> int:
    """
    将语义提取结果按源文件分组保存到缓存。

    根据每个节点、边和超边中的 'source_file' 字段将它们分组，
    然后为每个源文件单独保存一个缓存条目。只有存在于磁盘上的文件才会被缓存。

    Args:
        nodes: 所有提取的节点列表，每个节点应包含 'source_file' 字段。
        edges: 所有提取的边列表，每条边应包含 'source_file' 字段。
        hyperedges: 所有提取的超边列表，每个超边应包含 'source_file' 字段，默认为 None。
        root: 项目根目录路径，用于解析相对路径，默认为当前目录。

    Returns:
        成功缓存的文件数量。
    """
    from collections import defaultdict

    by_file: dict[str, dict] = defaultdict(lambda: {"nodes": [], "edges": [], "hyperedges": []})
    for n in nodes:
        src = n.get("source_file", "")
        if src:
            by_file[src]["nodes"].append(n)
    for e in edges:
        src = e.get("source_file", "")
        if src:
            by_file[src]["edges"].append(e)
    for h in (hyperedges or []):
        src = h.get("source_file", "")
        if src:
            by_file[src]["hyperedges"].append(h)

    saved = 0
    for fpath, result in by_file.items():
        p = Path(fpath)
        if not p.is_absolute():
            p = Path(root) / p
        if p.exists():
            save_cached(p, result, root)
            saved += 1
    return saved