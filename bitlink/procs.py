"""子进程执行与超时终止（执行器与 review 共用）。"""

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass

from bitlink import running

SESSION_KEYS = ("thread_id", "session_id", "conversation_id")


class ProcessRegistry:
    """按任务 id 登记在跑的子进程，供 Ctrl+C、stop() 与人工取消统一终止。

    执行器与 review 进程启动后登记、退出时注销；daemon 通过它拿到句柄，
    不需要执行器返回值来回传进程对象。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._procs = {}

    def register(self, key, proc):
        with self._lock:
            self._procs.setdefault(key, set()).add(proc)

    def unregister(self, key, proc):
        with self._lock:
            group = self._procs.get(key)
            if group is None:
                return
            group.discard(proc)
            if not group:
                self._procs.pop(key, None)

    def terminate(self, key):
        with self._lock:
            procs = list(self._procs.get(key, ()))
        for proc in procs:
            terminate_process(proc)

    def terminate_all(self):
        with self._lock:
            procs = [proc for group in self._procs.values() for proc in group]
        for proc in procs:
            terminate_process(proc)



@dataclass
class ProcOutcome:
    exit_code: object
    timed_out: bool
    stdout_path: str
    stderr_path: str


def terminate_process(proc):
    """先 taskkill 整棵进程树，失败再 terminate/kill。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_process(
    command,
    stdout_path,
    stderr_path,
    timeout_seconds,
    env=None,
    cwd=None,
    registry=None,
    registry_key=None,
    marker_dir=None,
    marker_task_id=None,
):
    """运行命令，stdout/stderr 落文件；超时终止并标记 timed_out。

    传入 registry 时，Popen 启动后按 registry_key 登记，退出时注销，
    让 daemon 能在 Ctrl+C / stop() / 取消时终止它。

    传入 marker_dir + marker_task_id 时，把子进程 pid 写进数据目录的运行标记，
    退出时只清掉属于自己的标记。daemon 被强杀而子进程存活时，标记会留下
    子进程 pid，下一次复用 worktree 前据此判活。
    """
    os.makedirs(os.path.dirname(os.path.abspath(stdout_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(stderr_path)), exist_ok=True)

    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    timed_out = False
    with open(stdout_path, "wb") as out_handle, open(stderr_path, "wb") as err_handle:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            env=env,
            cwd=cwd,
            **popen_kwargs,
        )
        if registry is not None:
            registry.register(registry_key, proc)
        if marker_dir is not None and marker_task_id is not None:
            try:
                running.write_marker(marker_dir, marker_task_id, pid=proc.pid)
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process(proc)
        finally:
            if registry is not None:
                registry.unregister(registry_key, proc)
            if marker_dir is not None and marker_task_id is not None:
                running.clear_marker(marker_dir, marker_task_id, pid=proc.pid)
    return ProcOutcome(
        exit_code=proc.returncode,
        timed_out=timed_out,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _search_session_id(payload):
    if isinstance(payload, dict):
        for key in SESSION_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _search_session_id(value)
                if found:
                    return found
    elif isinstance(payload, list):
        for item in payload:
            found = _search_session_id(item)
            if found:
                return found
    return None


def first_session_id(jsonl_path):
    """从 JSONL 第一段含会话 id 的事件里取 thread/session id。"""
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                found = _search_session_id(payload)
                if found:
                    return found
    except OSError:
        return None
    return None
