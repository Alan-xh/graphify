# git hook integration - install/uninstall graphify post-commit and post-checkout hooks
from __future__ import annotations
import re
import subprocess
from pathlib import Path

_HOOK_MARKER = "# graphify-hook-start"  # post-commit 钩子开始标记
_HOOK_MARKER_END = "# graphify-hook-end"  # post-commit 钩子结束标记
_CHECKOUT_MARKER = "# graphify-checkout-hook-start"  # post-checkout 钩子开始标记
_CHECKOUT_MARKER_END = "# graphify-checkout-hook-end"  # post-checkout 钩子结束标记

_PYTHON_DETECT = """\
# 检测正确的 Python 解释器（支持 pipx、venv、系统安装）
GRAPHIFY_BIN=$(command -v graphify 2>/dev/null)
if [ -n "$GRAPHIFY_BIN" ]; then
    case "$GRAPHIFY_BIN" in
        *.exe) _SHEBANG="" ;;
        *)     _SHEBANG=$(head -1 "$GRAPHIFY_BIN" | sed 's/^#![[:space:]]*//') ;;
    esac
    case "$_SHEBANG" in
        */env\\ *) GRAPHIFY_PYTHON="${_SHEBANG#*/env }" ;;
        *)         GRAPHIFY_PYTHON="$_SHEBANG" ;;
    esac
    # 白名单：只保留文件系统路径中的有效字符，防止 shebang 包含 shell 元字符导致注入
    case "$GRAPHIFY_PYTHON" in
        *[!a-zA-Z0-9/_.@-]*) GRAPHIFY_PYTHON="" ;;
    esac
    if [ -n "$GRAPHIFY_PYTHON" ] && ! "$GRAPHIFY_PYTHON" -c "import graphify" 2>/dev/null; then
        GRAPHIFY_PYTHON=""
    fi
fi
# 回退方案：尝试 python3，然后尝试 python（Windows 没有 python3 软链接）
if [ -z "$GRAPHIFY_PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1 && python3 -c "import graphify" 2>/dev/null; then
        GRAPHIFY_PYTHON="python3"
    elif command -v python >/dev/null 2>&1 && python -c "import graphify" 2>/dev/null; then
        GRAPHIFY_PYTHON="python"
    else
        exit 0
    fi
fi
"""

_HOOK_SCRIPT = """\
# graphify-hook-start
# 每次提交后自动重建知识图谱（仅代码文件，无需 LLM）
# 安装命令：graphify hook install

# 在 rebase/merge/cherry-pick 期间跳过执行，避免阻碍带有未暂存变更的 --continue 操作
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0

CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || git diff --name-only HEAD 2>/dev/null)
if [ -z "$CHANGED" ]; then
    exit 0
fi

""" + _PYTHON_DETECT + """
export GRAPHIFY_CHANGED="$CHANGED"
$GRAPHIFY_PYTHON -c "
import os, sys
from pathlib import Path

changed_raw = os.environ.get('GRAPHIFY_CHANGED', '')
changed = [Path(f.strip()) for f in changed_raw.strip().splitlines() if f.strip()]

if not changed:
    sys.exit(0)

print(f'[graphify hook] {len(changed)} file(s) changed - rebuilding graph...')

try:
    from graphify.watch import _rebuild_code
    _rebuild_code(Path('.'))
except Exception as exc:
    print(f'[graphify hook] Rebuild failed: {exc}')
    sys.exit(1)
"
# graphify-hook-end
"""


_CHECKOUT_SCRIPT = """\
# graphify-checkout-hook-start
# 切换分支时自动重建知识图谱（仅代码文件）
# 安装命令：graphify hook install

PREV_HEAD=$1
NEW_HEAD=$2
BRANCH_SWITCH=$3

# 仅在分支切换时执行，文件检出时不执行
if [ "$BRANCH_SWITCH" != "1" ]; then
    exit 0
fi

# 仅当 graphify-out/ 目录存在（图谱已构建过）时才执行
if [ ! -d "graphify-out" ]; then
    exit 0
fi

# 在 rebase/merge/cherry-pick 期间跳过执行
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null)
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0

""" + _PYTHON_DETECT + """
echo "[graphify] Branch switched - rebuilding knowledge graph (code files)..."
$GRAPHIFY_PYTHON -c "
from graphify.watch import _rebuild_code
from pathlib import Path
import sys
try:
    _rebuild_code(Path('.'))
except Exception as exc:
    print(f'[graphify] Rebuild failed: {exc}')
    sys.exit(1)
"
# graphify-checkout-hook-end
"""


