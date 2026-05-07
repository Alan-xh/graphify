# file discovery, type classification, and corpus health checks
from __future__ import annotations
import fnmatch
import json
import os
import re
from enum import Enum
from pathlib import Path


class FileType(str, Enum):
    """文件类型枚举，定义系统支持的所有文件分类。"""
    CODE = "code"
    DOCUMENT = "document"
    PAPER = "paper"
    IMAGE = "image"
    VIDEO = "video"


_MANIFEST_PATH = "graphify-out/manifest.json"

CODE_EXTENSIONS = {'.py', '.ts', '.js', '.jsx', '.tsx', '.mjs', '.ejs', '.go', '.rs', '.java', '.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.rb', '.swift', '.kt', '.kts', '.cs', '.scala', '.php', '.lua', '.toc', '.zig', '.ps1', '.ex', '.exs', '.m', '.mm', '.jl', '.vue', '.svelte', '.dart', '.v', '.sv'}
DOC_EXTENSIONS = {'.md', '.mdx', '.txt', '.rst', '.html'}
PAPER_EXTENSIONS = {'.pdf'}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}
OFFICE_EXTENSIONS = {'.docx', '.xlsx'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v', '.mp3', '.wav', '.m4a', '.ogg'}

CORPUS_WARN_THRESHOLD = 50_000    # words - below this, warn "you may not need a graph"
CORPUS_UPPER_THRESHOLD = 500_000  # words - above this, warn about token cost
FILE_COUNT_UPPER = 200             # files - above this, warn about token cost

# Files that may contain secrets - skip silently
_SENSITIVE_PATTERNS = [
    re.compile(r'(^|[\\/])\.(env|envrc)(\.|$)', re.IGNORECASE),
    re.compile(r'\.(pem|key|p12|pfx|cert|crt|der|p8)$', re.IGNORECASE),
    re.compile(r'(credential|secret|passwd|password|token|private_key)', re.IGNORECASE),
    re.compile(r'(id_rsa|id_dsa|id_ecdsa|id_ed25519)(\.pub)?$'),
    re.compile(r'(\.netrc|\.pgpass|\.htpasswd)$', re.IGNORECASE),
    re.compile(r'(aws_credentials|gcloud_credentials|service.account)', re.IGNORECASE),
]

# Signals that a .md/.txt file is actually a converted academic paper
_PAPER_SIGNALS = [
    re.compile(r'\barxiv\b', re.IGNORECASE),
    re.compile(r'\bdoi\s*:', re.IGNORECASE),
    re.compile(r'\babstract\b', re.IGNORECASE),
    re.compile(r'\bproceedings\b', re.IGNORECASE),
    re.compile(r'\bjournal\b', re.IGNORECASE),
    re.compile(r'\bpreprint\b', re.IGNORECASE),
    re.compile(r'\\cite\{'),          # LaTeX citation
    re.compile(r'\[\d+\]'),           # Numbered citation [1], [23] (inline)
    re.compile(r'\[\n\d+\n\]'),       # Numbered citation spread across lines (markdown conversion)
    re.compile(r'eq\.\s*\d+|equation\s+\d+', re.IGNORECASE),
    re.compile(r'\d{4}\.\d{4,5}'),   # arXiv ID like 1706.03762
    re.compile(r'\bwe propose\b', re.IGNORECASE),   # common academic phrasing
    re.compile(r'\bliterature\b', re.IGNORECASE),   # "from the literature"
]
_PAPER_SIGNAL_THRESHOLD = 3  # need at least this many signals to call it a paper


def _is_sensitive(path: Path) -> bool:
    """判断文件是否可能包含敏感信息（密钥、密码等）。
    
    根据预定义的敏感模式列表检查文件名和完整路径，用于在扫描时跳过这些文件，
    避免意外泄露敏感信息。
    
    Args:
        path: 要检查的文件路径对象。
        
    Returns:
        bool: 如果文件可能包含敏感信息返回 True，否则返回 False。
        
    Examples:
        >>> _is_sensitive(Path(".env"))
        True
        >>> _is_sensitive(Path("id_rsa"))
        True
        >>> _is_sensitive(Path("normal_file.txt"))
        False
    """
    name = path.name
    full = str(path)
    return any(p.search(name) or p.search(full) for p in _SENSITIVE_PATTERNS)


def _looks_like_paper(path: Path) -> bool:
    """启发式判断文本文件是否看起来像学术论文。
    
    通过扫描文件前3000个字符，匹配预设的学术论文特征模式（如arXiv、DOI、
    引用格式、学术用语等）。当匹配数量达到阈值时，判定为论文。
    
    Args:
        path: 要检查的文本文件路径。
        
    Returns:
        bool: 如果文件内容符合学术论文特征返回 True，否则返回 False。
        
    Note:
        只读取文件的前3000个字符以平衡性能和准确性。
        遇到读取异常时返回 False。
        
    Examples:
        >>> _looks_like_paper(Path("paper_with_arxiv_id.md"))
        True  # 如果包含"1706.03762"等特征
    """
    try:
        # Only scan first 3000 chars for speed
        text = path.read_text(encoding="utf-8", errors="ignore")[:3000]
        hits = sum(1 for pattern in _PAPER_SIGNALS if pattern.search(text))
        return hits >= _PAPER_SIGNAL_THRESHOLD
    except Exception:
        return False


_ASSET_DIR_MARKERS = {".imageset", ".xcassets", ".appiconset", ".colorset", ".launchimage"}


def classify_file(path: Path) -> FileType | None:
    """根据文件扩展名和内容特征分类文件类型。
    
    优先级顺序：
    1. 检查特殊复合扩展名（如 .blade.php）
    2. 根据扩展名匹配代码、论文、图片、文档、Office、视频类型
    3. 对文档类型额外进行论文特征检测
    4. 特殊处理 Xcode 资源目录中的 PDF（作为图标而非论文）
    
    Args:
        path: 要分类的文件路径。
        
    Returns:
        FileType | None: 识别到的文件类型，如果无法识别或应跳过则返回 None。
        
    Examples:
        >>> classify_file(Path("script.py"))
        <FileType.CODE: 'code'>
        >>> classify_file(Path("readme.md"))
        <FileType.DOCUMENT: 'document'>
        >>> classify_file(Path("paper.pdf"))
        <FileType.PAPER: 'paper'>
    """
    # Compound extensions must be checked before simple suffix lookup
    if path.name.lower().endswith(".blade.php"):
        return FileType.CODE
    ext = path.suffix.lower()
    if ext in CODE_EXTENSIONS:
        return FileType.CODE
    if ext in PAPER_EXTENSIONS:
        # PDFs inside Xcode asset catalogs are vector icons, not papers
        if any(part.endswith(tuple(_ASSET_DIR_MARKERS)) for part in path.parts):
            return None
        return FileType.PAPER
    if ext in IMAGE_EXTENSIONS:
        return FileType.IMAGE
    if ext in DOC_EXTENSIONS:
        # Check if it's a converted paper
        if _looks_like_paper(path):
            return FileType.PAPER
        return FileType.DOCUMENT
    if ext in OFFICE_EXTENSIONS:
        return FileType.DOCUMENT
    if ext in VIDEO_EXTENSIONS:
        return FileType.VIDEO
    return None


def extract_pdf_text(path: Path) -> str:
    """从 PDF 文件中提取纯文本内容。
    
    使用 pypdf 库读取 PDF 文件的所有页面，并提取文本内容。
    
    Args:
        path: PDF 文件路径。
        
    Returns:
        str: 提取的纯文本内容，所有页面用换行符连接。
             如果提取失败（缺少依赖、文件损坏等）返回空字符串。
             
    Note:
        需要安装 pypdf 库：pip install pypdf
        
    Examples:
        >>> text = extract_pdf_text(Path("document.pdf"))
        >>> print(len(text))
        12345
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n".join(pages)
    except Exception:
        return ""


def docx_to_markdown(path: Path) -> str:
    """将 .docx 文件转换为 Markdown 格式文本。
    
    使用 python-docx 库解析 Word 文档，将段落、标题、列表和表格转换为
    对应的 Markdown 语法。
    
    Args:
        path: .docx 文件路径。
        
    Returns:
        str: Markdown 格式的文本内容。
             如果转换失败（缺少依赖、文件损坏等）返回空字符串。
             
    Note:
        需要安装 python-docx 库：pip install python-docx
        
        转换规则：
        - Heading 1 -> # 标题
        - Heading 2 -> ## 标题
        - Heading 3 -> ### 标题
        - List -> - 列表项
        - Tables -> Markdown 表格格式
        
    Examples:
        >>> md = docx_to_markdown(Path("report.docx"))
        >>> print(md[:50])
        # Title
    
    ## Section 1
    """
    try:
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(str(path))
        lines = []
        for para in doc.paragraphs:
            style = para.style.name if para.style else ""
            text = para.text.strip()
            if not text:
                lines.append("")
                continue
            if style.startswith("Heading 1"):
                lines.append(f"# {text}")
            elif style.startswith("Heading 2"):
                lines.append(f"## {text}")
            elif style.startswith("Heading 3"):
                lines.append(f"### {text}")
            elif style.startswith("List"):
                lines.append(f"- {text}")
            else:
                lines.append(text)
        # Tables
        for table in doc.tables:
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            header = "| " + " | ".join(rows[0]) + " |"
            sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
            lines.extend([header, sep])
            for row in rows[1:]:
                lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)
    except ImportError:
        return ""
    except Exception:
        return ""


def xlsx_to_markdown(path: Path) -> str:
    """将 .xlsx 文件转换为 Markdown 格式文本。
    
    使用 openpyxl 库解析 Excel 工作簿，将每个工作表转换为 Markdown 表格格式。
    
    Args:
        path: .xlsx 文件路径。
        
    Returns:
        str: Markdown 格式的文本内容，包含工作表名称作为二级标题。
             如果转换失败（缺少依赖、文件损坏等）返回空字符串。
             
    Note:
        需要安装 openpyxl 库：pip install openpyxl
        
        转换规则：
        - 工作表名 -> ## 标题
        - 数据行 -> Markdown 表格格式
        - 跳过全空行
        
    Examples:
        >>> md = xlsx_to_markdown(Path("data.xlsx"))
        >>> print(md[:100])
        ## Sheet: Sheet1
        | Name | Age |
        | --- | --- |
        | Alice | 25 |
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        sections = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                # Skip entirely empty rows
                if all(cell is None for cell in row):
                    continue
                rows.append([str(cell) if cell is not None else "" for cell in row])
            if not rows:
                continue
            sections.append(f"## Sheet: {sheet_name}")
            if len(rows) >= 1:
                header = "| " + " | ".join(rows[0]) + " |"
                sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
                sections.extend([header, sep])
                for row in rows[1:]:
                    sections.append("| " + " | ".join(row) + " |")
        wb.close()
        return "\n".join(sections)
    except ImportError:
        return ""
    except Exception:
        return ""


def convert_office_file(path: Path, out_dir: Path) -> Path | None:
    """将 Office 文件（.docx 或 .xlsx）转换为 Markdown 侧车文件。
    
    在输出目录中生成转换后的 .md 文件，文件名基于原文件路径的哈希值
    以确保唯一性。
    
    Args:
        path: Office 文件路径（.docx 或 .xlsx）。
        out_dir: 输出目录路径，用于存放转换后的 Markdown 文件。
        
    Returns:
        Path | None: 生成的 .md 文件路径，如果转换失败或库未安装则返回 None。
        
    Note:
        生成的文件会在开头添加注释标记原始文件名：
        <!-- converted from original.docx -->
        
    Examples:
        >>> out = convert_office_file(Path("report.docx"), Path("converted"))
        >>> print(out)
        converted/report_a3f2b1c4.md
    """
    ext = path.suffix.lower()
    if ext == ".docx":
        text = docx_to_markdown(path)
    elif ext == ".xlsx":
        text = xlsx_to_markdown(path)
    else:
        return None

    if not text.strip():
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    # Use a stable name derived from the original path to avoid collisions
    import hashlib
    name_hash = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]
    out_path = out_dir / f"{path.stem}_{name_hash}.md"
    out_path.write_text(
        f"<!-- converted from {path.name} -->\n\n{text}",
        encoding="utf-8",
    )
    return out_path


