"""Native session adapters shared by all models using the same CLI.

Only session data created inside this attempt's isolated HOME is checkpointed.
Authentication, settings, plugins, locks and personal histories never persist.
"""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile

from .runtime_common import digest, regular_bytes, require, write

SCHEMA = 'candidate-native-session.2'
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 1024
RESUME_FLAGS = {'codex': 'resume', 'claude': '--resume', 'gemini': '--resume',
                'qwen': '--resume', 'kimi': '--session', 'dsh': '--session-id', 'antigravity': '--conversation'}
# Explicit paths from the pinned clients' storage implementations. No whole HOME copies.
PATTERNS = {
    'antigravity': (r'\.gemini/antigravity-cli/conversation_summaries\.db',
                    r'\.gemini/antigravity-cli/conversations/[0-9a-f-]{36}\.db',
                    r'\.gemini/antigravity-cli/brain/[0-9a-f-]{36}/.+'),
    'codex': (r'\.codex/sessions/.+\.jsonl',),
    'claude': (r'\.claude/projects/[^/]+/(?:[^/]+/subagents/)?[^/]+\.jsonl',),
    'gemini': (r'\.gemini/tmp/[^/]+/chats/session-[^/]+\.jsonl?', r'\.gemini/projects\.json'),
    'qwen': (r'\.qwen/projects/[^/]+/(?:[^/]+/subagents/)?[^/]+\.(?:jsonl|meta\.json)',
             r'\.qwen/projects/[^/]+/chats/[^/]+\.jsonl',
             r'\.qwen/projects/[^/]+/subagents/[^/]+/[^/]+\.(?:jsonl|meta\.json)',
             r'\.qwen/tmp/[^/]+/tool-results/.+'),
    'kimi': (r'\.kimi-code/session_index\.jsonl',
             r'\.kimi-code/sessions/[^/]+/[^/]+/state\.json',
             r'\.kimi-code/sessions/[^/]+/[^/]+/agents/[^/]+/wire\.jsonl',
             r'\.kimi-code/sessions/[^/]+/[^/]+/agents/[^/]+/plans/[^/]+\.md'),
    'dsh': (r'\.dsh/sessions/.+\.jsonl(?:\.zstd)?',),
}
HOME_ROOTS = {'antigravity': '.gemini/antigravity-cli', 'codex': '.codex/sessions', 'claude': '.claude/projects', 'gemini': '.gemini',
              'qwen': '.qwen', 'kimi': '.kimi-code', 'dsh': '.dsh/sessions'}


def capability(cli):
    require(cli in RESUME_FLAGS, f'No native session adapter for {cli}')
    return {'cli': cli, 'multi_turn': True, 'file_continuation': True, 'checkpoint_resume': True,
            'resume_flag': RESUME_FLAGS[cli], 'verification': 'adapter_implemented_runtime_trial_required'}


def valid_name(name, cli):
    require(isinstance(name, str), 'Invalid native continuation path')
    parts = PurePosixPath(name).parts
    require(parts and name == PurePosixPath(name).as_posix() and not name.startswith('/')
            and '..' not in parts and '\\' not in name and ':' not in name,
            'Invalid native continuation path')
    if parts[0] == 'artifacts' and len(parts) > 1:
        return parts
    if parts[0] == 'workspace' and len(parts) > 1:
        require(parts[1] not in ('task.json', 'query.md', 'answer.md', 'artifacts'), 'Reserved continuation input path')
        return parts
    require(parts[0] == 'native' and len(parts) > 2 and
            any(re.fullmatch(p, '/'.join(parts[1:])) for p in PATTERNS[cli]),
            'File outside native session allowlist')
    return parts


def load(path):
    value = json.loads(regular_bytes(path, MAX_TOTAL * 2))
    require(value.get('schema_version') == SCHEMA, 'Wrong native checkpoint version')
    cli = value.get('cli'); capability(cli)
    require(isinstance(value.get('session_id'), str) and value['session_id'].strip(), 'Missing session ID')
    require(isinstance(value.get('directory'), str) and
            re.fullmatch(r'baoyan-candidate-[a-z0-9_]{8}', value['directory']), 'Invalid session directory')
    require(isinstance(value.get('temporary_parent'), str) and Path(value['temporary_parent']).is_absolute(),
            'Missing original temporary directory')
    files = value.get('files')
    require(isinstance(files, dict) and len(files) <= MAX_FILES, 'Invalid native checkpoint files')
    total = 0
    for name, row in files.items():
        valid_name(name, cli)
        data = base64.b64decode(row['base64'], validate=True); total += len(data)
        require(total <= MAX_TOTAL and hashlib.sha256(data).hexdigest() == row['sha256'],
                'Native checkpoint changed or exceeds size limit')
    def session_file(n):
        if not n.startswith('native/'):
            return False
        if cli == 'antigravity':
            return n == 'native/.gemini/antigravity-cli/conversations/' + value['session_id'] + '.db'
        if cli == 'kimi':
            return n.endswith('/wire.jsonl')
        if cli == 'gemini':
            return '/chats/session-' in n
        if cli == 'qwen':
            return '/projects/' in n and n.endswith('.jsonl')
        return n.endswith(('.jsonl', '.jsonl.zstd'))
    require(any(session_file(n) for n in files), 'Missing persisted native session')
    if cli == 'antigravity':
        state = value.get('antigravity_state')
        require(isinstance(state, dict) and type(state.get('num_turns')) is int and state['num_turns'] > 0
                and isinstance(state.get('usage'), dict)
                and all(type(state['usage'].get(k)) is int and state['usage'][k] >= 0
                        for k in ('input_tokens', 'output_tokens')), 'Missing Antigravity cumulative accounting')
    return value


