# fetch URLs (tweet/arxiv/pdf/web) and save as annotated markdown
# 获取 URL（推文/学术论文/PDF/网页）并保存为带注释的 Markdown 文件

from __future__ import annotations
import json
import re
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from graphify.security import safe_fetch, safe_fetch_text, validate_url


def _yaml_str(s: str) -> str:
    """
    将字符串转义为适合嵌入 YAML 双引号标量中的格式。

    对反斜杠、双引号、换行符和回车符进行转义处理，确保字符串可以安全地放入 YAML 的
    双引号字符串中。

    参数:
        s: 需要转义的原始字符串

    返回:
        转义后的字符串，适合作为 YAML 双引号标量的值
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _safe_filename(url: str, suffix: str) -> str:
    """
    将 URL 转换为安全的文件名。

    解析 URL，提取网络位置和路径部分，将非法字符替换为下划线，并限制长度。

    参数:
        url: 原始 URL 字符串
        suffix: 文件名后缀（如 '.md', '.pdf'）

    返回:
        安全的文件名，长度不超过 80 字符（不含后缀），非法字符已被替换
    """
    parsed = urllib.parse.urlparse(url)
    name = parsed.netloc + parsed.path
    name = re.sub(r"[^\w\-]", "_", name).strip("_")
    name = re.sub(r"_+", "_", name)[:80]
    return name + suffix


def _detect_url_type(url: str) -> str:
    """
    根据 URL 特征判断内容类型，用于选择合适的提取策略。

    支持的类型包括：
        - tweet: Twitter/X 推文
        - arxiv: arXiv 学术论文
        - github: GitHub 仓库
        - youtube: YouTube 视频
        - pdf: PDF 文件
        - image: 图片文件（png, jpg, jpeg, webp, gif）
        - webpage: 普通网页（默认）

    参数:
        url: 待分类的 URL 字符串

    返回:
        表示内容类型的字符串
    """
    lower = url.lower()
    if "twitter.com" in lower or "x.com" in lower:
        return "tweet"
    if "arxiv.org" in lower:
        return "arxiv"
    if "github.com" in lower:
        return "github"
    if "youtube.com" in lower or "youtu.be" in lower:
        return "youtube"
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    return "webpage"


def _fetch_html(url: str) -> str:
    """
    获取指定 URL 的 HTML 内容。

    封装了安全获取函数，处理网络请求和基本错误。

    参数:
        url: 目标网页的 URL

    返回:
        HTML 内容的字符串

    异常:
        网络错误会由底层 safe_fetch_text 抛出
    """
    return safe_fetch_text(url)


def _html_to_markdown(html: str, url: str) -> str:
    """
    将 HTML 内容转换为干净的 Markdown 格式。

    优先使用 html2text 库进行高质量转换。如果该库不可用，则使用基本的正则表达式
    清理 HTML 标签作为降级方案。

    参数:
        html: 原始 HTML 字符串
        url: 源 URL（用于上下文，当前版本未使用）

    返回:
        转换后的 Markdown 字符串，降级方案下限制为 8000 字符
    """
    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html)
    except ImportError:
        # Fallback: 降级方案 - 简单去除 HTML 标签
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]


def _fetch_tweet(url: str, author: str | None, contributor: str | None) -> tuple[str, str]:
    """
    获取推文内容并生成带有 YAML 前言的 Markdown 文件。

    使用 Twitter oEmbed API 获取推文。将 x.com 域名统一转换为 twitter.com
    以确保 API 兼容性。如果 API 调用失败，则保存 URL 占位符。

    参数:
        url: 推文的 URL（支持 twitter.com 或 x.com）
        author: 作者名称（用于元数据）
        contributor: 贡献者名称（用于团队图谱）

    返回:
        元组 (文件内容字符串, 建议的文件名)
    """
    # Normalize to twitter.com for oEmbed / 标准化为 twitter.com 以兼容 oEmbed
    oembed_url = url.replace("x.com", "twitter.com")
    oembed_api = f"https://publish.twitter.com/oembed?url={urllib.parse.quote(oembed_url)}&omit_script=true"
    try:
        data = json.loads(safe_fetch_text(oembed_api))
        tweet_text = re.sub(r"<[^>]+>", "", data.get("html", "")).strip()
        tweet_author = data.get("author_name", "unknown")
    except Exception:
        # oEmbed failed - save URL stub / oEmbed 失败 - 保存 URL 占位符
        tweet_text = f"Tweet at {url} (could not fetch content)"
        tweet_author = "unknown"

    now = datetime.now(timezone.utc).isoformat()
    content = f"""---
