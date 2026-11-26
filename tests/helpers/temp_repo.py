"""临时 git 仓库辅助：git init + 一次初始提交。"""

import contextlib
import os
import shutil
import subprocess
import tempfile


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@contextlib.contextmanager
def temp_repo():
    path = tempfile.mkdtemp(prefix="bitlink-repo-")
    try:
        git(path, "init", "-q")
        git(path, "config", "user.email", "bitlink-test@example.com")
        git(path, "config", "user.name", "BitLink Test")
        git(path, "config", "commit.gpgsign", "false")
        with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("# temp repo\n")
        git(path, "add", "README.md")
        result = git(path, "commit", "-q", "-m", "init")
        if result.returncode != 0:
            raise RuntimeError(f"git commit 失败：{result.stderr}")
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
