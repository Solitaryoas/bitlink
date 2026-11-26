"""项目路径推导、数据目录定位与配置合并。

数据目录默认放在项目之外（%LOCALAPPDATA%\\BitLink\\<项目路径哈希前 12 位>），
保证任务执行期间目标仓库的 `git status` 保持干净。
"""

import hashlib
import json
import os
import subprocess

DEFAULTS = {
    "concurrency": 2,
    "poll_interval_seconds": 2,
    "timeout_seconds": 1800,
    "max_attempts": 3,
    # Windows 上 Codex 的沙箱服务会把 agent 写出的文件封在另一个用户下，
    # daemon 和人都读不到，worktree 等于不可用。这里不设默认值，要求显式选择：
    # bypass（隔离只靠 worktree）或 service（沙箱启用，但产物可能不可读）。
    "exec": {"model": None, "windows_sandbox": None},
    "review": {"model": None, "windows_sandbox": "service"},
    # 规划只读、不改文件，默认走沙箱；超时给 5 分钟，够读文件清单再拆卡
    "plan": {"model": None, "timeout_seconds": 300},
    # 新建 worktree 后执行的初始化命令（如 npm ci）。worktree 里没有被
    # .gitignore 忽略的依赖目录，不装依赖 agent 跑不了构建和测试。
    "worktree": {"setup_command": None, "setup_timeout_seconds": 900},
    "dashboard": {"port": 8787},
}


class ConfigError(Exception):
    """配置或环境不满足要求（CLI 退出码 3）。"""


def _run_git(project_root, *args):
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise ConfigError(f"无法执行 git：{exc}") from exc
    return completed


def git_toplevel(path):
    """返回 path 所在 git 仓库的绝对路径；不是 git 仓库时抛 ConfigError。"""
    completed = _run_git(path, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ConfigError(f"不是 git 仓库：{path}（{detail}）")
    return completed.stdout.strip().replace("/", os.sep)


def git_head(project_root):
    """返回当前 HEAD 提交哈希；仓库没有任何提交时抛 ConfigError。"""
    completed = _run_git(project_root, "rev-parse", "HEAD")
    if completed.returncode != 0:
        raise ConfigError("当前仓库还没有任何提交，无法确定 base_ref")
    return completed.stdout.strip()


def local_appdata():
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")


def default_data_dir(project_root):
    normalized = os.path.abspath(project_root)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return os.path.join(local_appdata(), "BitLink", digest)


def _deep_merge(base, overlay):
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_config_file(data_dir):
    path = os.path.join(data_dir, "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置文件无法解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"配置文件顶层必须是对象：{path}")
    return payload


def load_config(project=None, data_dir=None, overrides=None):
    """合并默认值、`<data-dir>/config.json` 与显式覆盖，并创建数据目录。"""
    project_root = git_toplevel(project or os.getcwd())
    resolved_dir = os.path.abspath(data_dir) if data_dir else default_data_dir(project_root)
    os.makedirs(resolved_dir, exist_ok=True)

    merged = _deep_merge(DEFAULTS, _read_config_file(resolved_dir))
    if overrides:
        merged = _deep_merge(merged, overrides)

    merged["project_root"] = project_root
    merged["data_dir"] = resolved_dir
    return merged
