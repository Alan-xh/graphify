# Video transcription using faster-whisper
# Converts video/audio files to text transcripts for graph extraction
from __future__ import annotations

import os
from pathlib import Path


VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v', '.mp3', '.wav', '.m4a', '.ogg'}
URL_PREFIXES = ('http://', 'https://', 'www.')

_DEFAULT_MODEL = "base"
_TRANSCRIPTS_DIR = "graphify-out/transcripts"
_FALLBACK_PROMPT = "Use proper punctuation and paragraph breaks."


def _model_name() -> str:
    """从环境变量获取 Whisper 模型名称，若未设置则返回默认值。

    Returns:
        str: 模型名称（如 'base', 'small', 'medium', 'large' 等），
             来自 GRAPHIFY_WHISPER_MODEL 环境变量，未设置时返回 'base'。
    """
    return os.environ.get("GRAPHIFY_WHISPER_MODEL", _DEFAULT_MODEL)


def _get_whisper():
    """导入并返回 faster_whisper 中的 WhisperModel 类。

    Returns:
        type: faster_whisper 的 WhisperModel 类。

    Raises:
        ImportError: 如果未安装 faster_whisper，提示用户通过
                     'pip install graphifyy[video]' 安装。
    """
    try:
        from faster_whisper import WhisperModel
        return WhisperModel
    except ImportError as exc:
        raise ImportError(
            "Video transcription requires faster-whisper. "
            "Run: pip install 'graphifyy[video]'"
        ) from exc


def _get_yt_dlp():
    """导入并返回 yt_dlp 模块。

    Returns:
        module: yt_dlp 模块。

    Raises:
        ImportError: 如果未安装 yt-dlp，提示用户通过
                     'pip install graphifyy[video]' 安装。
    """
    try:
        import yt_dlp
        return yt_dlp
    except ImportError as exc:
        raise ImportError(
            "YouTube/URL download requires yt-dlp. "
            "Run: pip install 'graphifyy[video]'"
        ) from exc


def is_url(path: str) -> bool:
    """判断字符串是否为 URL 而非本地文件路径。

    Args:
        path (str): 待检查的字符串（文件路径或 URL）。

    Returns:
        bool: 如果字符串以 'http://'、'https://' 或 'www.' 开头则返回 True，
              否则返回 False。
    """
    return any(path.startswith(p) for p in URL_PREFIXES)


def download_audio(url: str, output_dir: Path) -> Path:
    """使用 yt-dlp 从 URL 下载纯音频流。

    Args:
        url (str): 待下载视频/音频的 URL。
        output_dir (Path): 保存下载音频文件的目录。

    Returns:
        Path: 下载的音频文件路径（.m4a、.opus 等格式）。

    Notes:
        - 基于 URL 哈希生成稳定的文件名以支持缓存。
        - 下载前会检查是否已存在相同文件。
        - 下载最佳可用音频格式（优先 .m4a）。
    """
    yt_dlp = _get_yt_dlp()
    output_dir.mkdir(parents=True, exist_ok=True)

    # yt-dlp 使用 %(title)s 可能过长/特殊，改用基于 URL 哈希的稳定名称
    import hashlib
    url_hash = hashlib.sha1(url.encode()).hexdigest()[:12]
    out_template = str(output_dir / f"yt_{url_hash}.%(ext)s")

    # 检查是否已下载
    for ext in ('.m4a', '.opus', '.mp3', '.ogg', '.wav', '.webm'):
        candidate = output_dir / f"yt_{url_hash}{ext}"
        if candidate.exists():
            print(f"  cached audio: {candidate.name}")
            return candidate

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'postprocessors': [],  # 不需要 ffmpeg，使用原生音频
    }

    print(f"  downloading audio: {url[:80]} ...", flush=True)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        ext = info.get('ext', 'm4a')
        downloaded = output_dir / f"yt_{url_hash}.{ext}"
        if not downloaded.exists():
            # yt-dlp 可能选择了不同的扩展名
            for p in output_dir.glob(f"yt_{url_hash}.*"):
                downloaded = p
                break
        return downloaded


