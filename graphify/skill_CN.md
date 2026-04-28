---
name: graphify
description: "任何输入（代码、文档、论文、图像）→ 知识图谱 → 聚类社区 → HTML + JSON + 审计报告"
trigger: /graphify
---

# /graphify

将任意文件夹中的文件转化为可导航的知识图谱，支持社区检测，包含诚实的审计轨迹，并输出三种结果：交互式 HTML、GraphRAG 就绪的 JSON，以及用通俗语言撰写的 GRAPH_REPORT.md。

## 使用方法

```
/graphify                                             # 对当前目录执行完整流程 → Obsidian 仓库
/graphify <path>                                      # 对指定路径执行完整流程
/graphify <path> --mode deep                          # 深度提取，生成更丰富的推断边
/graphify <path> --update                             # 增量更新 - 仅重新提取新增或修改的文件
/graphify <path> --directed                           # 构建有向图（保留边的方向：source→target）
/graphify <path> --whisper-model medium               # 使用更大的 Whisper 模型以获得更好的转录准确率
/graphify <path> --cluster-only                       # 仅对现有图谱重新运行聚类
/graphify <path> --no-viz                             # 跳过可视化，仅生成报告和 JSON
/graphify <path> --html                               # （默认生成 HTML，此标志为无操作）
/graphify <path> --svg                                # 同时导出 graph.svg（可嵌入 Notion、GitHub）
/graphify <path> --graphml                            # 导出 graph.graphml（适用于 Gephi、yEd）
/graphify <path> --neo4j                              # 生成 graphify-out/cypher.txt 用于 Neo4j
/graphify <path> --neo4j-push bolt://localhost:7687   # 直接推送到 Neo4j
/graphify <path> --mcp                                # 启动 MCP stdio 服务器供 Agent 访问
/graphify <path> --watch                              # 监控文件夹，文件变更时自动重建（无需 LLM）
/graphify <path> --wiki                               # 构建可供 Agent 爬取的 Wiki（index.md + 每个社区一篇文章）
/graphify <path> --obsidian --obsidian-dir ~/vaults/my-project  # 将 vault 写入自定义路径（例如已有仓库）
/graphify add <url>                                   # 抓取 URL，保存到 ./raw 并更新图谱
/graphify add <url> --author "Name"                   # 标记作者
/graphify add <url> --contributor "Name"              # 标记谁将此内容加入语料库
/graphify query "<question>"                          # BFS 遍历 - 获取广泛上下文
/graphify query "<question>" --dfs                    # DFS - 追踪特定路径
/graphify query "<question>" --budget 1500            # 将答案限制在 N 个 token 内
/graphify path "AuthModule" "Database"                # 查找两个概念之间的最短路径
/graphify explain "SwinTransformer"                   # 用通俗语言解释某个节点
```

## graphify 的用途

graphify 围绕 Andrej Karpathy 的 /raw 文件夹工作流设计：把任何内容（论文、推文、截图、代码、笔记）丢进一个文件夹，即可获得结构化的知识图谱，帮助你发现原本不知道的关联。

它能做到 Claude 单独无法做到的三件事：
1. **持久化图谱** —— 关系存储在 `graphify-out/graph.json` 中，可跨会话保留。几周后仍可直接查询，无需重新阅读所有内容。
2. **诚实的审计轨迹** —— 每条边都被标记为 EXTRACTED（提取）、INFERRED（推断）或 AMBIGUOUS（模糊）。你能清楚区分什么是真正找到的，什么是被创造的。
3. **跨文档惊喜** —— 社区检测能发现不同文件中概念之间的关联，这些关联是你自己永远不会直接想到的。

适用场景：
- 你刚接手的新代码库（在动手前先理解整体架构）
- 阅读清单（论文 + 推文 + 笔记 → 一个可导航的图谱）
- 研究语料库（引用图谱 + 概念图谱合二为一）
- 你的个人 /raw 文件夹（把所有东西丢进去，让它不断生长，然后查询它）

## 被调用时你必须执行的操作

如果没有提供路径，则默认使用 `.`（当前目录）。不要询问用户路径。

请按以下顺序执行步骤，不要跳过任何步骤。

### Step 1 - 确保 graphify 已安装

```bash
# 检测正确的 Python 解释器（兼容 pipx、venv、系统安装）
GRAPHIFY_BIN=$(which graphify 2>/dev/null)
if [ -n "$GRAPHIFY_BIN" ]; then
    PYTHON=$(head -1 "$GRAPHIFY_BIN" | tr -d '#!')
    case "$PYTHON" in
        *[!a-zA-Z0-9/_.-]*) PYTHON="python3" ;;
    esac
else
    PYTHON="python3"
fi
"$PYTHON" -c "import graphify" 2>/dev/null || "$PYTHON" -m pip install graphifyy -q 2>/dev/null || "$PYTHON" -m pip install graphifyy -q --break-system-packages 2>&1 | tail -3
# 写入解释器路径供后续步骤使用（跨调用持久化）
mkdir -p graphify-out
"$PYTHON" -c "import sys; open('graphify-out/.graphify_python', 'w').write(sys.executable)"
```

如果导入成功，则不打印任何内容，直接进入 Step 2。

**在之后的所有 bash 代码块中，请将 `python3` 替换为 `$(cat graphify-out/.graphify_python)` 以使用正确的解释器。**

### Step 2 - 检测文件

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.detect import detect
from pathlib import Path
result = detect(Path('INPUT_PATH'))
print(json.dumps(result))
" > graphify-out/.graphify_detect.json
```

将 `INPUT_PATH` 替换为用户提供的实际路径。**不要** cat 或打印该 JSON —— 请静默读取并给出干净的摘要：

```
语料库：X 个文件 · 约 Y 个词
  代码：     N 个文件 (.py .ts .go ...)
  文档：     N 个文件 (.md .txt ...)
  论文：     N 个文件 (.pdf ...)
  图像：     N 个文件
  视频：     N 个文件 (.mp4 .mp3 ...)
```

省略文件数为 0 的类别。

然后根据结果处理：
- 如果 `total_files` 为 0：停止并输出“No supported files found in [path].”
- 如果 `skipped_sensitive` 非空：提及跳过的文件数量，不要列出文件名。
- 如果 `total_words` > 2,000,000 或 `total_files` > 200：显示警告及按文件数排序的前 5 个子目录，然后询问用户要在哪个子文件夹上运行。等待用户回复后再继续。
- 否则：如果检测到视频文件，则进入 Step 2.5；否则直接进入 Step 3。

### Step 2.5 - 转录视频 / 音频文件（仅当检测到视频文件时执行）

如果 `detect` 返回的 `video` 文件数为零，则完全跳过此步骤。

视频和音频文件无法直接读取。需先转录为文本，再将转录结果作为文档文件在 Step 3 中处理。

**策略：** 从 `graphify-out/.graphify_detect.json`（或之前运行的分析文件）中读取 god nodes。你作为语言模型，可自行根据这些标签写一句话领域提示。然后将此提示传给 Whisper 作为初始提示。无需额外 API 调用。

**但是**，如果语料库中**只有**视频文件而没有任何其他文档或代码，则使用通用后备提示：`"Use proper punctuation and paragraph breaks."`

**步骤 1 - 自行编写 Whisper 提示。**

读取 detect 输出或分析结果中的顶级 god node 标签，然后撰写简短的领域提示句，例如：

- 标签：`transformer, attention, encoder, decoder` → `"Machine learning research on transformer architectures and attention mechanisms. Use proper punctuation and paragraph breaks."`
- 标签：`kubernetes, deployment, pod, helm` → `"DevOps discussion about Kubernetes deployments and Helm charts. Use proper punctuation and paragraph breaks."`

将它设置为 `WHISPER_PROMPT` 以供下一步命令使用。

**步骤 2 - 执行转录：**

```bash
GRAPHIFY_WHISPER_MODEL=base  # 或用户传入的 --whisper-model 值
$(cat graphify-out/.graphify_python) -c "
import json, os
from pathlib import Path
from graphify.transcribe import transcribe_all

detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
video_files = detect.get('files', {}).get('video', [])
prompt = os.environ.get('GRAPHIFY_WHISPER_PROMPT', 'Use proper punctuation and paragraph breaks.')

transcript_paths = transcribe_all(video_files, initial_prompt=prompt)
print(json.dumps(transcript_paths))
" > graphify-out/.graphify_transcripts.json
```

转录完成后：
- 从 `graphify-out/.graphify_transcripts.json` 读取转录路径
- 在 Step 3B 调度语义子代理前，将这些路径加入文档列表
- 打印转录数量：`Transcribed N video file(s) -> treating as docs`
- 如果某个文件转录失败，打印警告并继续处理其余文件

**Whisper 模型：** 默认使用 `base`。如果用户传入 `--whisper-model <name>`，则在运行上述命令前设置环境变量 `GRAPHIFY_WHISPER_MODEL=<name>`。

### Step 3 - 提取实体和关系

**开始前：** 注意用户是否提供了 `--mode deep`。如果有，则必须在 Step B2 中向每个语义子代理传递 `DEEP_MODE=true`。请从原始调用中跟踪此参数，不要丢失。

此步骤分为两部分：**结构化提取**（确定性、免费）和**语义提取**（使用 Claude，会消耗 token）。

**同时运行 Part A（AST）和 Part B（语义）。** 在同一条消息中同时调度所有语义子代理并启动 AST 提取。由于它们处理不同类型的文件，可以并行执行。在 Part C 中合并结果。

#### 第 A 部分 - 代码文件的结构化提取

对于检测到的任何代码文件，与第 B 部分子代理并行运行 AST 提取：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.extract import collect_files, extract
from pathlib import Path
import json

code_files = []
detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
for f in detect.get('files', {}).get('code', []):
    code_files.extend(collect_files(Path(f)) if Path(f).is_dir() else [Path(f)])

if code_files:
    result = extract(code_files, cache_root=Path('.'))
    Path('graphify-out/.graphify_ast.json').write_text(json.dumps(result, indent=2))
    print(f'AST: {len(result[\"nodes\"])} nodes, {len(result[\"edges\"])} edges')
else:
    Path('graphify-out/.graphify_ast.json').write_text(json.dumps({'nodes':[],'edges':[],'input_tokens':0,'output_tokens':0}))
    print('No code files - skipping AST extraction')
"
```

#### 第 B 部分 - 语义提取（并行子代理）

**快速路径：** 如果检测结果中文档、论文和图像数量均为零（纯代码语料库），则完全跳过第 B 部分，直接进入第 C 部分。AST 已处理代码——语义子代理无事可做。

**强制要求：您必须在此处使用 Agent 工具。** 禁止自己逐个读取文件——这会慢 5-10 倍。如果您不使用 Agent 工具，就是操作错误。

在分派子代理之前，先打印时间估算：
- 从 `graphify-out/.graphify_detect.json` 加载 `total_words` 和文件数量
- 估算所需代理数量：`ceil(uncached_non_code_files / 22)`（分块大小为 20-25）
- 估算时间：每个代理批次约 45 秒（它们并行运行，因此总时间 ≈ 45 秒 × ceil(代理数/并行限制)）
- 打印："Semantic extraction: ~N files → X agents, estimated ~Ys"

**步骤 B0 - 先检查提取缓存**

在分派任何子代理之前，先检查哪些文件已有缓存的提取结果：

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.cache import check_semantic_cache
from pathlib import Path

detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
all_files = [f for files in detect['files'].values() for f in files]

cached_nodes, cached_edges, cached_hyperedges, uncached = check_semantic_cache(all_files)

if cached_nodes or cached_edges or cached_hyperedges:
    Path('graphify-out/.graphify_cached.json').write_text(json.dumps({'nodes': cached_nodes, 'edges': cached_edges, 'hyperedges': cached_hyperedges}))
Path('graphify-out/.graphify_uncached.txt').write_text('\n'.join(uncached))
print(f'Cache: {len(all_files)-len(uncached)} files hit, {len(uncached)} files need extraction')
"
```

仅针对 `graphify-out/.graphify_uncached.txt` 中列出的文件分派子代理。如果所有文件均已缓存，则直接跳至第 C 部分。

**步骤 B1 - 拆分成块**

从 `graphify-out/.graphify_uncached.txt` 加载文件列表。将文件拆分成每块 20-25 个文件的块。每个图像文件单独占一个块（视觉处理需要独立上下文）。拆分时，尽量将同一目录下的文件分组在一起，以便相关工件落在同一块中，更容易提取跨文件关系。

**步骤 B2 - 在单条消息中分派所有子代理**

在**同一条响应**中多次调用 Agent 工具——每个块调用一次。这是实现并行运行的唯一方式。如果您一个一个调用、等待、再调用，就会变成串行执行，从而失去并行意义。

**重要 - 子代理类型：** 始终使用 `subagent_type="general-purpose"`。**不要**使用 `Explore` —— 它是只读的，无法将块文件写入磁盘，会导致提取结果静默丢失。General-purpose 拥有写入和 Bash 访问权限，这是子代理所需的。

3 个块的实际调用示例：
```
[Agent 工具调用 1：文件 1-15, subagent_type="general-purpose"]
[Agent 工具调用 2：文件 16-30, subagent_type="general-purpose"]
[Agent 工具调用 3：文件 31-45, subagent_type="general-purpose"]
```
以上三个调用必须放在**同一条消息**中，而非分多次消息发送。

每个子代理接收以下**完全相同**的提示（需替换 FILE_LIST、CHUNK_NUM、TOTAL_CHUNKS 和 DEEP_MODE）：

```
您是 graphify 提取子代理。请读取列出的文件并提取知识图谱片段。
仅输出符合以下 schema 的有效 JSON —— 不要有任何解释、不要有 markdown 代码块、不要有前言。

文件（第 CHUNK_NUM 块 / 共 TOTAL_CHUNKS 块）：
FILE_LIST

