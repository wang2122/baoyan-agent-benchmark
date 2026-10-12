"""Bounded full-batch return archives; transport data is never executed.

Shared with the answer computer. No question-bank or reviewer imports.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from .cli_common import regular
from .runtime_common import encoded, regular_bytes, require, write

SCHEMA = 'native-full-return.1'
MANIFEST = 'return-manifest.json'
MAX_FILE = 128 * 1024 * 1024  # Includes base64 native-session checkpoints.
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_MANIFEST = 4 * 1024 * 1024


def safe_name(name):
    require(isinstance(name, str), 'Invalid return path')
    path = PurePosixPath(name)
    require(name and path.parts and name == path.as_posix() and not path.is_absolute()
            and '..' not in path.parts and '\\' not in name and ':' not in name
            and '\x00' not in name and not name.endswith('/'), 'Unsafe return path')
    return path


def fingerprint(path):
    path = Path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= MAX_FILE,
            'Return file must be bounded, regular and not linked')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require((info.st_dev, info.st_ino, info.st_size) == (before.st_dev, before.st_ino, before.st_size),
                'Return file changed while opening')
        value = hashlib.sha256(); size = 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk); require(size <= MAX_FILE, 'Return file exceeds size limit'); value.update(chunk)
        require(size == before.st_size, 'Return file changed while reading')
    return {'sha256': value.hexdigest(), 'bytes': size}


def validate_manifest(value):
    require(isinstance(value, dict) and set(value) == {'schema', 'packet_id', 'files'}
            and value['schema'] == SCHEMA and isinstance(value['packet_id'], str), 'Invalid return manifest')
    files = value['files']
    require(isinstance(files, dict) and 0 < len(files) < MAX_ENTRIES and MANIFEST not in files,
            'Invalid return file count')
    require(len({n.casefold() for n in files}) == len(files), 'Duplicate return paths')
    total = 0
    for name, row in files.items():
        safe_name(name)
        require(isinstance(row, dict) and set(row) == {'sha256', 'bytes'}
                and isinstance(row['sha256'], str) and len(row['sha256']) == 64
                and all(c in '0123456789abcdef' for c in row['sha256'])
                and type(row['bytes']) is int and 0 <= row['bytes'] <= MAX_FILE, 'Invalid return fingerprint')
        total += row['bytes']
    require(total <= MAX_TOTAL, 'Return package exceeds total size limit')
    return value


def export_archive(directory, names, archive, packet_id):
    directory, archive = Path(directory), Path(archive)
    require(len(names) == len(set(names)), 'Duplicate returned file')
    files = {}
    for name in names:
        safe_name(name); files[name] = fingerprint(regular(directory, name))
    manifest = validate_manifest({'schema': SCHEMA, 'packet_id': packet_id, 'files': files})
    require(len(encoded(manifest)) <= MAX_MANIFEST, 'Return manifest exceeds size limit')
    write(directory / MANIFEST, manifest)
    # Exclusive output, explicit membership, no .local/, HOME, credentials or runtime code.
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as target:
        for name in [*names, MANIFEST]:
            target.write(regular(directory, name), name)
    return manifest


def validate_directory(directory):
    directory = Path(directory)
    value = validate_manifest(json.loads(regular_bytes(regular(directory, MANIFEST), MAX_MANIFEST)))
    actual = set()
    for path in directory.rglob('*'):
        require(not path.is_symlink(), 'Linked path in returned package')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    # .lock is owned by the answer runner and is deliberately not transported.
    require(actual - {'.lock'} == set(value['files']) | {MANIFEST}, 'Undeclared or missing returned file')
    for name, row in value['files'].items():
        require(fingerprint(regular(directory, name)) == row, 'Returned content fingerprint differs')
    return value


def extract_archive(archive, target):
    """Extract only verified regular files into a new caller-owned temporary directory."""
    archive, target = Path(archive), Path(target)
    require(archive.is_file() and not archive.is_symlink(), 'Missing or linked return ZIP')
    require(not target.exists(), 'Return extraction destination exists')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist(); names = [e.filename for e in entries]
        require(0 < len(entries) <= MAX_ENTRIES and len(names) == len(set(names))
                and len({n.casefold() for n in names}) == len(names), 'Duplicate or excessive ZIP entries')
        require(sum(e.file_size for e in entries) <= MAX_TOTAL + MAX_MANIFEST, 'ZIP expands beyond size limit')
        for item in entries:
            safe_name(item.filename); mode = item.external_attr >> 16
            require(not item.flag_bits & 1 and not item.is_dir() and not stat.S_ISLNK(mode)
                    and stat.S_IFMT(mode) in (0, stat.S_IFREG) and item.file_size <= MAX_FILE,
                    'Unsupported or oversized ZIP entry')
        require(MANIFEST in names and source.getinfo(MANIFEST).file_size <= MAX_MANIFEST,
                'Missing or oversized return manifest')
        manifest = validate_manifest(json.loads(source.read(MANIFEST)))
        require(set(names) == set(manifest['files']) | {MANIFEST}, 'ZIP membership differs from manifest')
        target.mkdir(parents=True)
        for name, row in manifest['files'].items():
            require(source.getinfo(name).file_size == row['bytes'], 'ZIP file size differs')
            path = target / safe_name(name); path.parent.mkdir(parents=True, exist_ok=True)
            value = hashlib.sha256(); size = 0
            with source.open(name) as incoming, path.open('xb') as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    size += len(chunk); require(size <= row['bytes'], 'ZIP file exceeded declared size')
                    value.update(chunk); outgoing.write(chunk)
            require(size == row['bytes'] and value.hexdigest() == row['sha256'], 'ZIP file fingerprint differs')
            path.chmod(0o600)
        write(target / MANIFEST, manifest)
    return manifest