def build_whisper_prompt(god_nodes: list[dict]) -> str:
    """从 god 节点构建 Whisper 的领域提示（domain hint）。

    Args:
        god_nodes (list[dict]): God 节点字典列表，每个字典包含 'label' 键，
                                存储节点的文本标签。

    Returns:
        str: 用于 Whisper 的 initial_prompt 参数的提示字符串。
             - 优先返回 GRAPHIFY_WHISPER_PROMPT 环境变量的值。
             - 否则从顶层 god 节点标签生成提示。
             - 若无 god 节点则回退到默认标点符号提示。

    Notes:
        编码代理（Claude Code、Codex 等）从这些标签生成实际的一句子领域提示，
        并通过 GRAPHIFY_WHISPER_PROMPT 传递，此处无需单独的 API 调用。
    """
    if not god_nodes:
        return _FALLBACK_PROMPT

    override = os.environ.get("GRAPHIFY_WHISPER_PROMPT")
    if override:
        return override

    labels = [n.get("label", "") for n in god_nodes[:10] if n.get("label")]
    if not labels:
        return _FALLBACK_PROMPT

    topics = ", ".join(labels[:5])
    return f"Technical discussion about {topics}. Use proper punctuation and paragraph breaks."


def transcribe(
    video_path: Path | str,
    output_dir: Path | None = None,
    initial_prompt: str | None = None,
    force: bool = False,
) -> Path:
    """将视频/音频文件或 URL 转录音频为文本。

    Args:
        video_path (Path | str): 本地视频/音频文件路径或 URL。
        output_dir (Path | None): 保存转录文件的目录。若为 None，
                                  默认使用 'graphify-out/transcripts'。
        initial_prompt (str | None): Whisper 的领域提示，用于提高识别准确率。
                                     若为 None，使用默认提示。
        force (bool): 若为 True，即使转录文件已存在也重新转录。
                      若为 False，返回缓存的转录结果。

    Returns:
        Path: 保存的转录文件路径（.txt 格式）。

    Notes:
        - URL 会先通过 yt-dlp 下载音频。
        - 默认使用 faster-whisper 在 CPU 上运行，使用 int8 量化。
        - 可通过 GRAPHIFY_WHISPER_MODEL 环境变量更改模型。
        - 转录结果保存为纯文本文件，每行对应一个识别片段。

    Raises:
        ImportError: 如果未安装 faster_whisper。
        Exception: 转录过程中的其他错误会被捕获并打印。
    """
    out_dir = Path(output_dir) if output_dir else Path(_TRANSCRIPTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    if is_url(str(video_path)):
        audio_path = download_audio(str(video_path), out_dir / "downloads")
    else:
        audio_path = Path(video_path)

    transcript_path = out_dir / (audio_path.stem + ".txt")
    if transcript_path.exists() and not force:
        return transcript_path

    WhisperModel = _get_whisper()
    model_name = _model_name()
    prompt = initial_prompt or _FALLBACK_PROMPT

    print(f"  transcribing {audio_path.name} (model={model_name}) ...", flush=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        initial_prompt=prompt,
    )

    lines = [segment.text.strip() for segment in segments if segment.text.strip()]
    transcript = "\n".join(lines)

    transcript_path.write_text(transcript, encoding="utf-8")
    lang = info.language if hasattr(info, "language") else "unknown"
    print(f"  transcript saved -> {transcript_path} (lang={lang}, {len(lines)} segments)")
    return transcript_path


def transcribe_all(
    video_files: list[str],
    output_dir: Path | None = None,
    initial_prompt: str | None = None,
) -> list[str]:
    """批量转录多个视频/音频文件或 URL。

    Args:
        video_files (list[str]): 待转录的文件路径或 URL 列表。
        output_dir (Path | None): 保存转录文件的目录。若为 None，
                                  默认使用 'graphify-out/transcripts'。
        initial_prompt (str | None): Whisper 的领域提示，所有转录共享使用。
                                     从语料库 god 节点构建一次。

    Returns:
        list[str]: 保存的转录文件路径列表（.txt 格式），顺序与输入文件一致。
                   转录失败的文件不会出现在结果中。

    Notes:
        - 已转录的文件会直接从缓存返回。
        - 转录过程中的错误会被捕获并记录日志，但不会影响剩余文件的处理。
    """
    if not video_files:
        return []

    transcript_paths = []
    for vf in video_files:
        try:
            t = transcribe(vf, output_dir, initial_prompt=initial_prompt)
            transcript_paths.append(str(t))
        except Exception as exc:
            print(f"  warning: could not transcribe {vf}: {exc}")
    return transcript_paths