source_url: "{_yaml_str(url)}"
type: tweet
author: "{_yaml_str(tweet_author)}"
captured_at: {now}
contributor: "{_yaml_str(contributor or author or 'unknown')}"
---

# Tweet by @{tweet_author}

{tweet_text}

Source: {url}
"""
    filename = _safe_filename(url, ".md")
    return content, filename


def _fetch_webpage(url: str, author: str | None, contributor: str | None) -> tuple[str, str]:
    """
    获取普通网页内容并转换为带 YAML 前言的 Markdown 文件。

    提取页面标题，将 HTML 内容转换为 Markdown，并添加元数据前言。

    参数:
        url: 网页的 URL
        author: 作者名称（用于元数据）
        contributor: 贡献者名称（用于团队图谱）

    返回:
        元组 (文件内容字符串, 建议的文件名)
    """
    html = _fetch_html(url)
    # Extract title / 提取标题
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else url

    markdown = _html_to_markdown(html, url)
    now = datetime.now(timezone.utc).isoformat()
    content = f"""---
source_url: "{_yaml_str(url)}"
type: webpage
title: "{_yaml_str(title)}"
captured_at: {now}
contributor: "{_yaml_str(contributor or author or 'unknown')}"
---

# {title}

Source: {url}

---

{markdown[:12000]}
"""
    filename = _safe_filename(url, ".md")
    return content, filename


def _fetch_arxiv(url: str, author: str | None, contributor: str | None) -> tuple[str, str]:
    """
    获取 arXiv 学术论文的摘要页面并生成结构化的 Markdown 文件。

    从摘要页面提取论文标题、作者列表和摘要内容。支持 /abs/ 和 /pdf/ 格式的 URL。
    如果无法识别 arXiv ID，则回退到普通网页处理。

    参数:
        url: arXiv 论文页面 URL
        author: 作者名称（用于元数据）
        contributor: 贡献者名称（用于团队图谱）

    返回:
        元组 (文件内容字符串, 建议的文件名)
    """
    # Convert /abs/ or /pdf/ to abs for the API / 将 /abs/ 或 /pdf/ 转换为 API 可用的格式
    arxiv_id = re.search(r"(\d{4}\.\d{4,5})", url)
    if arxiv_id:
        api_url = f"https://export.arxiv.org/abs/{arxiv_id.group(1)}"
        try:
            html = _fetch_html(api_url)
            abstract_match = re.search(r'class="abstract[^"]*"[^>]*>(.*?)</blockquote>', html, re.DOTALL | re.IGNORECASE)
            abstract = re.sub(r"<[^>]+>", "", abstract_match.group(1)).strip() if abstract_match else ""
            title_match = re.search(r'class="title[^"]*"[^>]*>(.*?)</h1>', html, re.DOTALL | re.IGNORECASE)
            title = re.sub(r"<[^>]+>", " ", title_match.group(1)).strip() if title_match else arxiv_id.group(1)
            authors_match = re.search(r'class="authors"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
            paper_authors = re.sub(r"<[^>]+>", "", authors_match.group(1)).strip() if authors_match else ""
        except Exception:
            title, abstract, paper_authors = arxiv_id.group(1), "", ""
    else:
        return _fetch_webpage(url, author, contributor)

    now = datetime.now(timezone.utc).isoformat()
    content = f"""---
source_url: "{_yaml_str(url)}"
arxiv_id: "{_yaml_str(arxiv_id.group(1) if arxiv_id else '')}"
type: paper
title: "{_yaml_str(title)}"
paper_authors: "{_yaml_str(paper_authors)}"
captured_at: {now}
contributor: "{_yaml_str(contributor or author or 'unknown')}"
---

# {title}

**Authors:** {paper_authors}
**arXiv:** {arxiv_id.group(1) if arxiv_id else url}

## Abstract

{abstract}