规则：
- EXTRACTED：源文件中明确存在的关系（import、call、citation、“见 §3.2”）
- INFERRED：合理的推断（共享数据结构、隐含依赖）
- AMBIGUOUS：不确定的关系 — 标记为待审查，不要省略

代码文件：重点提取 AST 无法发现的语义边（调用关系、共享数据、架构模式）。
  不要重复提取 import —— AST 已处理这些。
文档/论文文件：提取命名概念、实体、引用。同时提取 rationale（解释决策原因、选择的权衡或设计意图的部分）。这些将成为节点，并通过 `rationale_for` 边指向它们所解释的概念。
图像文件：使用视觉能力理解图像**是什么** —— 而非仅 OCR。
  UI 截图：布局模式、设计决策、关键元素、用途。
  图表：指标、趋势/洞见、数据来源。
  推文/帖子：将主张作为节点，包含作者和提到的概念。
  示意图：组件及其连接。
  研究图表：它所演示的内容、方法、结果。
  手写/白板：想法和箭头，不确定的读取标记为 AMBIGUOUS。

DEEP_MODE（如果传入了 --mode deep）：积极提取 INFERRED 边 —— 包括间接依赖、共享假设、潜在耦合。不确定的标记为 AMBIGUOUS 而非省略。

语义相似性：如果本块中的两个概念解决了相同问题或代表相同想法，但没有任何结构链接（无 import、无调用、无引用），则添加 `semantically_similar_to` 边，标记为 INFERRED，并附带 confidence_score（0.6-0.95）。示例：
- 两个从不互相调用的用户输入验证函数
- 代码中的类与论文中描述同一算法的概念
- 处理相同失败模式但方式不同的两种错误类型
仅在相似性确实不明显且具有跨领域意义时添加。不要为显而易见的相似内容添加。

超边（Hyperedges）：如果 3 个或更多节点共同参与一个无法仅用成对边表示的共享概念、流程或模式，则在顶层 `hyperedges` 数组中添加一个超边。示例：
- 所有实现同一协议或接口的类
- 认证流程中的所有函数（即使它们不都互相调用）
- 论文某一节中形成一个连贯思想的所有概念
谨慎使用 —— 仅当群体关系能提供超出成对边的信息时。每个块最多 3 个超边。

如果文件包含 YAML frontmatter（--- ... ---），请将 source_url、captured_at、author、contributor 复制到该文件产生的每个节点上。

confidence_score 在每条边上都是**必需**的 —— 绝不能省略，也不要默认使用 0.5：
- EXTRACTED 边：confidence_score 始终为 1.0
- INFERRED 边：逐条独立评估。
  有直接结构证据（共享数据结构、明确依赖）：0.8-0.9
  合理推断但有一定不确定性：0.6-0.7
  较弱或推测性：0.4-0.5。大多数边应在 0.6-0.9 之间，而非 0.5。
- AMBIGUOUS 边：0.1-0.3

节点 ID 格式：仅使用小写字母 `[a-z0-9_]`，不允许出现点号或斜杠。格式为：`{stem}_{entity}`，其中 stem 是去掉扩展名的文件名，entity 是符号名称，两者均需规范化（小写，非字母数字字符替换为 `_`）。示例：`src/auth/session.py` + `ValidateToken` → `session_validatetoken`。此格式必须与 AST 提取器生成的 ID 一致，以便代码节点与语义节点能正确交叉引用。

请**仅**输出以下 JSON（不要有其他任何文字）：
{"nodes":[{"id":"session_validatetoken","label":"Human Readable Name","file_type":"code|document|paper|image","source_file":"relative/path","source_location":null,"source_url":null,"captured_at":null,"author":null,"contributor":null}],"edges":[{"source":"node_id","target":"node_id","relation":"calls|implements|references|cites|conceptually_related_to|shares_data_with|semantically_similar_to|rationale_for","confidence":"EXTRACTED|INFERRED|AMBIGUOUS","confidence_score":1.0,"source_file":"relative/path","source_location":null,"weight":1.0}],"hyperedges":[{"id":"snake_case_id","label":"Human Readable Label","nodes":["node_id1","node_id2","node_id3"],"relation":"participate_in|implement|form","confidence":"EXTRACTED|INFERRED","confidence_score":0.75,"source_file":"relative/path"}],"input_tokens":0,"output_tokens":0}
```

**步骤 B3 - 收集、缓存与合并**

等待所有子代理完成。对于每个结果：
- 检查 `graphify-out/.graphify_chunk_NN.json` 文件是否存在于磁盘上 —— 这是成功的信号
- 如果文件存在且包含有效的包含 `nodes` 和 `edges` 的 JSON，则纳入结果并保存到缓存
- 如果文件缺失，子代理很可能被以只读模式（Explore 类型）分派 —— 打印警告："chunk N missing from disk — subagent may have been read-only. Re-run with general-purpose agent." 不要静默跳过。
- 如果子代理失败或返回无效 JSON，打印警告并跳过该块 —— 不要中止整个流程

如果超过一半的块失败或缺失，请停止并告知用户重新运行，并确保使用了 `subagent_type="general-purpose"`。

将新结果保存到缓存：
```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.cache import save_semantic_cache
from pathlib import Path

new = json.loads(Path('graphify-out/.graphify_semantic_new.json').read_text()) if Path('graphify-out/.graphify_semantic_new.json').exists() else {'nodes':[],'edges':[],'hyperedges':[]}
saved = save_semantic_cache(new.get('nodes', []), new.get('edges', []), new.get('hyperedges', []))
print(f'Cached {saved} files')
"
```

将缓存结果与新结果合并为 `graphify-out/.graphify_semantic.json`：
```bash
$(cat graphify-out/.graphify_python) -c "
import json
from pathlib import Path

cached = json.loads(Path('graphify-out/.graphify_cached.json').read_text()) if Path('graphify-out/.graphify_cached.json').exists() else {'nodes':[],'edges':[],'hyperedges':[]}
new = json.loads(Path('graphify-out/.graphify_semantic_new.json').read_text()) if Path('graphify-out/.graphify_semantic_new.json').exists() else {'nodes':[],'edges':[],'hyperedges':[]}

all_nodes = cached['nodes'] + new.get('nodes', [])
all_edges = cached['edges'] + new.get('edges', [])
all_hyperedges = cached.get('hyperedges', []) + new.get('hyperedges', [])
seen = set()
deduped = []
for n in all_nodes:
    if n['id'] not in seen:
        seen.add(n['id'])
        deduped.append(n)