def count_words(path: Path) -> int:
    """统计文件中的词数。
    
    根据文件类型采用不同的提取策略：
    - PDF: 使用 pypdf 提取文本后分词
    - DOCX: 转换为 Markdown 后分词
    - XLSX: 转换为 Markdown 后分词
    - 其他文本文件: 直接读取并分词
    
    Args:
        path: 要统计的文件路径。
        
    Returns:
        int: 文件中的词数（按空格分割计算）。如果处理失败返回 0。
        
    Examples:
        >>> count_words(Path("short.txt"))
        150
        >>> count_words(Path("big_report.pdf"))
        50000
    """
    try:
        ext = path.suffix.lower()
        if ext == ".pdf":
            return len(extract_pdf_text(path).split())
        if ext == ".docx":
            return len(docx_to_markdown(path).split())
        if ext == ".xlsx":
            return len(xlsx_to_markdown(path).split())
        return len(path.read_text(encoding="utf-8", errors="ignore").split())
    except Exception:
        return 0


# Directory names to always skip - venvs, caches, build artifacts, deps
_SKIP_DIRS = {
    "venv", ".venv", "env", ".env",
    "node_modules", "__pycache__", ".git",
    "dist", "build", "target", "out",
    "site-packages", "lib64",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".eggs", "*.egg-info",
}

