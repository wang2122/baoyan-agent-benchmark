"""Bounded, checksummed archives. Imported files are data and are never executed."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from .core import encoded, require

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    require(isinstance(name, str) and name and path.parts and '\x00' not in name
            and '\\' not in name and ':' not in name
            and not path.is_absolute() and '..' not in path.parts
            and name == path.as_posix() and not name.endswith('/'), 'Unsafe archive path')
    return path


def write_archive(out, files, manifest):
    out = Path(out).resolve()
    require(not out.exists(), 'Archive already exists; choose a new output')
    require('manifest.json' not in files and files, 'Invalid archive file set')
    require(sum(len(data) for data in files.values()) <= MAX_TOTAL, 'Archive too large')
    for name, data in files.items():
        safe_name(name)
        require(isinstance(data, bytes) and len(data) <= MAX_FILE, 'Invalid or oversized archive file')
    value = dict(manifest, files={name: hash_bytes(data) for name, data in sorted(files.items())})
    payload = dict(files, **{'manifest.json': encoded(value)})
    out.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects against a concurrent writer or pre-existing symlink.
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(payload.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 8, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, data)
    except BaseException:
        out.unlink(missing_ok=True)  # Only the file exclusively created by this call.
        raise
    return value


def read_archive(path):
    require(Path(path).is_file() and not Path(path).is_symlink(), 'Missing or linked archive')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        require(0 < len(entries) <= 2048 and len(names) == len(set(names))
                and len({n.casefold() for n in names}) == len(names), 'Duplicate or excessive archive entries')
        require(sum(entry.file_size for entry in entries) <= MAX_TOTAL, 'Archive expands beyond size limit')
        for entry in entries:
            safe_name(entry.filename)
            mode = entry.external_attr >> 16
            require(not entry.flag_bits & 1 and not stat.S_ISLNK(mode) and not entry.is_dir()
                    and stat.S_IFMT(mode) in (0, stat.S_IFREG), 'Unsupported archive entry')
            require(entry.file_size <= MAX_FILE, 'Oversized archive entry')
        require('manifest.json' in names, 'Missing archive manifest')
        files = {entry.filename: archive.read(entry) for entry in entries}
    manifest = json.loads(files.pop('manifest.json').decode('utf-8'))
    require(isinstance(manifest, dict) and isinstance(manifest.get('files'), dict), 'Invalid archive manifest')
    require(set(files) == set(manifest['files']), 'Undeclared or missing archive file')
    require(all(hash_bytes(data) == manifest['files'][name] for name, data in files.items()),
            'Archive content fingerprint mismatch')
    return manifest, files