Source: {url}
"""
    filename = f"arxiv_{arxiv_id.group(1).replace('.', '_')}.md" if arxiv_id else _safe_filename(url, ".md")
    return content, filename


def _download_binary(url: str, suffix: str, target_dir: Path) -> Path:
    """
    下载二进制文件（PDF、图片等）直接保存到目标目录。

    参数:
        url: 文件的 URL
        suffix: 文件后缀名（如 '.pdf', '.jpg'）
        target_dir: 保存文件的目标目录路径

    返回:
        保存后的文件路径
    """
    filename = _safe_filename(url, suffix)
    out_path = target_dir / filename
    out_path.write_bytes(safe_fetch(url))
    return out_path


def ingest(url: str, target_dir: Path, author: str | None = None, contributor: str | None = None) -> Path:
    """
    获取指定 URL 的内容并保存到目标目录中，生成 graphify 可读取的文件。

    根据 URL 类型自动选择处理策略：
        - PDF/图片：直接下载二进制文件
        - YouTube：下载音频文件
        - 推文：通过 oEmbed API 获取并生成 Markdown
        - arXiv：提取论文摘要并生成结构化 Markdown
        - 普通网页：转换为 Markdown

    所有 Markdown 文件都包含 YAML 格式的前言，存储元数据供 graphify 提取。

    参数:
        url: 需要获取的 URL
        target_dir: 保存文件的目标目录（会自动创建）
        author: 作者名称，存储在节点元数据中
        contributor: 贡献者名称，用于团队协作图谱

    返回:
        保存文件的完整路径

    异常:
        ValueError: URL 验证失败时抛出
        RuntimeError: 网络请求或文件操作失败时抛出
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    url_type = _detect_url_type(url)

    try:
        validate_url(url)
    except ValueError as exc:
        raise ValueError(f"ingest: {exc}") from exc

    try:
        if url_type == "pdf":
            out = _download_binary(url, ".pdf", target_dir)
            print(f"Downloaded PDF: {out.name}")
            return out

        if url_type == "image":
            suffix = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
            out = _download_binary(url, suffix, target_dir)
            print(f"Downloaded image: {out.name}")
            return out

        if url_type == "youtube":
            from graphify.transcribe import download_audio
            out = download_audio(url, target_dir)
            print(f"Downloaded audio: {out.name}")
            return out

        if url_type == "tweet":
            content, filename = _fetch_tweet(url, author, contributor)
        elif url_type == "arxiv":
            content, filename = _fetch_arxiv(url, author, contributor)
        else:
            content, filename = _fetch_webpage(url, author, contributor)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"ingest: failed to fetch {url!r}: {exc}") from exc

    out_path = target_dir / filename
    # Avoid overwriting - append counter if needed / 避免覆盖 - 如有需要则追加计数器
    counter = 1
    while out_path.exists() and counter < 1000:
        stem = Path(filename).stem
        out_path = target_dir / f"{stem}_{counter}.md"
        counter += 1

    out_path.write_text(content, encoding="utf-8")
    print(f"Saved {url_type}: {out_path.name}")
    return out_path


def save_query_result(
    question: str,
    answer: str,
    memory_dir: Path,
    query_type: str = "query",
    source_nodes: list[str] | None = None,
) -> Path:
    """
    将问答结果保存为 Markdown 文件，供 graphify 在下一次 --update 时提取到知识图谱中。

    文件存储在 memory_dir 目录（通常为 graphify-out/memory/），带有 YAML 前言，
    可被 graphify 的提取器解析为节点元数据。这形成了反馈闭环：系统从用户添加的内容
    和用户提出的问题两方面持续学习并变得更智能。

    参数:
        question: 用户提出的问题
        answer: 系统给出的答案
        memory_dir: 存储目录路径（将自动创建）
        query_type: 查询类型标识，默认 "query"
        source_nodes: 答案引用的源节点列表（用于追溯信息来源）

    返回:
        保存文件的完整路径
    """
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    slug = re.sub(r"[^\w]", "_", question.lower())[:50].strip("_")
    filename = f"query_{now.strftime('%Y%m%d_%H%M%S')}_{slug}.md"

    frontmatter_lines = [
        "---",
        f'type: "{query_type}"',
        f'date: "{now.isoformat()}"',
        f'question: "{_yaml_str(question)}"',
        'contributor: "graphify"',
    ]
    if source_nodes:
        nodes_str = ", ".join(f'"{n}"' for n in source_nodes[:10])
        frontmatter_lines.append(f"source_nodes: [{nodes_str}]")
    frontmatter_lines.append("---")

    body_lines = [
        "",
        f"# Q: {question}",
        "",
        "## Answer",
        "",
        answer,
    ]
    if source_nodes:
        body_lines += ["", "## Source Nodes", ""]
        body_lines += [f"- {n}" for n in source_nodes]

    content = "\n".join(frontmatter_lines + body_lines)
    out_path = memory_dir / filename
    out_path.write_text(content, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch a URL into a graphify /raw folder")
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("target_dir", nargs="?", default="./raw", help="Target directory (default: ./raw)")
    parser.add_argument("--author", help="Your name (stored as node metadata)")
    parser.add_argument("--contributor", help="Contributor name for team graphs")
    args = parser.parse_args()
    out = ingest(args.url, Path(args.target_dir), author=args.author, contributor=args.contributor)
    print(f"Ready for graphify: {out}")