# Large generated files that are never useful to extract
_SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "Gemfile.lock",
    "composer.lock", "go.sum", "go.work.sum",
}

def _is_noise_dir(part: str) -> bool:
    """判断目录名是否为噪音目录（虚拟环境、缓存、构建产物等）。
    
    根据预定义的跳过目录集合进行精确匹配，并支持通配模式匹配。
    
    Args:
        part: 目录名称字符串。
        
    Returns:
        bool: 如果是噪音目录返回 True，否则返回 False。
        
    Examples:
        >>> _is_noise_dir("venv")
        True
        >>> _is_noise_dir("my_venv")
        True
        >>> _is_noise_dir("src")
        False
    """
    if part in _SKIP_DIRS:
        return True
    # Catch *_venv, *_repo/site-packages patterns
    if part.endswith("_venv") or part.endswith("_env"):
        return True
    if part.endswith(".egg-info"):
        return True
    return False


def _load_graphifyignore(root: Path) -> list[tuple[Path, str]]:
    """从根目录及其祖先目录加载 .graphifyignore 规则。
    
    递归向上查找直到 .git 边界或文件系统根目录，收集所有找到的忽略文件。
    
    Args:
        root: 扫描的根目录路径。
        
    Returns:
        list[tuple[Path, str]]: 规则列表，每个元素为 (规则所在目录, 模式字符串)。
        
    Note:
        - 空行和以 # 开头的行会被忽略
        - 支持在父目录定义规则，对子目录扫描同样生效
        - 遇到 .git 目录时停止向上查找
        
    Examples:
        >>> rules = _load_graphifyignore(Path("/project/src"))
        >>> len(rules)
        2  # 可能包括 /project/.graphifyignore 和 /project/src/.graphifyignore
    """
    patterns: list[tuple[Path, str]] = []
    current = root.resolve()
    while True:
        ignore_file = current / ".graphifyignore"
        if ignore_file.exists():
            for line in ignore_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append((current, line))
        # Stop climbing once we've processed the git repo root
        if (current / ".git").exists():
            break
        parent = current.parent
        if parent == current:
            break  # filesystem root
        current = parent
    return patterns