merged = {
    'nodes': deduped,
    'edges': all_edges,
    'hyperedges': all_hyperedges,
    'input_tokens': new.get('input_tokens', 0),
    'output_tokens': new.get('output_tokens', 0),
}
Path('graphify-out/.graphify_semantic.json').write_text(json.dumps(merged, indent=2))
print(f'Extraction complete - {len(deduped)} nodes, {len(all_edges)} edges ({len(cached[\"nodes\"])} from cache, {len(new.get(\"nodes\",[]))} new)')
"
```
清理临时文件：`rm -f graphify-out/.graphify_cached.json graphify-out/.graphify_uncached.txt graphify-out/.graphify_semantic_new.json`

#### 第 C 部分 - 将 AST 与语义结果合并为最终提取结果

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from pathlib import Path

ast = json.loads(Path('graphify-out/.graphify_ast.json').read_text())
sem = json.loads(Path('graphify-out/.graphify_semantic.json').read_text())

# 合并：AST 节点优先，语义节点按 id 去重
seen = {n['id'] for n in ast['nodes']}
merged_nodes = list(ast['nodes'])
for n in sem['nodes']:
    if n['id'] not in seen:
        merged_nodes.append(n)
        seen.add(n['id'])

merged_edges = ast['edges'] + sem['edges']
merged_hyperedges = sem.get('hyperedges', [])
merged = {
    'nodes': merged_nodes,
    'edges': merged_edges,
    'hyperedges': merged_hyperedges,
    'input_tokens': sem.get('input_tokens', 0),
    'output_tokens': sem.get('output_tokens', 0),
}
Path('graphify-out/.graphify_extract.json').write_text(json.dumps(merged, indent=2))
total = len(merged_nodes)
edges = len(merged_edges)
print(f'Merged: {total} nodes, {edges} edges ({len(ast[\"nodes\"])} AST + {len(sem[\"nodes\"])} semantic)')
"
```

### 步骤 4 - 构建图谱、聚类、分析、生成输出

**开始前：** 注意是否传入了 `--directed` 参数。如果有，则在下面的代码块中向 `build_from_json()` 传入 `directed=True`。这将构建一个保留边方向（source→target）的 `DiGraph`，而非默认的无向 `Graph`。

```bash
mkdir -p graphify-out
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
detection  = json.loads(Path('graphify-out/.graphify_detect.json').read_text())

G = build_from_json(extraction)
communities = cluster(G)
cohesion = score_all(G, communities)
tokens = {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}
gods = god_nodes(G)
surprises = surprising_connections(G, communities)
labels = {cid: 'Community ' + str(cid) for cid in communities}
# 占位问题 —— 将在步骤 5 中用真实标签重新生成
questions = suggest_questions(G, communities, labels)

report = generate(G, communities, cohesion, labels, gods, surprises, detection, tokens, 'INPUT_PATH', suggested_questions=questions)
Path('graphify-out/GRAPH_REPORT.md').write_text(report)
to_json(G, communities, 'graphify-out/graph.json')

analysis = {
    'communities': {str(k): v for k, v in communities.items()},
    'cohesion': {str(k): v for k, v in cohesion.items()},
    'gods': gods,
    'surprises': surprises,
    'questions': questions,
}
Path('graphify-out/.graphify_analysis.json').write_text(json.dumps(analysis, indent=2))
if G.number_of_nodes() == 0:
    print('ERROR: Graph is empty - extraction produced no nodes.')
    print('Possible causes: all files were skipped, binary-only corpus, or extraction failed.')
    raise SystemExit(1)
print(f'Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(communities)} communities')
"
```

如果此步骤打印出 `ERROR: Graph is empty`，请停止并告知用户发生了什么 —— 不要继续进行标签生成或可视化。

将 INPUT_PATH 替换为实际路径。

### 第 5 步 - 为社区打标签

读取 `graphify-out/.graphify_analysis.json` 文件。对于每个社区的 key，查看其节点标签，并为其写一个 **2-5 个词** 的简洁自然语言名称（例如：“注意力机制”、“训练流程”、“数据加载”）。

然后重新生成报告并保存标签供可视化工具使用：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.cluster import score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
detection  = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())

G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}
cohesion = {int(k): v for k, v in analysis['cohesion'].items()}
tokens = {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}

# LABELS - 请将下面替换为你上面选择的社区名称
labels = LABELS_DICT

# 使用真实社区标签重新生成问题（标签会影响问题措辞）
questions = suggest_questions(G, communities, labels)

report = generate(G, communities, cohesion, labels, analysis['gods'], analysis['surprises'], detection, tokens, 'INPUT_PATH', suggested_questions=questions)
Path('graphify-out/GRAPH_REPORT.md').write_text(report)
Path('graphify-out/.graphify_labels.json').write_text(json.dumps({str(k): v for k, v in labels.items()}))
print('报告已使用社区标签更新')
"
```

请将 `LABELS_DICT` 替换为你实际构造的字典（例如：`{0: "注意力机制", 1: "训练流程"}`）。
将 `INPUT_PATH` 替换为实际输入路径。

### 第 6 步 - 生成 Obsidian 仓库（可选）+ HTML

**始终生成 HTML**（除非使用了 `--no-viz` 参数）。**仅当明确给出 `--obsidian` 参数时才生成 Obsidian 仓库**，否则跳过（因为它会为每个节点生成一个文件）。

如果给出了 `--obsidian` 参数：

- 如果同时给出了 `--obsidian-dir <path>`，则使用该路径作为仓库目录；否则默认使用 `graphify-out/obsidian`。

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.export import to_obsidian, to_canvas
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())
labels_raw = json.loads(Path('graphify-out/.graphify_labels.json').read_text()) if Path('graphify-out/.graphify_labels.json').exists() else {}

G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}
cohesion = {int(k): v for k, v in analysis['cohesion'].items()}
labels = {int(k): v for k, v in labels_raw.items()}

obsidian_dir = 'OBSIDIAN_DIR'  # 替换为 --obsidian-dir 的值，若未给出则使用 'graphify-out/obsidian'

n = to_obsidian(G, communities, obsidian_dir, community_labels=labels or None, cohesion=cohesion)
print(f'Obsidian 仓库：{n} 个笔记，路径：{obsidian_dir}/')

to_canvas(G, communities, f'{obsidian_dir}/graph.canvas', community_labels=labels or None)
print(f'Canvas 文件：{obsidian_dir}/graph.canvas - 在 Obsidian 中打开可看到按社区分组的结构化布局')
print()
print(f'在 Obsidian 中打开 {obsidian_dir}/ 作为仓库。')
print('  图谱视图   - 节点按社区自动着色')
print('  graph.canvas - 带有社区分组的结构化布局')
print('  _COMMUNITY_* - 包含内聚分数和 dataview 查询的社区概览笔记')
"
```

**始终生成 HTML 图谱**（除非使用 `--no-viz`）：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.export import to_html
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())
labels_raw = json.loads(Path('graphify-out/.graphify_labels.json').read_text()) if Path('graphify-out/.graphify_labels.json').exists() else {}

G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}
labels = {int(k): v for k, v in labels_raw.items()}

if G.number_of_nodes() > 5000:
    print(f'图谱包含 {G.number_of_nodes()} 个节点 - 节点过多，HTML 可视化可能不适合。请改用 Obsidian 仓库。')
else:
    to_html(G, communities, 'graphify-out/graph.html', community_labels=labels or None)
    print('graph.html 已生成 - 可直接用浏览器打开，无需服务器')
