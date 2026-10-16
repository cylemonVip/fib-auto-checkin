"""跨平台私有数据目录、进程锁和原子写入。"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile


def data_root():
    override = os.environ.get("FIB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/FIB Checkin"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "FIB Checkin"
    return Path.home() / ".local/share/fib-checkin"


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def file_lock(path):
    path = Path(path)
    private_dir(path.parent)
    with path.open("a+b") as stream:
        if os.name != "nt":
            import fcntl
            os.chmod(path, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise BlockingIOError("file is locked") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, data):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize_data(legacy_root=None):
    """一次迁移旧版项目内数据；保留原文件，不覆盖新目录里的记录。"""
    root = private_dir(data_root())
    if os.environ.get("FIB_DATA_DIR"):
        return root  # 显式指定的目录用于独立实例或测试，不混入其他账号。
    legacy = Path(legacy_root or Path(__file__).resolve().parent)
    marker = root / "migration.json"
    with file_lock(root / "migration.lock"):
        if marker.exists():
            return root
        # 先迁移每日提交记录，再开放账号会话。
        old_state = legacy / ".state"
        if old_state.is_dir():
            for source in old_state.glob("*.json"):
                destination = root / ".state" / source.name
                if not destination.exists():
                    atomic_json(destination, json.loads(source.read_text(encoding="utf-8")))
        old_users = legacy / ".users/users.json"
        new_users = root / ".users/users.json"
        if old_users.is_file() and not new_users.exists():
            atomic_json(new_users, json.loads(old_users.read_text(encoding="utf-8")))
        atomic_json(marker, {"version": 1})
    return root
