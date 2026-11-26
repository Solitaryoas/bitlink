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


def _is_ancestor(project_root, ancestor, descendant):
    if ancestor == descendant:
        return True
    return (
        _git(["merge-base", "--is-ancestor", ancestor, descendant], project_root).returncode == 0
    )


def resolve_chain_base(project_root, base_ref, dependency_ids):
    """依赖链的起点提交：取能包含其他候选的那个分支 tip。

    依赖任务的改动会提交到它自己的 bitlink/<id> 分支上，所以被依赖的卡应该
    从依赖分支的最新提交起算，否则每张卡都会从同一个 base 重做一遍前置改动。
    依赖互相分叉（没有一个分支包含其他全部）时宁可失败也不猜。
    """
    if not dependency_ids:
        return base_ref
    candidates = [base_ref]
    for dependency_id in dependency_ids:
        tip = _git(
            ["rev-parse", "--verify", "--quiet", branch_name(dependency_id)], project_root
        )
        if tip.returncode == 0 and tip.stdout.strip():
            candidates.append(tip.stdout.strip())
    for candidate in candidates:
        if all(_is_ancestor(project_root, other, candidate) for other in candidates):
            return candidate
    raise WorktreeError(
        "依赖分支互相分叉，无法确定叠加起点：" + ", ".join(dependency_ids),
        reason="chain_diverged",
    )


def commit_worktree(worktree_path, message):
    """把当前改动提交到任务分支，返回提交 sha；没有改动时返回当前 HEAD。"""
    _git(["add", "-A"], worktree_path)
    committed = _git(["-c", "commit.gpgsign=false", "commit", "-m", message], worktree_path)
    head = _git(["rev-parse", "HEAD"], worktree_path)
    if committed.returncode == 0:
        return head.stdout.strip()
    status = _git(["status", "--porcelain"], worktree_path)
    if status.stdout.strip() == "" and head.returncode == 0:
        return head.stdout.strip()
    detail = (committed.stderr or committed.stdout or "").strip()
    raise WorktreeError(f"提交任务分支失败：{detail}", reason="commit_failed")


def ensure_worktree(
    project_root, task_id, base_ref, worktrees_dir, data_dir=None, on_create=None
):
    """返回 worktree 路径；目录已存在则复用，否则新建分支与 workspace。

    复用前查两件事：
    1. 数据目录里的运行标记：pid 仍存活说明另一个进程（强杀后残留的 codex）
       还在写这个目录，判 worktree_in_use；pid 已死或标记缺失则清掉残留标记。
    2. `git status`：目录存在但不是这个仓库的 worktree 时同样判 worktree_in_use。

    `on_create` 只在本次新建了 worktree 时调用，用于执行依赖安装等初始化命令；
    复用旧 worktree 不会重复执行。
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
    if on_create is not None:
        on_create(path)
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