"
```

### 第 6b 步 - 生成 Wiki（仅当使用 `--wiki` 参数时）

**仅当原始命令中明确给出 `--wiki` 参数时才执行此步骤。**

请在第 9 步（清理）之前运行此步骤，以确保 `.graphify_labels.json` 仍然可用。

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.build import build_from_json
from graphify.wiki import to_wiki
from graphify.analyze import god_nodes
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())
labels_raw = json.loads(Path('graphify-out/.graphify_labels.json').read_text()) if Path('graphify-out/.graphify_labels.json').exists() else {}

G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}
cohesion = {int(k): v for k, v in analysis['cohesion'].items()}
labels = {int(k): v for k, v in labels_raw.items()}
gods = god_nodes(G)

n = to_wiki(G, communities, 'graphify-out/wiki', community_labels=labels or None, cohesion=cohesion, god_nodes_data=gods)
print(f'Wiki：{n} 篇文章已写入 graphify-out/wiki/')
print('  graphify-out/wiki/index.md  ->  入口文件')
"
```

### 第 7 步 - Neo4j 导出（仅当使用 `--neo4j` 或 `--neo4j-push` 参数时）

**如果使用 `--neo4j`** - 生成 Cypher 文件供手动导入：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.export import to_cypher
from pathlib import Path

G = build_from_json(json.loads(Path('graphify-out/.graphify_extract.json').read_text()))
to_cypher(G, 'graphify-out/cypher.txt')
print('cypher.txt 已生成 - 使用以下命令导入：cypher-shell < graphify-out/cypher.txt')
"
```

**如果使用 `--neo4j-push <uri>`** - 直接推送到正在运行的 Neo4j 实例。若未提供凭证，需询问用户：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.export import push_to_neo4j
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())
G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}

result = push_to_neo4j(G, uri='NEO4J_URI', user='NEO4J_USER', password='NEO4J_PASSWORD', communities=communities)
print(f'已推送到 Neo4j：{result[\"nodes\"]} 个节点，{result[\"edges\"]} 条边')
"
```

请将 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD` 替换为实际值。默认 URI 为 `bolt://localhost:7687`，默认用户为 `neo4j`。使用 MERGE 语句，可安全重复执行。

### 第 7b 步 - SVG 导出（仅当使用 `--svg` 参数时）

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.export import to_svg
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())
labels_raw = json.loads(Path('graphify-out/.graphify_labels.json').read_text()) if Path('graphify-out/.graphify_labels.json').exists() else {}

G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}
labels = {int(k): v for k, v in labels_raw.items()}

to_svg(G, communities, 'graphify-out/graph.svg', community_labels=labels or None)
print('graph.svg 已生成 - 可嵌入 Obsidian、Notion、GitHub README')
"
```

### 第 7c 步 - GraphML 导出（仅当使用 `--graphml` 参数时）

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.build import build_from_json
from graphify.export import to_graphml
from pathlib import Path

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
analysis   = json.loads(Path('graphify-out/.graphify_analysis.json').read_text())

G = build_from_json(extraction)
communities = {int(k): v for k, v in analysis['communities'].items()}

to_graphml(G, communities, 'graphify-out/graph.graphml')
print('graph.graphml 已生成 - 可在 Gephi、yEd 或其他支持 GraphML 的工具中打开')
"
```

### 第 7d 步 - MCP 服务（仅当使用 `--mcp` 参数时）

```bash
python3 -m graphify.serve graphify-out/graph.json
```

这将启动一个 stdio MCP 服务，暴露以下工具：`query_graph`、`get_node`、`get_neighbors`、`get_community`、`god_nodes`、`graph_stats`、`shortest_path`。可将其添加到 Claude Desktop 或任何支持 MCP 的代理编排器中，以便其他代理实时查询图谱。

在 Claude Desktop 中的配置示例（添加到 `claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "graphify": {
      "command": "python3",
      "args": ["-m", "graphify.serve", "/absolute/path/to/graphify-out/graph.json"]
    }
  }
}
```

### 第 8 步 - Token 压缩基准测试（仅当 total_words > 5000 时）

如果 `graphify-out/.graphify_detect.json` 中的 `total_words` 大于 5000，则运行：

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.benchmark import run_benchmark, print_benchmark
from pathlib import Path

detection = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
result = run_benchmark('graphify-out/graph.json', corpus_words=detection['total_words'])
print_benchmark(result)
"
```

请将输出直接粘贴到聊天中。如果 `total_words <= 5000`，则静默跳过——此时图的价值在于结构清晰度，而非 token 压缩。

---

### 第 9 步 - 保存清单、更新成本记录、清理并报告

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from pathlib import Path
from datetime import datetime, timezone
from graphify.detect import save_manifest

# 保存 manifest 以支持 --update
detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
save_manifest(detect['files'])

# 更新累计成本记录
extract = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
input_tok = extract.get('input_tokens', 0)
output_tok = extract.get('output_tokens', 0)

cost_path = Path('graphify-out/cost.json')
if cost_path.exists():
    cost = json.loads(cost_path.read_text())
else:
    cost = {'runs': [], 'total_input_tokens': 0, 'total_output_tokens': 0}

cost['runs'].append({
    'date': datetime.now(timezone.utc).isoformat(),
    'input_tokens': input_tok,
    'output_tokens': output_tok,
    'files': detect.get('total_files', 0),
})
cost['total_input_tokens'] += input_tok
cost['total_output_tokens'] += output_tok
cost_path.write_text(json.dumps(cost, indent=2))

print(f'本次运行：{input_tok:,} 输入 token，{output_tok:,} 输出 token')
print(f'历史累计：{cost[\"total_input_tokens\"]:,} 输入，{cost[\"total_output_tokens\"]:,} 输出（共 {len(cost[\"runs\"])} 次运行）')
"
rm -f graphify-out/.graphify_detect.json graphify-out/.graphify_extract.json graphify-out/.graphify_ast.json graphify-out/.graphify_semantic.json graphify-out/.graphify_analysis.json graphify-out/.graphify_labels.json
rm -f graphify-out/.needs_update 2>/dev/null || true
```

**告知用户**（仅当使用了 `--obsidian` 时才显示 Obsidian 相关文字）：

```
图谱生成完成。所有输出位于：PATH_TO_DIR/graphify-out/

  graph.html            - 交互式图谱，可直接用浏览器打开
  GRAPH_REPORT.md       - 分析报告
  graph.json            - 原始图数据
  obsidian/             - Obsidian 仓库（仅当使用 --obsidian 参数时生成）
```

如果 graphify 为你节省了时间，欢迎支持项目：https://github.com/sponsors/safishamsi

请将 `PATH_TO_DIR` 替换为实际处理的目录的**绝对路径**。

然后从 `GRAPH_REPORT.md` 中直接粘贴以下三个部分到聊天中：
- **God Nodes**（核心节点）
- **Surprising Connections**（意外连接）
- **Suggested Questions**（建议问题）

**不要**粘贴完整报告，仅这三个部分即可，保持简洁。