def _git_root(path: Path) -> Path | None:
    """
    向上遍历查找包含 .git 目录的根目录。

    参数：
        path: 起始搜索路径

    返回：
        找到的 Git 仓库根目录路径，若未找到则返回 None
    """
    current = path.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _hooks_dir(root: Path) -> Path:
    """
    获取 git 钩子目录路径，遵循 core.hooksPath 配置（例如 Husky）。

    参数：
        root: Git 仓库根目录

    返回：
        钩子目录的路径（如不存在则自动创建）
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "core.hooksPath"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            custom = result.stdout.strip()
            if custom:
                p = Path(custom).expanduser()
                if not p.is_absolute():
                    p = root / p
                p.mkdir(parents=True, exist_ok=True)
                return p
    except (OSError, FileNotFoundError):
        pass
    d = root / ".git" / "hooks"
    d.mkdir(exist_ok=True)
    return d


def _install_hook(hooks_dir: Path, name: str, script: str, marker: str) -> str:
    """
    安装单个 git 钩子，若钩子已存在则追加内容。

    参数：
        hooks_dir: 钩子目录路径
        name: 钩子名称（如 'post-commit'）
        script: 要插入的钩子脚本内容
        marker: 标识钩子所属的标记字符串

    返回：
        描述安装结果的状态信息
    """
    hook_path = hooks_dir / name
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8")
        if marker in content:
            return f"already installed at {hook_path}"
        hook_path.write_text(content.rstrip() + "\n\n" + script, encoding="utf-8", newline="\n")
        return f"appended to existing {name} hook at {hook_path}"
    hook_path.write_text("#!/bin/sh\n" + script, encoding="utf-8", newline="\n")
    hook_path.chmod(0o755)
    return f"installed at {hook_path}"


def _uninstall_hook(hooks_dir: Path, name: str, marker: str, marker_end: str) -> str:
    """
    使用开始/结束标记从 git 钩子中移除 graphify 相关代码段。

    参数：
        hooks_dir: 钩子目录路径
        name: 钩子名称（如 'post-commit'）
        marker: 开始标记字符串
        marker_end: 结束标记字符串

    返回：
        描述卸载结果的状态信息
    """
    hook_path = hooks_dir / name
    if not hook_path.exists():
        return f"no {name} hook found - nothing to remove."
    content = hook_path.read_text(encoding="utf-8")
    if marker not in content:
        return f"graphify hook not found in {name} - nothing to remove."
    new_content = re.sub(
        rf"{re.escape(marker)}.*?{re.escape(marker_end)}\n?",
        "",
        content,
        flags=re.DOTALL,
    ).strip()
    if not new_content or new_content in ("#!/bin/bash", "#!/bin/sh"):
        hook_path.unlink()
        return f"removed {name} hook at {hook_path}"
    hook_path.write_text(new_content + "\n", encoding="utf-8", newline="\n")
    return f"graphify removed from {name} at {hook_path} (other hook content preserved)"


def install(path: Path = Path(".")) -> str:
    """
    在最近的 git 仓库中安装 graphify 的 post-commit 和 post-checkout 钩子。

    参数：
        path: 仓库内的任意路径，默认为当前目录

    返回：
        描述安装结果的字符串信息

    异常：
        RuntimeError: 在给定路径或以上层级未找到 git 仓库时抛出
    """
    root = _git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")

    hooks_dir = _hooks_dir(root)

    commit_msg = _install_hook(hooks_dir, "post-commit", _HOOK_SCRIPT, _HOOK_MARKER)
    checkout_msg = _install_hook(hooks_dir, "post-checkout", _CHECKOUT_SCRIPT, _CHECKOUT_MARKER)

    return f"post-commit: {commit_msg}\npost-checkout: {checkout_msg}"


def uninstall(path: Path = Path(".")) -> str:
    """
    移除 graphify 的 post-commit 和 post-checkout 钩子。

    参数：
        path: 仓库内的任意路径，默认为当前目录

    返回：
        描述卸载结果的字符串信息

    异常：
        RuntimeError: 在给定路径或以上层级未找到 git 仓库时抛出
    """
    root = _git_root(path)
    if root is None:
        raise RuntimeError(f"No git repository found at or above {path.resolve()}")

    hooks_dir = _hooks_dir(root)
    commit_msg = _uninstall_hook(hooks_dir, "post-commit", _HOOK_MARKER, _HOOK_MARKER_END)
    checkout_msg = _uninstall_hook(hooks_dir, "post-checkout", _CHECKOUT_MARKER, _CHECKOUT_MARKER_END)

    return f"post-commit: {commit_msg}\npost-checkout: {checkout_msg}"


def status(path: Path = Path(".")) -> str:
    """
    检查 graphify 钩子是否已安装。

    参数：
        path: 仓库内的任意路径，默认为当前目录

    返回：
        描述 post-commit 和 post-checkout 钩子安装状态的字符串
    """
    root = _git_root(path)
    if root is None:
        return "Not in a git repository."
    hooks_dir = _hooks_dir(root)

    def _check(name: str, marker: str) -> str:
        p = hooks_dir / name
        if not p.exists():
            return "not installed"
        return "installed" if marker in p.read_text(encoding="utf-8") else "not installed (hook exists but graphify not found)"

    commit = _check("post-commit", _HOOK_MARKER)
    checkout = _check("post-checkout", _CHECKOUT_MARKER)
    return f"post-commit: {commit}\npost-checkout: {checkout}"