def _is_ignored(path: Path, root: Path, patterns: list[tuple[Path, str]]) -> bool:
    """判断文件路径是否匹配任何 .graphifyignore 模式。
    
    支持多种匹配方式：
    - 完整相对路径匹配
    - 文件名匹配
    - 路径中的任一部分匹配
    - 相对于扫描根目录和规则所在目录的路径匹配
    
    Args:
        path: 要检查的文件路径。
        root: 扫描的根目录。
        patterns: 从 _load_graphifyignore 加载的规则列表。
        
    Returns:
        bool: 如果路径匹配任何忽略模式则返回 True，否则返回 False。
        
    Examples:
        >>> patterns = [(Path("/project"), "*.log")]
        >>> _is_ignored(Path("/project/logs/app.log"), Path("/project"), patterns)
        True
    """
    if not patterns:
        return False

    def _matches(rel: str, p: str) -> bool:
        parts = rel.split("/")
        if fnmatch.fnmatch(rel, p):
            return True
        if fnmatch.fnmatch(path.name, p):
            return True
        for i, part in enumerate(parts):
            if fnmatch.fnmatch(part, p):
                return True
            if fnmatch.fnmatch("/".join(parts[:i + 1]), p):
                return True
        return False

    for anchor, pattern in patterns:
        p = pattern.strip("/")
        if not p:
            continue
        # Try path relative to the scan root
        try:
            rel = str(path.relative_to(root)).replace(os.sep, "/")
            if _matches(rel, p):
                return True
        except ValueError:
            pass
        # Also try relative to the anchor dir (the .graphifyignore's location),
        # so patterns written at a parent level still fire when running on a subfolder
        if anchor != root:
            try:
                rel_anchor = str(path.relative_to(anchor)).replace(os.sep, "/")
                if _matches(rel_anchor, p):
                    return True
            except ValueError:
                pass
    return False