def save(box, cli, session_id, path, *, antigravity_state=None):
    capability(cli)
    files, total = {}, 0
    for base, prefix in ((box.home / HOME_ROOTS[cli], 'native'), (box.work, 'workspace')):
        if not base.exists():
            continue
        require(not base.is_symlink(), 'Linked native state directory')
        for item in sorted(base.rglob('*')):
            # agy creates this runtime-log alias; it is never checkpointed or followed.
            if cli == 'antigravity' and prefix == 'native' and item == base / 'cli.log':
                continue
            require(not item.is_symlink(), 'Linked native state file')
            if item.is_dir():
                continue
            relative = item.relative_to(base).as_posix()
            if prefix == 'workspace' and relative in ('task.json', 'query.md', 'answer.md'):
                continue
            name = ('native/' + item.relative_to(box.home).as_posix() if prefix == 'native' else
                    relative if relative.startswith('artifacts/') else 'workspace/' + relative)
            if prefix == 'native' and not any(re.fullmatch(p, name[7:]) for p in PATTERNS[cli]):
                continue
            valid_name(name, cli)
            data = regular_bytes(item, MAX_TOTAL); total += len(data)
            require(total <= MAX_TOTAL and len(files) < MAX_FILES, 'Native continuation exceeds budget')
            files[name] = {'base64': base64.b64encode(data).decode('ascii'),
                           'sha256': hashlib.sha256(data).hexdigest()}
    value = {'schema_version': SCHEMA, 'cli': cli, 'session_id': session_id,
             'directory': box.root.name, 'temporary_parent': str(box.root.parent.resolve()), 'files': files}
    if cli == 'antigravity':
        value['antigravity_state'] = antigravity_state
    write(path, value)
    load(path)  # Refuse empty/non-resumable captures before declaring success.
    return {'sha256': digest(value), 'session_id': session_id,
            'artifacts': {n: r['sha256'] for n, r in files.items() if n.startswith('artifacts/')}}


def restore(box, path, cli):
    value = load(path)
    require(value['cli'] == cli and box.root.name == value['directory'],
            'Native checkpoint CLI or working directory changed')
    require(str(Path(tempfile.gettempdir()).resolve()) == value['temporary_parent'],
            'Native session requires its original temporary directory; no relocated-session fallback')
    for name, row in value['files'].items():
        parts = valid_name(name, cli)
        target = (box.home.joinpath(*parts[1:]) if parts[0] == 'native' else
                  box.work.joinpath(*parts[1:]) if parts[0] == 'workspace' else box.work / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        require(not target.exists() and not target.is_symlink(), 'Continuation destination already exists')
        target.write_bytes(base64.b64decode(row['base64'], validate=True)); target.chmod(0o600)
    return value['session_id']


def invocation(agent, binary, prompt, box, output, selected_env, session_id=None):
    from .cli_adapters import invocation as initial
    cli = agent['cli']; capability(cli)
    args, stdin, env = initial(agent, binary, prompt, box.work, box.work, output,
                               box=box, selected_env=selected_env)
    if cli == 'codex':
        args.remove('--ephemeral')
        if session_id is not None:
            index = args.index('--cd'); del args[index:index + 2]
            args.insert(2, 'resume'); args.insert(len(args) - 1, session_id)
    else:
        if cli == 'claude':
            args.remove('--no-session-persistence')
        if cli == 'qwen':
            args += ['--chat-recording', 'true']
        if session_id is not None:
            # DSH's trailing '-' is the stdin positional; put flags before it.
            at = len(args) - 1 if cli == 'dsh' else len(args)
            args[at:at] = [RESUME_FLAGS[cli], session_id]
    return args, stdin, env


def check(agent, binary):
    from .isolation import CandidateSandbox
    cli = agent['cli']; capability(cli)
    with CandidateSandbox(binary, loopback_listener=cli == 'antigravity',
                          system_user_lookup=cli == 'codex',
                          system_trust=cli == 'antigravity', allow_pty=cli == 'antigravity') as box:
        (box.work / 'task.json').write_text('{}')
        (box.work / 'query.md').write_text('Continuation capability check only')
        box.verify()
        flags = ['exec', 'resume', '--help'] if cli == 'codex' else (
            ['--profile', 'headless', '--help'] if cli == 'dsh' else ['--help'])
        result = box.run([binary, *flags], env={'DSH_HOME': str(box.home / '.dsh')}, timeout=30)
        expected = ('SESSION_ID', '--json', '--model') if cli == 'codex' else (RESUME_FLAGS[cli],)
        require(result.returncode == 0 and all(flag in result.stdout + result.stderr for flag in expected),
                'Pinned CLI lacks required native continuation flags: ' + cli)
    return capability(cli)
