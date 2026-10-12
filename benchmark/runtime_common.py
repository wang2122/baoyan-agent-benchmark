"""Standard-library file/process helpers shared by organizer and answer runtimes.

This module must not import tasks, rubrics, model adapters or evaluation code.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    """Atomic JSON replacement; preserve existing regular files and their mode."""
    path = Path(path)
    data = encoded(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode), 'JSON destination must be a regular file')
        mode = stat.S_IMODE(info.st_mode)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def regular_bytes(path, limit=32 * 1024 * 1024):
    """Bounded regular-file read, rejecting links and path swaps before reading."""
    path = Path(path)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_reparse_tag', 0):
        raise OSError('Refusing linked input file')
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'Output is not an unlinked regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino), 'Input file changed while opening')
        require(info.st_size <= limit, 'Output exceeds size limit')
        data = source.read(limit + 1)
        require(len(data) <= limit, 'Output exceeds size limit')
        return data


@contextmanager
def file_lock(path, *, blocking=True, busy_message=None):
    """Lock one persistent file on Unix or Windows; never unlink another holder's lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), 'Lock destination must not be a symlink')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'Lock destination must be a regular file')
        if os.name == 'nt':
            import msvcrt
            # msvcrt locks a byte range; ensure the range exists without truncating.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'\0')
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        if busy_message:
                            raise ValueError(busy_message) from None
                        raise BlockingIOError('Another process owns this file lock') from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                if busy_message:
                    raise ValueError(busy_message) from None
                raise
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