def detect(root: Path, *, follow_symlinks: bool = False) -> dict:
    """扫描目录，发现并分类所有文件，执行语料库健康检查。
    
    这是模块的核心函数，执行完整的文件发现流程：
    1. 加载 .graphifyignore 规则
    2. 递归扫描目录（自动跳过噪音目录）
    3. 分类每个文件
    4. 转换 Office 文档
    5. 统计词数和文件数
    6. 生成健康检查报告
    
    Args:
        root: 要扫描的根目录路径。
        follow_symlinks: 是否跟随符号链接。默认为 False。
        
    Returns:
        dict: 包含以下字段的字典：
            - files: 按类型分类的文件路径列表
            - total_files: 总文件数
            - total_words: 总词数
            - needs_graph: 是否建议使用图结构（词数 >= 50,000）
            - warning: 健康警告信息（可能为 None）
            - skipped_sensitive: 跳过的敏感文件列表
            - graphifyignore_patterns: 加载的忽略规则数量
            
    Note:
        特殊处理：
        - 始终包含 graphify-out/memory/ 目录（查询结果）
        - 自动跳过敏感文件、噪音目录、忽略规则文件
        - Office 文件转换为 Markdown 存储在 graphify-out/converted/
        
    Examples:
        >>> result = detect(Path("./my_project"))
        >>> print(f"Found {result['total_files']} files")
        >>> if result['warning']:
        ...     print(f"Warning: {result['warning']}")
    """
    files: dict[FileType, list[str]] = {
        FileType.CODE: [],
        FileType.DOCUMENT: [],
        FileType.PAPER: [],
        FileType.IMAGE: [],
        FileType.VIDEO: [],
    }
    total_words = 0

    skipped_sensitive: list[str] = []
    ignore_patterns = _load_graphifyignore(root)

    # Always include graphify-out/memory/ - query results filed back into the graph
    memory_dir = root / "graphify-out" / "memory"
    scan_paths = [root]
    if memory_dir.exists():
        scan_paths.append(memory_dir)

    seen: set[Path] = set()
    all_files: list[Path] = []

    for scan_root in scan_paths:
        in_memory_tree = memory_dir.exists() and str(scan_root).startswith(str(memory_dir))
        for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=follow_symlinks):
            dp = Path(dirpath)
            if follow_symlinks and os.path.islink(dirpath):
                real = os.path.realpath(dirpath)
                parent_real = os.path.realpath(os.path.dirname(dirpath))
                if parent_real == real or parent_real.startswith(real + os.sep):
                    dirnames.clear()
                    continue
            if not in_memory_tree:
                # Prune noise dirs in-place so os.walk never descends into them
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and not _is_noise_dir(d)
                    and not _is_ignored(dp / d, root, ignore_patterns)
                ]
            for fname in filenames:
                if fname in _SKIP_FILES:
                    continue
                p = dp / fname
                if p not in seen:
                    seen.add(p)
                    all_files.append(p)

    converted_dir = root / "graphify-out" / "converted"

    for p in all_files:
        # For memory dir files, skip hidden/noise filtering
        in_memory = memory_dir.exists() and str(p).startswith(str(memory_dir))
        if not in_memory:
            # Hidden files are already excluded via dir pruning above,
            # but catch hidden files at the root level
            if p.name.startswith("."):
                continue
            # Skip files inside our own converted/ dir (avoid re-processing sidecars)
            if str(p).startswith(str(converted_dir)):
                continue
        if _is_ignored(p, root, ignore_patterns):
            continue
        if _is_sensitive(p):
            skipped_sensitive.append(str(p))
            continue
        ftype = classify_file(p)
        if ftype:
            # Office files: convert to markdown sidecar so subagents can read them
            if p.suffix.lower() in OFFICE_EXTENSIONS:
                md_path = convert_office_file(p, converted_dir)
                if md_path:
                    files[ftype].append(str(md_path))
                    total_words += count_words(md_path)
                else:
                    # Conversion failed (library not installed) - skip with note
                    skipped_sensitive.append(str(p) + " [office conversion failed - pip install graphifyy[office]]")
                continue
            files[ftype].append(str(p))
            if ftype != FileType.VIDEO:
                total_words += count_words(p)

    total_files = sum(len(v) for v in files.values())
    needs_graph = total_words >= CORPUS_WARN_THRESHOLD

    # Determine warning - lower bound, upper bound, or sensitive files skipped
    warning: str | None = None
    if not needs_graph:
        warning = (
            f"Corpus is ~{total_words:,} words - fits in a single context window. "
            f"You may not need a graph."
        )
    elif total_words >= CORPUS_UPPER_THRESHOLD or total_files >= FILE_COUNT_UPPER:
        warning = (
            f"Large corpus: {total_files} files · ~{total_words:,} words. "
            f"Semantic extraction will be expensive (many Claude tokens). "
            f"Consider running on a subfolder, or use --no-semantic to run AST-only."
        )

    return {
        "files": {k.value: v for k, v in files.items()},
        "total_files": total_files,
        "total_words": total_words,
        "needs_graph": needs_graph,
        "warning": warning,
        "skipped_sensitive": skipped_sensitive,
        "graphifyignore_patterns": len(ignore_patterns),
    }


