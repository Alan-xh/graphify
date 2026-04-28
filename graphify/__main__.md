这是一个名为 **graphify** 的命令行工具，核心功能是将代码库或文件夹转换为**可导航的知识图谱**，帮助 AI 编程助手更好地理解代码架构。

## 主要功能模块

### 1. **AI 平台集成安装** (`graphify install`)
支持为十几种 AI 编程助手安装技能文件，包括：
- Claude Code
- Gemini CLI  
- Cursor
- VS Code Copilot
- GitHub Copilot CLI
- OpenCode / Codex
- Aider / Kiro / Trae
- Google Antigravity

安装时自动：
- 复制技能文件到平台特定目录（如 `~/.claude/skills/graphify/`）
- 配置钩子（hooks）让 AI 在读取文件前先查看知识图谱
- 写入平台配置文件（如 `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`）

### 2. **知识图谱操作**

| 命令 | 功能 |
|------|------|
| `graphify update .` | 重新提取代码文件，更新图谱（纯 AST 分析，无 API 成本）|
| `graphify query "问题"` | 在图谱上执行 BFS/DFS 搜索，返回相关子图上下文 |
| `graphify path "源节点" "目标节点"` | 查找两个节点之间的最短路径 |
| `graphify explain "节点名"` | 解释某个节点的详情和连接关系 |
| `graphify cluster-only .` | 仅重新聚类现有图谱，重新生成报告 |

### 3. **外部内容导入**
`graphify add <URL> [--author Name] [--contributor Name]` - 抓取网页并添加到知识库

### 4. **文件监听**
`graphify watch <path>` - 监听文件夹变化，自动重建图谱

### 5. **基准测试**
`graphify benchmark` - 测量图谱相比全量文件搜索的 token 节省效果

## 核心设计思路

1. **降低 AI 成本**：用结构化图谱替代全文件扫描，AI 只需读图谱报告而非数千行代码
2. **多平台适配**：为每个 AI 工具配置对应的技能格式和钩子机制
3. **版本管理**：在每个安装目录留下 `.graphify_version` 文件，防止过期技能被使用
4. **图谱分析**：社区检测、中心节点识别、意外连接发现等

## 典型工作流

```bash
# 首次安装到 AI 助手
graphify install --platform=claude

# 构建代码知识图谱（在 AI 对话中输入 /graphify .）

# 代码修改后快速更新（无需 LLM）
graphify update .

# 查询图谱中的依赖关系
graphify query "数据库连接在哪里初始化"
```

这个工具本质上是为 AI 编程助手提供一个**结构化的代码理解层**，让 AI 能够像人类开发者一样先看架构概览，再深入细节。