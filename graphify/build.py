# assemble node+edge dicts into a NetworkX graph, preserving edge direction
#
# Node deduplication — three layers:
#
# 1. Within a file (AST): each extractor tracks a `seen_ids` set. A node ID is
#    emitted at most once per file, so duplicate class/function definitions in
#    the same source file are collapsed to the first occurrence.
#
# 2. Between files (build): NetworkX G.add_node() is idempotent — calling it
#    twice with the same ID overwrites the attributes with the second call's
#    values. Nodes are added in extraction order (AST first, then semantic),
#    so if the same entity is extracted by both passes the semantic node
#    silently overwrites the AST node. This is intentional: semantic nodes
#    carry richer labels and cross-file context, while AST nodes have precise
#    source_location. If you need to change the priority, reorder extractions
#    passed to build().
#
# 3. Semantic merge (skill): before calling build(), the skill merges cached
#    and new semantic results using an explicit `seen` set keyed on node["id"],
#    so duplicates across cache hits and new extractions are resolved there
#    before any graph construction happens.
#
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
import networkx as nx
from .validate import validate_extraction


def _normalize_id(s: str) -> str:
    """Normalize an ID string the same way extract._make_id does.

    Used to reconcile edge endpoints when the LLM generates IDs with slightly
    different punctuation or casing than the AST extractor.

    Args:
        s: 原始ID字符串

    Returns:
        规范化后的ID字符串（小写，非字母数字字符替换为下划线）
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    return cleaned.strip("_").lower()


def build_from_json(extraction: dict, *, directed: bool = False) -> nx.Graph:
    """从提取字典构建NetworkX图。

    将JSON格式的节点和边数据转换为NetworkX图对象。支持有向图和无向图两种模式。
    自动处理遗留数据格式（如"links"字段）和字段名兼容性问题（如"source" -> "source_file"）。

    Args:
        extraction: 包含"nodes"、"edges"（或"links"）、可选"hyperedges"的字典
        directed: True返回有向图(DiGraph)，False返回无向图(Graph)，默认为False

    Returns:
        构建好的NetworkX图对象

    Note:
        有向图模式下边方向被保留（source->target）；
        无向图模式下边方向信息仍存储在边的"_src"和"_tgt"属性中以备显示用。
    """
    # NetworkX <= 3.1 serialised edges as "links"; remap to "edges" for compatibility.
    if "edges" not in extraction and "links" in extraction:
        extraction = dict(extraction, edges=extraction["links"])

    # Canonicalize legacy node/edge schema before validation.
    for node in extraction.get("nodes", []):
        if isinstance(node, dict) and "source" in node and "source_file" not in node:
            # Count edges that reference this node so the warning is actionable (#479)
            node_id = node.get("id", "?")
            affected_edges = sum(
                1 for e in extraction.get("edges", [])
                if e.get("source") == node_id or e.get("target") == node_id
            )
            print(
                f"[graphify] WARNING: node '{node_id}' uses field 'source' instead of "
                f"'source_file' — {affected_edges} edge(s) may be misrouted. "
                f"Rename the field to 'source_file' to silence this warning.",
                file=sys.stderr,
            )
            node["source_file"] = node.pop("source")

    errors = validate_extraction(extraction)
    # Dangling edges (stdlib/external imports) are expected - only warn about real schema errors.
    real_errors = [e for e in errors if "does not match any node id" not in e]
    if real_errors:
        print(f"[graphify] Extraction warning ({len(real_errors)} issues): {real_errors[0]}", file=sys.stderr)
    G: nx.Graph = nx.DiGraph() if directed else nx.Graph()
    for node in extraction.get("nodes", []):
        G.add_node(node["id"], **{k: v for k, v in node.items() if k != "id"})
    node_set = set(G.nodes())
    # Normalized ID map: lets edges survive when the LLM generates IDs with
    # slightly different casing or punctuation than the AST extractor.
    # e.g. "Session_ValidateToken" maps to "session_validatetoken".
    norm_to_id: dict[str, str] = {_normalize_id(nid): nid for nid in node_set}
    for edge in extraction.get("edges", []):
        if "source" not in edge and "from" in edge:
            edge["source"] = edge["from"]
        if "target" not in edge and "to" in edge:
            edge["target"] = edge["to"]
        if "source" not in edge or "target" not in edge:
            continue
        src, tgt = edge["source"], edge["target"]
        # Remap mismatched IDs via normalization before dropping the edge.
        if src not in node_set:
            src = norm_to_id.get(_normalize_id(src), src)
        if tgt not in node_set:
            tgt = norm_to_id.get(_normalize_id(tgt), tgt)
        if src not in node_set or tgt not in node_set:
            continue  # skip edges to external/stdlib nodes - expected, not an error
        attrs = {k: v for k, v in edge.items() if k not in ("source", "target")}
        # Preserve original edge direction - undirected graphs lose it otherwise,
        # causing display functions to show edges backwards.
        attrs["_src"] = src
        attrs["_tgt"] = tgt
        G.add_edge(src, tgt, **attrs)
    hyperedges = extraction.get("hyperedges", [])
    if hyperedges:
        G.graph["hyperedges"] = hyperedges
    return G


def build(extractions: list[dict], *, directed: bool = False) -> nx.Graph:
    """合并多个提取结果为一个图。

    按顺序合并多个提取字典中的节点、边和超边。对于相同ID的节点，后出现的节点属性会覆盖先出现的节点属性。
    建议先传入AST提取结果，再传入语义提取结果，以便语义标签覆盖AST节点。

    Args:
        extractions: 提取字典列表，每个字典应包含"nodes"、"edges"等字段
        directed: True返回有向图(DiGraph)，False返回无向图(Graph)，默认为False

    Returns:
        合并后的NetworkX图对象

    Note:
        NetworkX的add_node()是幂等的，重复添加相同ID的节点会用新属性覆盖旧属性。
    """
    combined: dict = {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0}
    for ext in extractions:
        combined["nodes"].extend(ext.get("nodes", []))
        combined["edges"].extend(ext.get("edges", []))
        combined["hyperedges"].extend(ext.get("hyperedges", []))
        combined["input_tokens"] += ext.get("input_tokens", 0)
        combined["output_tokens"] += ext.get("output_tokens", 0)
    return build_from_json(combined, directed=directed)


def _norm_label(label: str) -> str:
    """计算标准化后的标签去重键。

    将标签转换为小写并移除非字母数字字符，用于节点标签的模糊匹配去重。

    Args:
        label: 原始标签字符串

    Returns:
        标准化后的去重键字符串
    """
    return re.sub(r"[^a-z0-9 ]", "", label.lower()).strip()


def deduplicate_by_label(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """合并具有相同标准化标签的节点，并重写相关边的引用。

    当多个节点拥有相似的标签时，将它们合并为一个节点。合并策略：
    - 优先保留没有分块后缀(_c\\d+)的节点ID
    - 当后缀情况相同时，保留ID字符串较短的节点
    - 合并后自动丢弃产生的自环边

    Args:
        nodes: 节点字典列表，每个节点需包含"id"和可选的"label"字段
        edges: 边字典列表，每条边需包含"source"和"target"字段

    Returns:
        元组(deduped_nodes, deduped_edges)：
        - deduped_nodes: 去重后的节点列表
        - deduped_edges: 更新引用后的边列表（不含自环边）

    Note:
        该函数在build()中自动调用，无需手动调用。
    """
    _CHUNK_SUFFIX = re.compile(r"_c\d+$")
    canonical: dict[str, dict] = {}  # norm_label -> surviving node
    remap: dict[str, str] = {}       # old_id -> surviving_id

    for node in nodes:
        key = _norm_label(node.get("label", node.get("id", "")))
        if not key:
            continue
        existing = canonical.get(key)
        if existing is None:
            canonical[key] = node
        else:
            has_suffix = bool(_CHUNK_SUFFIX.search(node["id"]))
            existing_has_suffix = bool(_CHUNK_SUFFIX.search(existing["id"]))
            if has_suffix and not existing_has_suffix:
                remap[node["id"]] = existing["id"]
            elif existing_has_suffix and not has_suffix:
                remap[existing["id"]] = node["id"]
                canonical[key] = node
            elif len(node["id"]) < len(existing["id"]):
                remap[existing["id"]] = node["id"]
                canonical[key] = node
            else:
                remap[node["id"]] = existing["id"]

    if not remap:
        return nodes, edges

    print(f"[graphify] Deduplicated {len(remap)} duplicate node(s) by label.", file=sys.stderr)
    deduped_nodes = list(canonical.values())
    deduped_edges = []
    for edge in edges:
        e = dict(edge)
        e["source"] = remap.get(e["source"], e["source"])
        e["target"] = remap.get(e["target"], e["target"])
        if e["source"] != e["target"]:
            deduped_edges.append(e)
    return deduped_nodes, deduped_edges


def build_merge(
    new_chunks: list[dict],
    graph_path: str | Path = "graphify-out/graph.json",
    prune_sources: list[str] | None = None,
    *,
    directed: bool = False,
) -> nx.Graph:
    """加载现有图文件，合并新的提取块，并保存回文件。

    该函数用于增量更新图数据：读取已有的graph.json文件，将新的提取结果合并进去，
    然后保存回原路径。可选地支持删除已移除源文件对应的节点。

    Args:
        new_chunks: 新的提取字典列表（每个字典通常对应一次LLM提取结果）
        graph_path: 现有图文件的路径，默认为"graphify-out/graph.json"
        prune_sources: 需要删除其节点的源文件路径列表，默认为None表示不删除
        directed: True返回有向图(DiGraph)，False返回无向图(Graph)，默认为False

    Returns:
        合并后的NetworkX图对象

    Raises:
        ValueError: 当合并后节点数少于原图节点数且未显式指定prune_sources时抛出异常，
                    防止意外缩小图规模

    Note:
        该函数永远不会替换原有图数据——只会增加数据（或通过prune_sources显式删除）。
        可安全重复调用：现有节点和边都会被保留。
    """
    from networkx.readwrite import json_graph as _jg

    graph_path = Path(graph_path)
    if graph_path.exists():
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        try:
            existing_G = _jg.node_link_graph(data, edges="links")
        except TypeError:
            existing_G = _jg.node_link_graph(data)
        # Reconstruct as a plain extraction dict so build() can merge it
        existing_nodes = [{"id": n, **existing_G.nodes[n]} for n in existing_G.nodes]
        existing_edges = [
            {"source": u, "target": v, **d} for u, v, d in existing_G.edges(data=True)
        ]
        base = [{"nodes": existing_nodes, "edges": existing_edges}]
    else:
        base = []

    all_chunks = base + list(new_chunks)
    G = build(all_chunks, directed=directed)

    # Prune nodes from deleted source files
    if prune_sources:
        to_remove = [
            n for n, d in G.nodes(data=True)
            if d.get("source_file") in prune_sources
        ]
        G.remove_nodes_from(to_remove)
        if to_remove:
            print(f"[graphify] Pruned {len(to_remove)} node(s) from deleted sources.", file=sys.stderr)

    # Safety check: refuse to shrink the graph silently (#479)
    if graph_path.exists():
        existing_n = len(existing_nodes)
        new_n = G.number_of_nodes()
        if new_n < existing_n:
            raise ValueError(
                f"graphify: build_merge would shrink graph from {existing_n} → {new_n} nodes. "
                f"Pass prune_sources explicitly if you intend to remove nodes."
            )

    return G