最后立即主动提供探索引导。从报告的建议问题中挑选 **最有趣的一个**（通常是跨越最多社区边界或包含最惊人桥接节点的问题），询问：

> “这个图谱中最值得探索的问题是：**[问题]**。需要我帮你沿着图谱追踪解答吗？”

如果用户回答“是”，则运行 `/graphify query "[问题]"`，并使用图结构为用户逐步讲解答案（哪些节点相连、跨越了哪些社区边界、路径揭示了什么）。根据用户兴趣持续深入探索。每次回答结束时，用自然的跟进句结束，例如：“这又连接到了 X —— 想继续深入看看吗？”

**图谱是地图，你的任务是成为优秀的向导。**

---

## 子命令的解释器守护机制

在运行以下任何子命令（`--update`、`--cluster-only`、`query`、`path`、`explain`、`add`）之前，先检查 `.graphify_python` 文件是否存在。如果文件缺失（例如用户删除了 `graphify-out/` 目录），则重新解析解释器：

```bash
if [ ! -f graphify-out/.graphify_python ]; then
    GRAPHIFY_BIN=$(which graphify 2>/dev/null)
    if [ -n "$GRAPHIFY_BIN" ]; then
        PYTHON=$(head -1 "$GRAPHIFY_BIN" | tr -d '#!')
        case "$PYTHON" in *[!a-zA-Z0-9/_.-]*) PYTHON="python3" ;; esac
    else
        PYTHON="python3"
    fi
    mkdir -p graphify-out
    "$PYTHON" -c "import sys; open('graphify-out/.graphify_python', 'w').write(sys.executable)"
fi
```

## For --update（增量重新提取）

适用于自上次运行后有新增或修改文件的情况。仅重新提取发生变化的文件，可节省 token 和时间。

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.detect import detect_incremental, save_manifest
from pathlib import Path

result = detect_incremental(Path('INPUT_PATH'))
new_total = result.get('new_total', 0)
print(json.dumps(result, indent=2))
Path('graphify-out/.graphify_incremental.json').write_text(json.dumps(result))
if new_total == 0:
    print('自上次运行以来没有文件发生变化。无需更新。')
    raise SystemExit(0)
print(f'检测到 {new_total} 个新增/变更文件需要重新提取。')
"
```

如果存在新文件，首先检查所有变更文件是否均为代码文件：

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from pathlib import Path

result = json.loads(open('graphify-out/.graphify_incremental.json').read()) if Path('graphify-out/.graphify_incremental.json').exists() else {}
code_exts = {'.py','.ts','.js','.go','.rs','.java','.cpp','.c','.rb','.swift','.kt','.cs','.scala','.php','.cc','.cxx','.hpp','.h','.kts','.lua','.toc'}
new_files = result.get('new_files', {})
all_changed = [f for files in new_files.values() for f in files]
code_only = all(Path(f).suffix.lower() in code_exts for f in all_changed)
print('code_only:', code_only)
"
```

如果 `code_only` 为 True：输出 `[graphify update] 检测到仅代码变更 - 跳过语义提取（无需调用 LLM）`，仅对变更文件执行 Step 3A（AST 解析），完全跳过 Step 3B（子代理），然后直接进入合并阶段及 Steps 4–8。

如果 `code_only` 为 False（存在文档/论文/图像等非代码文件）：按正常流程执行完整的 Steps 3A–3C 管道。

然后执行：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.build import build_from_json
from graphify.export import to_json
from networkx.readwrite import json_graph
import networkx as nx
from pathlib import Path

# 加载现有图
existing_data = json.loads(Path('graphify-out/graph.json').read_text())
G_existing = json_graph.node_link_graph(existing_data, edges='links')

# 加载新提取结果
new_extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
G_new = build_from_json(new_extraction)

# 移除已删除文件的节点（防止出现幽灵节点）
incremental = json.loads(Path('graphify-out/.graphify_incremental.json').read_text())
deleted = set(incremental.get('deleted_files', []))
if deleted:
    to_remove = [n for n, d in G_existing.nodes(data=True) if d.get('source_file') in deleted]
    G_existing.remove_nodes_from(to_remove)
    print(f'已清理 {len(to_remove)} 个来自 {len(deleted)} 个已删除文件的幽灵节点')

# 合并：将新节点和边合并到现有图中
G_existing.update(G_new)
print(f'合并完成：{G_existing.number_of_nodes()} 个节点，{G_existing.number_of_edges()} 条边')

# 将合并后的结果写回 .graphify_extract.json，供 Step 4 使用完整图
merged_out = {
    'nodes': [{'id': n, **d} for n, d in G_existing.nodes(data=True)],
    'edges': [{'source': u, 'target': v, **d} for u, v, d in G_existing.edges(data=True)],
    'hyperedges': new_extraction.get('hyperedges', []),
    'input_tokens': new_extraction.get('input_tokens', 0),
    'output_tokens': new_extraction.get('output_tokens', 0),
}
Path('graphify-out/.graphify_extract.json').write_text(json.dumps(merged_out))
print(f'[graphify update] 已写入合并后的提取结果（{len(merged_out[\"nodes\"])} 个节点，{len(merged_out[\"edges\"])} 条边）')
" 
```

然后按正常流程继续执行 Steps 4–8。

在执行合并步骤之前，先备份旧图：`cp graphify-out/graph.json graphify-out/.graphify_old.json`

执行完后清理：`rm -f graphify-out/.graphify_old.json`

更新完成后，显示图的差异：

```bash
$(cat graphify-out/.graphify_python) -c "
import json
from graphify.analyze import graph_diff
from graphify.build import build_from_json
from networkx.readwrite import json_graph
import networkx as nx
from pathlib import Path

# 从备份中加载旧图（合并前）
old_data = json.loads(Path('graphify-out/.graphify_old.json').read_text()) if Path('graphify-out/.graphify_old.json').exists() else None
new_extract = json.loads(Path('graphify-out/.graphify_extract.json').read_text())
G_new = build_from_json(new_extract)

if old_data:
    G_old = json_graph.node_link_graph(old_data, edges='links')
    diff = graph_diff(G_old, G_new)
    print(diff['summary'])
    if diff['new_nodes']:
        print('新增节点：', ', '.join(n['label'] for n in diff['new_nodes'][:5]))
    if diff['new_edges']:
        print('新增边：', len(diff['new_edges']))
"
```

---

## For --cluster-only

跳过 Steps 1–3。直接从 `graphify-out/graph.json` 加载现有图，并重新运行聚类：

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections
from graphify.report import generate
from graphify.export import to_json
from networkx.readwrite import json_graph
import networkx as nx
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text())
G = json_graph.node_link_graph(data, edges='links')

detection = {'total_files': 0, 'total_words': 99999, 'needs_graph': True, 'warning': None,
             'files': {'code': [], 'document': [], 'paper': []}}
tokens = {'input': 0, 'output': 0}

communities = cluster(G)
cohesion = score_all(G, communities)
gods = god_nodes(G)
surprises = surprising_connections(G, communities)
labels = {cid: 'Community ' + str(cid) for cid in communities}

report = generate(G, communities, cohesion, labels, gods, surprises, detection, tokens, '.')
Path('graphify-out/GRAPH_REPORT.md').write_text(report)
to_json(G, communities, 'graphify-out/graph.json')

analysis = {
    'communities': {str(k): v for k, v in communities.items()},
    'cohesion': {str(k): v for k, v in cohesion.items()},
    'gods': gods,
    'surprises': surprises,
}
Path('graphify-out/.graphify_analysis.json').write_text(json.dumps(analysis, indent=2))
print(f'重新聚类完成：共 {len(communities)} 个社区')
"
```

