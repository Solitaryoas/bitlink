"""任务运行标记：记录正在操作某个 worktree 的进程，供复用前判活。

标记写在数据目录内（`<data-dir>/running/<task_id>.json`），不写进 worktree，
避免污染 diff 与主仓库的 `git status`。daemon 派发时先写自己的 pid 占位，
真正执行/review 的子进程启动后把 pid 覆盖成自己的，各自退出时只删自己那条；
daemon 被强杀而子进程存活时标记会留下，下一次复用前据此判活。
"""

import json
import os
import subprocess

from bitlink.db import now_iso


def marker_dir(data_dir):
    return os.path.join(data_dir, "running")


def marker_path(data_dir, task_id):
    return os.path.join(marker_dir(data_dir), f"{task_id}.json")


def pid_is_alive(pid):
    """判断 pid 对应的进程是否存在；不引入第三方依赖。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_is_alive_windows(pid):
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return _pid_is_alive_ctypes(pid)
    return str(pid) in (completed.stdout or "")


def _pid_is_alive_ctypes(pid):
    """tasklist 不可用时的兜底：能拿到进程句柄即视为存活。"""
    import ctypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def write_marker(data_dir, task_id, pid=None):
    """写入（或覆盖）运行标记，返回标记路径。"""
    path = marker_path(data_dir, task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "pid": os.getpid() if pid is None else int(pid),
        "task_id": task_id,
        "started_at": now_iso(),
    }
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, path)
    return path


def read_marker(data_dir, task_id):
    """读取标记；缺失或损坏返回 None。"""
    path = marker_path(data_dir, task_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def clear_marker(data_dir, task_id, pid=None):
    """删除标记；传 pid 时只删属于该进程的标记，避免误删后写入者的。

    标记不存在或已被别人覆盖时静默忽略。
    """
    path = marker_path(data_dir, task_id)
    if pid is not None:
        current = read_marker(data_dir, task_id)
        if current is None or current.get("pid") != int(pid):
            return
    try:
        os.remove(path)
    except OSError:
        return
