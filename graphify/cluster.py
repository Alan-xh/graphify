"""
NetworkX图上的社区检测模块。优先使用Leiden算法（graspologic），
若不可用则回退到Louvain算法（networkx）。拆分过大的社区，并返回内聚性评分。
"""
from __future__ import annotations
import contextlib
import inspect
import io
import sys
import networkx as nx


def _suppress_output():
    """抑制库调用期间的stdout/stderr输出的上下文管理器。

    graspologic的leiden()函数会输出ANSI转义序列（进度条、彩色警告），
    在Windows PowerShell 5.1中会损坏滚动缓冲区（见issue #19）。
    在调用期间将stdout/stderr重定向到devnull可以防止此问题，
    同时不会丢失任何graphify输出。

    Returns:
        contextlib.redirect_stdout: 重定向标准输出的上下文管理器
    """
    return contextlib.redirect_stdout(io.StringIO())


def _partition(G: nx.Graph) -> dict[str, int]:
    """执行社区检测算法，返回节点到社区ID的映射。

    优先尝试Leiden算法（graspologic库）——质量最佳。
    若graspologic未安装，则回退到networkx内置的Louvain算法。

    抑制graspologic的输出，防止ANSI转义码在Windows PowerShell 5.1
    终端中损坏滚动缓冲区。

    Args:
        G: NetworkX图对象（无向图）

    Returns:
        dict[str, int]: 键为节点ID，值为所属社区ID的字典

    Notes:
        社区ID为整数索引，从0开始连续编号
    """
    try:
        from graspologic.partition import leiden
        # 抑制graspologic输出，防止ANSI转义码损坏PowerShell 5.1滚动缓冲区（issue #19）
        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with _suppress_output():
                result = leiden(G)
        finally:
            sys.stderr = old_stderr
        return result
    except ImportError:
        pass

    # 回退方案：networkx的Louvain算法（自networkx 2.7版本可用）
    # 检查参数签名以保持跨NetworkX版本的兼容性——max_level参数
    # 在后续版本中添加，可防止大型稀疏图上出现挂起问题
    kwargs: dict = {"seed": 42, "threshold": 1e-4}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = 10
    communities = nx.community.louvain_communities(G, **kwargs)
    return {node: cid for cid, nodes in enumerate(communities) for node in nodes}


_MAX_COMMUNITY_FRACTION = 0.25   # 超过图节点数25%的社区将被拆分
_MIN_SPLIT_SIZE = 10             # 仅当社区至少有这么多节点时才进行拆分


def cluster(G: nx.Graph) -> dict[int, list[str]]:
    """执行Leiden社区检测算法，返回社区ID到节点列表的映射。

    社区ID在多次运行中保持稳定：0表示拆分后最大的社区。
    过大的社区（>图节点数的25%，且至少10个节点）通过在子图上
    执行第二轮Leiden检测来进行拆分。

    Args:
        G: NetworkX图对象（支持有向图或无向图）

    Returns:
        dict[int, list[str]]: 键为社区ID（整数），值为该社区包含的节点ID列表
                              列表中的节点ID按字母顺序排序

    Notes:
        有向图会被内部转换为无向图，因为Louvain/Leiden算法要求输入为无向图。
        孤立节点（度为0）会被分配为独立的单节点社区。
    """
    if G.number_of_nodes() == 0:
        return {}
    if G.is_directed():
        G = G.to_undirected()
    if G.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(sorted(G.nodes))}

    # Leiden算法会发出警告并丢弃孤立节点——提前单独处理它们
    isolates = [n for n in G.nodes() if G.degree(n) == 0]
    connected_nodes = [n for n in G.nodes() if G.degree(n) > 0]
    connected = G.subgraph(connected_nodes)

    raw: dict[int, list[str]] = {}
    if connected.number_of_nodes() > 0:
        partition = _partition(connected)
        for node, cid in partition.items():
            raw.setdefault(cid, []).append(node)

    # 每个孤立节点成为独立的单节点社区
    next_cid = max(raw.keys(), default=-1) + 1
    for node in isolates:
        raw[next_cid] = [node]
        next_cid += 1

    # 拆分过大的社区
    max_size = max(_MIN_SPLIT_SIZE, int(G.number_of_nodes() * _MAX_COMMUNITY_FRACTION))
    final_communities: list[list[str]] = []
    for nodes in raw.values():
        if len(nodes) > max_size:
            final_communities.extend(_split_community(G, nodes))
        else:
            final_communities.append(nodes)

    # 按社区大小降序重新索引，保证确定性排序
    final_communities.sort(key=len, reverse=True)
    return {i: sorted(nodes) for i, nodes in enumerate(final_communities)}


def _split_community(G: nx.Graph, nodes: list[str]) -> list[list[str]]:
    """在社区子图上执行第二轮Leiden检测，以进一步拆分过大社区。

    Args:
        G: 原始完整图对象
        nodes: 待拆分的社区节点列表

    Returns:
        list[list[str]]: 拆分后的子社区列表，每个子社区为节点ID列表，
                         列表中的节点ID按字母顺序排序

    Notes:
        若子图内无边，则拆分为单个节点。
        若拆分失败（如发生异常），则返回原始节点列表作为单个社区。
    """
    subgraph = G.subgraph(nodes)
    if subgraph.number_of_edges() == 0:
        # 无边连接——拆分为独立节点
        return [[n] for n in sorted(nodes)]
    try:
        sub_partition = _partition(subgraph)
        sub_communities: dict[int, list[str]] = {}
        for node, cid in sub_partition.items():
            sub_communities.setdefault(cid, []).append(node)
        if len(sub_communities) <= 1:
            return [sorted(nodes)]
        return [sorted(v) for v in sub_communities.values()]
    except Exception:
        return [sorted(nodes)]


def cohesion_score(G: nx.Graph, community_nodes: list[str]) -> float:
    """计算社区内聚性分数：实际内部边数与最大可能边数之比。

    Args:
        G: 完整图对象
        community_nodes: 社区内的节点ID列表

    Returns:
        float: 内聚性分数，取值范围[0.0, 1.0]，保留两位小数
               单节点社区的分数为1.0

    Examples:
        >>> import networkx as nx
        >>> G = nx.complete_graph(5)
        >>> cohesion_score(G, ['0', '1', '2'])
        1.0
    """
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    return round(actual / possible, 2) if possible > 0 else 0.0


def score_all(G: nx.Graph, communities: dict[int, list[str]]) -> dict[int, float]:
    """计算所有社区的内聚性评分。

    Args:
        G: 完整图对象
        communities: 社区字典，键为社区ID，值为节点列表

    Returns:
        dict[int, float]: 键为社区ID，值为对应的内聚性评分

    See Also:
        cohesion_score: 计算单个社区内聚性分数的函数
    """
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}