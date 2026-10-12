"""Answer-side continuation checkpoints: artifacts and Codex session logs only.

No task/rubric imports. Never persist copied authentication or host login state.
"""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath

from .runtime_common import digest, regular_bytes, require, write

MAX_TOTAL = 16 * 1024 * 1024
MAX_FILES = 256
SCHEMA = 'candidate-episode-state.1'


def valid_name(name):
    require(isinstance(name, str), 'Invalid continuation path')
    parts = PurePosixPath(name).parts
    require(parts and name == PurePosixPath(name).as_posix() and not name.startswith('/')
            and '..' not in parts and '\\' not in name and ':' not in name,
            'Invalid continuation path')
    require((parts[0] == 'artifacts' and len(parts) > 1) or
            (parts[0] == 'sessions' and len(parts) > 1 and name.endswith('.jsonl')),
            'Only candidate artifacts and session JSONL may persist')
    return parts


def load_checkpoint(path):
    from .native_sessions import MAX_TOTAL as NATIVE_MAX_TOTAL
    value = json.loads(regular_bytes(path, NATIVE_MAX_TOTAL * 2))
    if value.get('schema_version') == 'candidate-native-session.2':
        from .native_sessions import load
        return load(path)
    require(value.get('schema_version') == SCHEMA and isinstance(value.get('session_id'), str)
            and value['session_id'].strip(), 'Invalid continuation identity')
    files = value.get('files')
    require(isinstance(files, dict) and len(files) <= MAX_FILES, 'Invalid continuation files')
    total = 0
    for name, record in files.items():
        valid_name(name)
        data = base64.b64decode(record['base64'], validate=True)
        total += len(data)
        require(total <= MAX_TOTAL and hashlib.sha256(data).hexdigest() == record['sha256'],
                'Continuation content changed or exceeds size limit')
    require(any(name.startswith('sessions/') for name in files), 'Missing native session persistence')
    return value


def restore_checkpoint(box, path):
    value = load_checkpoint(path)
    for name, record in value['files'].items():
        parts = valid_name(name)
        target = (box.home / '.codex' if parts[0] == 'sessions' else box.work) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        require(not target.exists() and not target.is_symlink(), 'Continuation destination already exists')
        target.write_bytes(base64.b64decode(record['base64'], validate=True))
        target.chmod(0o600)
    return value['session_id']


def save_checkpoint(box, session_id, path):
    files, total = {}, 0
    for prefix, base in (('sessions', box.home / '.codex/sessions'), ('artifacts', box.work / 'artifacts')):
        if not base.exists():
            continue
        require(not base.is_symlink(), 'Linked continuation directory')
        for item in sorted(base.rglob('*')):
            require(not item.is_symlink(), 'Linked continuation file')
            if item.is_dir():
                continue
            name = prefix + '/' + item.relative_to(base).as_posix()
            valid_name(name)
            data = regular_bytes(item, MAX_TOTAL)
            total += len(data)
            require(total <= MAX_TOTAL and len(files) < MAX_FILES, 'Continuation state exceeds budget')
            files[name] = {'base64': base64.b64encode(data).decode('ascii'),
                           'sha256': hashlib.sha256(data).hexdigest()}
    require(any(name.startswith('sessions/') for name in files), 'CLI did not save a resumable session')
    value = {'schema_version': SCHEMA, 'session_id': session_id, 'files': files}
    write(path, value)
    return {'sha256': digest(value), 'session_id': session_id,
            'artifacts': {name: row['sha256'] for name, row in files.items() if name.startswith('artifacts/')}}


def continuation_invocation(agent, binary, prompt, box, output, selected_env, session_id=None):
    from .cli_adapters import invocation
    require(agent['cli'] == 'codex', 'Automatic episodes currently support Codex; use the manual episode flow for other products')
    args, stdin, env = invocation(agent, binary, prompt, box.work, box.work, output,
                                  box=box, selected_env=selected_env)
    args.remove('--ephemeral')
    if session_id is not None:
        index = args.index('--cd'); del args[index:index + 2]
        args.insert(2, 'resume')
        args.insert(len(args) - 1, session_id)
    return args, stdin, env