随后按正常流程继续执行 Steps 5–9（为社区打标签、生成可视化、基准测试、清理、生成报告）。

---

## 对于 /graphify 查询

两种遍历模式 —— 根据问题选择使用：

| 模式 | 标志 | 最适用于 |
|------|------|----------|
| BFS（默认） | _(无)_ | “X 连接到什么？”—— 宽泛上下文，优先探索最近邻居 |
| DFS | `--dfs` | “X 如何到达 Y？”—— 追踪特定链条或依赖路径 |

首先检查图是否存在：
```bash
$(cat graphify-out/.graphify_python) -c "
from pathlib import Path
if not Path('graphify-out/graph.json').exists():
    print('错误：未找到图。请先运行 /graphify <path> 来构建图。')
    raise SystemExit(1)
"
```
如果检查失败，请停止并告知用户先运行 `/graphify <path>`。

加载 `graphify-out/graph.json`，然后执行以下步骤：

1. 找到 1-3 个标签最匹配问题中关键术语的节点。
2. 从每个起始节点运行相应的遍历算法。
3. 读取子图 —— 包含节点标签、边关系、置信度标签、源位置。
4. **仅使用图中包含的信息**进行回答。引用具体事实时请注明 `source_location`。
5. 如果图中信息不足，请明确说明 —— 不要虚构边。

```bash
$(cat graphify-out/.graphify_python) -c "
import sys, json
from networkx.readwrite import json_graph
import networkx as nx
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text())
G = json_graph.node_link_graph(data, edges='links')

question = 'QUESTION'
mode = 'MODE'  # 'bfs' 或 'dfs'
terms = [t.lower() for t in question.split() if len(t) > 3]

# 寻找最佳匹配的起始节点
scored = []
for nid, ndata in G.nodes(data=True):
    label = ndata.get('label', '').lower()
    score = sum(1 for t in terms if t in label)
    if score > 0:
        scored.append((score, nid))
scored.sort(reverse=True)
start_nodes = [nid for _, nid in scored[:3]]

if not start_nodes:
    print('未找到与查询词匹配的节点：', terms)
    sys.exit(0)

subgraph_nodes = set()
subgraph_edges = []

if mode == 'dfs':
    # DFS：尽可能沿一条路径深入，再回溯。
    # 深度限制为 6 以避免遍历整个图。
    visited = set()
    stack = [(n, 0) for n in reversed(start_nodes)]
    while stack:
        node, depth = stack.pop()
        if node in visited or depth > 6:
            continue
        visited.add(node)
        subgraph_nodes.add(node)
        for neighbor in G.neighbors(node):
            if neighbor not in visited:
                stack.append((neighbor, depth + 1))
                subgraph_edges.append((node, neighbor))
else:
    # BFS：逐层探索邻居，最多 3 层。
    frontier = set(start_nodes)
    subgraph_nodes = set(start_nodes)
    for _ in range(3):
        next_frontier = set()
        for n in frontier:
            for neighbor in G.neighbors(n):
                if neighbor not in subgraph_nodes:
                    next_frontier.add(neighbor)
                    subgraph_edges.append((n, neighbor))
        subgraph_nodes.update(next_frontier)
        frontier = next_frontier

# 考虑 token 预算的输出：按相关性排序，控制输出长度
token_budget = BUDGET  # 默认 2000
char_budget = token_budget * 4

# 根据词重叠度对节点进行相关性评分
def relevance(nid):
    label = G.nodes[nid].get('label', '').lower()
    return sum(1 for t in terms if t in label)

ranked_nodes = sorted(subgraph_nodes, key=relevance, reverse=True)

lines = [f'Traversal: {mode.upper()} | Start: {[G.nodes[n].get(\"label\",n) for n in start_nodes]} | {len(subgraph_nodes)} nodes']
for nid in ranked_nodes:
    d = G.nodes[nid]
    lines.append(f'  NODE {d.get(\"label\", nid)} [src={d.get(\"source_file\",\"\")} loc={d.get(\"source_location\",\"\")}]')
for u, v in subgraph_edges:
    if u in subgraph_nodes and v in subgraph_nodes:
        d = G.edges[u, v]
        lines.append(f'  EDGE {G.nodes[u].get(\"label\",u)} --{d.get(\"relation\",\"\")} [{d.get(\"confidence\",\"\")}]--> {G.nodes[v].get(\"label\",v)}')

output = '\n'.join(lines)
if len(output) > char_budget:
    output = output[:char_budget] + f'\n... (已按 ~{token_budget} token 预算截断 - 使用 --budget N 获取更多内容)'
print(output)
"
```

将 `QUESTION` 替换为用户的实际问题，`MODE` 替换为 `bfs` 或 `dfs`，`BUDGET` 替换为 token 预算（默认为 `2000`，或用户通过 `--budget N` 指定的值）。然后基于上方输出的子图信息撰写回答。

回答完成后，将其保存回图中，以便提升未来查询效果：

```bash
$(cat graphify-out/.graphify_python) -m graphify save-result --question "QUESTION" --answer "ANSWER" --type query --nodes NODE1 NODE2
```

将 `QUESTION` 替换为问题，`ANSWER` 替换为你的完整回答文本，`SOURCE_NODES` 替换为你引用的节点标签列表。此操作形成反馈闭环：下次执行 `--update` 时，此问答将被提取为图中的新节点。

---

## 对于 /graphify path

查找图中两个指定概念之间的最短路径。

首先检查图是否存在：
```bash
$(cat graphify-out/.graphify_python) -c "
from pathlib import Path
if not Path('graphify-out/graph.json').exists():
    print('错误：未找到图。请先运行 /graphify <path> 来构建图。')
    raise SystemExit(1)
"
```
如果失败，停止并告知用户先运行 `/graphify <path>`。

