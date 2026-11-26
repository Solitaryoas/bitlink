"""git worktree 与任务分支的创建、复用、清理，以及 diff 生成。"""

import os
import shutil
import subprocess

from bitlink import running


class WorktreeError(RuntimeError):
    """worktree 创建或清理失败；reason 供 daemon 直接写进 block_reason。"""

    def __init__(self, message, reason="worktree_setup_failed"):
        super().__init__(message)
        self.reason = reason


def _git(args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def branch_name(task_id):
    return f"bitlink/{task_id}"


def ensure_worktree(project_root, task_id, base_ref, worktrees_dir, data_dir=None):
    """返回 worktree 路径；目录已存在则复用，否则新建分支与 workspace。

    复用前查两件事：
    1. 数据目录里的运行标记：pid 仍存活说明另一个进程（强杀后残留的 codex）
       还在写这个目录，判 worktree_in_use；pid 已死或标记缺失则清掉残留标记。
    2. `git status`：目录存在但不是这个仓库的 worktree 时同样判 worktree_in_use。
    """
    path = os.path.abspath(os.path.join(worktrees_dir, task_id))
    resolved_data_dir = data_dir or os.path.dirname(os.path.abspath(worktrees_dir))
    if os.path.isdir(path):
        marker = running.read_marker(resolved_data_dir, task_id)
        if marker is not None:
            pid = marker.get("pid")
            if running.pid_is_alive(pid):
                raise WorktreeError(
                    f"worktree_in_use：{path}（pid {pid} 仍在运行）",
                    reason="worktree_in_use",
                )
            running.clear_marker(resolved_data_dir, task_id)
        elif os.path.exists(running.marker_path(resolved_data_dir, task_id)):
            # 文件在但解析不出来：按残留标记清掉，不阻断复用。
            running.clear_marker(resolved_data_dir, task_id)
        probe = _git(["status", "--porcelain"], path)
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout or "").strip()
            raise WorktreeError(
                f"worktree_in_use：{path}（{detail or 'git status 失败'}）",
                reason="worktree_in_use",
            )
        return path
    os.makedirs(worktrees_dir, exist_ok=True)

    branch = branch_name(task_id)
    exists = _git(["rev-parse", "--verify", "--quiet", branch], project_root).returncode == 0
    if exists:
        args = ["worktree", "add", path, branch]
    else:
        args = ["worktree", "add", "-b", branch, path, base_ref]
    completed = _git(args, project_root)
    if completed.returncode != 0:
        raise WorktreeError(
            f"git worktree add 失败（{branch}）：{(completed.stderr or completed.stdout).strip()}"
        )
    return path


def remove_worktree(project_root, path):
    """删除 worktree 目录，保留分支。返回是否成功。"""
    completed = _git(["worktree", "remove", "--force", path], project_root)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    return completed.returncode == 0 or not os.path.isdir(path)


def make_diff(worktree_path, base_ref, out_path):
    """把 worktree 相对 base_ref 的改动写成 patch 文件，供 review 阅读。"""
    _git(["add", "-A"], worktree_path)
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "diff", "--cached", base_ref],
        capture_output=True,
    )
    if completed.returncode != 0:
        completed = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", base_ref], capture_output=True
        )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as handle:
        handle.write(completed.stdout or b"")
    return out_path