def load_manifest(manifest_path: str = _MANIFEST_PATH) -> dict[str, float]:
    """加载先前运行的文件修改时间清单。
    
    从 JSON 文件中读取文件路径到修改时间的映射。
    
    Args:
        manifest_path: 清单文件路径。默认使用 _MANIFEST_PATH。
        
    Returns:
        dict[str, float]: 文件路径到修改时间戳的映射。
                          如果文件不存在或解析失败，返回空字典。
                          
    Examples:
        >>> manifest = load_manifest()
        >>> if "src/main.py" in manifest:
        ...     print(f"Last modified: {manifest['src/main.py']}")
    """
    try:
        return json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(files: dict[str, list[str]], manifest_path: str = _MANIFEST_PATH) -> None:
    """保存当前文件的修改时间到清单，用于后续增量更新。
    
    遍历所有文件，记录每个文件的最后修改时间（st_mtime）。
    
    Args:
        files: 按类型分类的文件字典，键为文件类型，值为文件路径列表。
        manifest_path: 清单文件保存路径。默认使用 _MANIFEST_PATH。
        
    Note:
        - 自动创建父目录（如果不存在）
        - 跳过无法访问的文件（如已被删除）
        - 使用 JSON 格式存储，便于调试
        
    Examples:
        >>> files = {"code": ["src/main.py"], "document": ["README.md"]}
        >>> save_manifest(files, "data/manifest.json")
    """
    manifest: dict[str, float] = {}
    for file_list in files.values():
        for f in file_list:
            try:
                manifest[f] = Path(f).stat().st_mtime
            except OSError:
                pass  # file deleted between detect() and manifest write - skip it
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def detect_incremental(root: Path, manifest_path: str = _MANIFEST_PATH) -> dict:
    """增量扫描：仅返回自上次运行后新增或修改的文件。
    
    对比当前文件状态与存储的清单，识别变更的文件，用于 --update 模式。
    
    Args:
        root: 要扫描的根目录路径。
        manifest_path: 清单文件路径。默认使用 _MANIFEST_PATH。
        
    Returns:
        dict: 包含 detect() 所有字段，额外增加：
            - incremental: True（表示增量扫描）
            - new_files: 按类型分类的新增/修改文件
            - unchanged_files: 按类型分类的未变更文件
            - new_total: 变更文件总数
            - deleted_files: 清单中存在但已删除的文件列表
            
    Note:
        如果清单文件不存在，则回退到完整扫描，并将 incremental 标记为 True，
        new_files 等于完整文件列表。
        
    Examples:
        >>> result = detect_incremental(Path("./project"))
        >>> if result['new_total'] > 0:
        ...     print(f"Need to process {result['new_total']} changed files")
        >>> if result['deleted_files']:
        ...     print(f"Remove graph nodes for: {result['deleted_files']}")
    """
    full = detect(root)
    manifest = load_manifest(manifest_path)

    if not manifest:
        # No previous run - treat everything as new
        full["incremental"] = True
        full["new_files"] = full["files"]
        full["unchanged_files"] = {k: [] for k in full["files"]}
        full["new_total"] = full["total_files"]
        return full

    new_files: dict[str, list[str]] = {k: [] for k in full["files"]}
    unchanged_files: dict[str, list[str]] = {k: [] for k in full["files"]}

    for ftype, file_list in full["files"].items():
        for f in file_list:
            stored_mtime = manifest.get(f)
            try:
                current_mtime = Path(f).stat().st_mtime
            except Exception:
                current_mtime = 0
            if stored_mtime is None or current_mtime > stored_mtime:
                new_files[ftype].append(f)
            else:
                unchanged_files[ftype].append(f)

    # Files in manifest that no longer exist - their cached nodes are now ghost nodes
    current_files = {f for flist in full["files"].values() for f in flist}
    deleted_files = [f for f in manifest if f not in current_files]

    new_total = sum(len(v) for v in new_files.values())
    full["incremental"] = True
    full["new_files"] = new_files
    full["unchanged_files"] = unchanged_files
    full["new_total"] = new_total
    full["deleted_files"] = deleted_files
    return full