```bash
$(cat graphify-out/.graphify_python) -c "
import json, sys
import networkx as nx
from networkx.readwrite import json_graph
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text())
G = json_graph.node_link_graph(data, edges='links')

a_term = 'NODE_A'
b_term = 'NODE_B'

def find_node(term):
    term = term.lower()
    scored = sorted(
        [(sum(1 for w in term.split() if w in G.nodes[n].get('label','').lower()), n)
         for n in G.nodes()],
        reverse=True
    )
    return scored[0][1] if scored and scored[0][0] > 0 else None

src = find_node(a_term)
tgt = find_node(b_term)

if not src or not tgt:
    print(f'无法找到匹配以下术语的节点：{a_term!r} 或 {b_term!r}')
    sys.exit(0)

try:
    path = nx.shortest_path(G, src, tgt)
    print(f'最短路径（{len(path)-1} 跳）：')
    for i, nid in enumerate(path):
        label = G.nodes[nid].get('label', nid)
        if i < len(path) - 1:
            edge = G.edges[nid, path[i+1]]
            rel = edge.get('relation', '')
            conf = edge.get('confidence', '')
            print(f'  {label} --{rel}--> [{conf}]')
        else:
            print(f'  {label}')
except nx.NetworkXNoPath:
    print(f'在 {a_term!r} 和 {b_term!r} 之间未找到路径')
except nx.NodeNotFound as e:
    print(f'节点未找到：{e}')
"
```

将 `NODE_A` 和 `NODE_B` 替换为用户提供的实际概念名称。然后用通俗语言解释这条路径 —— 说明每一跳的含义及其重要性。

解释完成后，将其保存回图中：

```bash
$(cat graphify-out/.graphify_python) -m graphify save-result --question "Path from NODE_A to NODE_B" --answer "ANSWER" --type path_query --nodes NODE_A NODE_B
```

---

## 对于 /graphify explain

用通俗语言解释单个节点 —— 包括与其相连的所有内容。

首先检查图是否存在（同上，略）。

```bash
$(cat graphify-out/.graphify_python) -c "
import json, sys
import networkx as nx
from networkx.readwrite import json_graph
from pathlib import Path

data = json.loads(Path('graphify-out/graph.json').read_text())
G = json_graph.node_link_graph(data, edges='links')

term = 'NODE_NAME'
term_lower = term.lower()

# 寻找最佳匹配节点
scored = sorted(
    [(sum(1 for w in term_lower.split() if w in G.nodes[n].get('label','').lower()), n)
     for n in G.nodes()],
    reverse=True
)
if not scored or scored[0][0] == 0:
    print(f'未找到匹配 {term!r} 的节点')
    sys.exit(0)

nid = scored[0][1]
data_n = G.nodes[nid]
print(f'NODE: {data_n.get(\"label\", nid)}')
print(f'  source: {data_n.get(\"source_file\",\"unknown\")}')
print(f'  type: {data_n.get(\"file_type\",\"unknown\")}')
print(f'  degree: {G.degree(nid)}')
print()
print('CONNECTIONS:')
for neighbor in G.neighbors(nid):
    edge = G.edges[nid, neighbor]
    nlabel = G.nodes[neighbor].get('label', neighbor)
    rel = edge.get('relation', '')
    conf = edge.get('confidence', '')
    src_file = G.nodes[neighbor].get('source_file', '')
    print(f'  --{rel}--> {nlabel} [{conf}] ({src_file})')
"
```

将 `NODE_NAME` 替换为用户询问的概念名称。然后用 3-5 句话解释该节点是什么、它连接到哪些内容，以及这些连接为什么重要。引用源位置作为依据。

解释完成后保存：

```bash
$(cat graphify-out/.graphify_python) -m graphify save-result --question "Explain NODE_NAME" --answer "ANSWER" --type explain --nodes NODE_NAME
```

---

## 对于 /graphify add

获取 URL 并将其添加到语料库，随后更新图谱。

```bash
$(cat graphify-out/.graphify_python) -c "
import sys
from graphify.ingest import ingest
from pathlib import Path

try:
    out = ingest('URL', Path('./raw'), author='AUTHOR', contributor='CONTRIBUTOR')
    print(f'已保存至 {out}')
except ValueError as e:
    print(f'错误: {e}', file=sys.stderr)
    sys.exit(1)
except RuntimeError as e:
    print(f'错误: {e}', file=sys.stderr)
    sys.exit(1)
"
```

将 `URL` 替换为实际链接，`AUTHOR` 和 `CONTRIBUTOR` 替换为提供的作者/贡献者名称（若有）。若命令执行出错，请明确告知用户错误原因，不要静默继续。添加成功后，自动对 `./raw` 目录执行 `--update` 流水线，将新文件合并到现有图中。

支持的 URL 类型（自动识别）：
- YouTube / 任意视频 URL → 通过 yt-dlp 下载音频，下次运行时转录为 `.txt`（需安装 `pip install 'graphify[video]'`）
- Twitter/X → 通过 oEmbed 获取，保存为带文本和作者的 `.md` 文件
- arXiv → 保存摘要与元数据为 `.md`
- PDF → 下载为 `.pdf`
- 图片 (.png/.jpg/.webp) → 下载后，下次运行时通过 Claude Vision 提取内容
- 任意网页 → 通过 html2text 转换为 Markdown

---

## 对于 --watch

启动后台监视器，监控文件夹并在文件变化时自动更新图谱。

```bash
python3 -m graphify.watch INPUT_PATH --debounce 3
```

将 `INPUT_PATH` 替换为要监视的文件夹路径。行为取决于变更类型：

- **仅代码文件 (.py, .ts, .go 等)：** 立即重新运行 AST 提取 + 重建 + 聚类，无需 LLM。`graph.json` 和 `GRAPH_REPORT.md` 会自动更新。
- **文档、论文或图片：** 写入 `graphify-out/needs_update` 标记，并提示用户运行 `/graphify --update`（需要 LLM 语义重新提取）。

防抖动（默认 3 秒）：等待文件活动停止后再触发，避免多个并行写入时反复重建。

按 Ctrl+C 停止监视。

适用于智能体工作流：在后台终端运行 `--watch`。代码变更可被自动捕获。若智能体同时写入文档或笔记，则需在波次结束后手动运行 `/graphify --update`。

---

## 对于 git commit 钩子

安装提交后钩子，在每次 commit 后自动重建图谱。无需后台进程。

```bash
graphify hook install    # 安装
graphify hook uninstall  # 卸载
graphify hook status     # 检查状态
```

每次 `git commit` 后，钩子会检测变更的代码文件（通过 `git diff HEAD~1`），仅对这些文件重新运行 AST 提取并重建 `graph.json` 和 `GRAPH_REPORT.md`。文档和图片变更会被钩子忽略 —— 需手动运行 `/graphify --update`。

若已存在 post-commit 钩子，graphify 会追加内容而非覆盖。

---

## 对于原生 CLAUDE.md 集成

在项目中运行一次，使 graphify 在 Claude Code 会话中始终可用：

```bash
graphify claude install
```

此命令会在本地 `CLAUDE.md` 中写入 `## graphify` 部分，指示 Claude 在回答代码库问题前先查图，并在代码变更后重建图谱。后续会话无需手动调用 `/graphify`。

```bash
graphify claude uninstall  # 移除该部分
```

---

## 诚实规则（Honesty Rules）

- 绝不虚构边。如果不确定，请使用 AMBIGUOUS。
- 绝不跳过语料库检查警告。
- 始终在报告中显示 token 消耗。
- 绝不将内聚分数隐藏在符号后面 —— 必须显示原始数值。
- 图中节点超过 5,000 个时，运行 HTML 可视化前必须警告用户。
