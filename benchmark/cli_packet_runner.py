"""Portable answer-only native CLI runner. No organizer or scoring imports."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from . import cli_adapters, cli_setup, native_runtime
from .cli_common import digest, now, read, regular, require, result_exit_code, safe_id, sha, stop_owned, write
from .runtime_common import file_lock

VERSION = 'native-cli-packet.2'
RUNTIME = ('__init__.py', 'runtime_common.py', 'cli_common.py', 'credentials.py', 'cli_adapters.py', 'cli_setup.py',
           'cli_packet_runner.py', 'native_runtime.py', 'native_auth.py', 'native_sessions.py', 'isolation.py', 'network.py')
FILES = ('prompt.md', 'answer.md', 'final-response.md', 'events.jsonl', 'stderr.log', 'execution.json', 'trace.jsonl')
TERMINAL = {'completed', 'failed', 'timeout', 'interrupted', 'invalid_output', 'model_mismatch'}


def load_packet(packet):
    packet = Path(packet)
    manifest = read(regular(packet, 'packet.json'))
    require(manifest.get('schema') == VERSION, 'Unsupported answer packet')
    require(manifest.get('packet_id') == digest({k: v for k, v in manifest.items() if k != 'packet_id'}),
            'Packet manifest changed')
    registry = read(regular(packet, 'registry.json'))
    cli_adapters.validate_registry(registry)
    require(len(registry['agents']) == 1 and registry['agents'][0]['cli_version'],
            'Each answer packet needs one agent and a pinned CLI version')
    require(digest(registry) == manifest['registry_sha256'], 'Frozen model recipe changed')
    expected = {'run_agent.py', *(f'native_cli/{n}' for n in RUNTIME)}
    require(set(manifest['runtime_sha256']) == expected, 'Unexpected runner files')
    for name, expected_hash in manifest['runtime_sha256'].items():
        require(sha(regular(packet, name)) == expected_hash, 'Packet runner changed')
    tasks = manifest['tasks']
    require(isinstance(tasks, list) and tasks and len({t['id'] for t in tasks}) == len(tasks),
            'Empty or duplicate packet tasks')
    for task in tasks:
        safe_id(task['id'])
        native_runtime.network_policy(registry['agents'][0], task.get('web_mode'))
        require(sha(regular(packet, 'questions/' + task['id'] + '.md')) == task['prompt_sha256'],
                'Packet question changed')
    return manifest, registry


def local_registry(packet, frozen):
    path = Path(packet) / '.local/registry.json'
    require(path.is_file(), 'Run python3 run_agent.py setup on this computer first')
    local = read(regular(Path(packet), '.local/registry.json'))
    cli_adapters.validate_registry(local)
    normalized = dict(local, agents=[dict(a, binary=b['binary'])
                       for a, b in zip(local['agents'], frozen['agents'])])
    require(len(local['agents']) == 1 and normalized == frozen,
            'Local setup may change only CLI path; model/version/effort are frozen')
    return path, local


def validate_attempt(folder, task, registry, manifest, repeat):
    folder = Path(folder)
    meta = read(regular(folder, 'run.json'))
    agent = registry['agents'][0]
    expected = {'packet_id': manifest['packet_id'], 'task_id': task['id'],
                'prompt_sha256': task['prompt_sha256'], 'registry_sha256': digest(registry),
                'agent': agent['id'], 'model': agent['model'], 'cli_version': agent['cli_version'],
                'repeat': repeat, 'timeout_seconds': registry['timeout_seconds']}
    require(all(meta.get(k) == v for k, v in expected.items()), 'Returned run identity changed')
    require(meta.get('status') in TERMINAL, 'Unfinished attempt; inspect process and preserve its record')
    files = meta.get('files')
    require(isinstance(files, dict) and set(files) <= set(FILES), 'Unexpected returned files')
    require(files.get('prompt.md') == task['prompt_sha256'], 'Captured prompt changed')
    for name, expected_hash in files.items():
        require(sha(regular(folder, name)) == expected_hash, 'Returned file changed: ' + name)
    require(type(meta.get('elapsed_seconds')) in (int, float) and meta['elapsed_seconds'] >= 0,
            'Missing elapsed time')
    if meta['status'] == 'completed':
        require({'answer.md', 'events.jsonl', 'execution.json', 'trace.jsonl'} <= set(files) and meta.get('exit_code') == 0,
                'Missing successful capture')
        evidence = read(regular(folder, 'execution.json'))
        require(evidence.get('schema') == 'native-execution.1' and evidence.get('isolation') == meta.get('isolation')
                and evidence.get('auth_mode') == meta.get('auth_mode') == cli_adapters.native_auth.auth_mode(agent)
                and evidence['isolation'].get('status') == 'passed' and evidence['isolation'].get('cleanup') == 'complete',
                'Missing or inconsistent submitted execution evidence')
        if evidence.get('answer_capture') is not None:
            require('final-response.md' in files and evidence['answer_capture'] == meta.get('answer_capture')
                    and evidence['answer_capture']['source'] == 'workspace_file', 'Missing answer capture provenance')
        result = native_runtime.parse_capture(agent['cli'], folder / 'events.jsonl', folder / 'answer.md', evidence)
        require(result.pop('answer') == (folder / 'answer.md').read_text(encoding='utf-8'), 'Answer/event mismatch')
        require(all(meta.get(k) == v for k, v in result.items()), 'Returned metadata/event mismatch')
        require(not result['reported_models'] or result['reported_models'] == [agent['model']],
                'Returned model mismatch')
    return meta


def attempt(packet, task, registry, manifest, agent, binary, repeat, folder):
    folder.mkdir(parents=True)
    shutil.copyfile(packet / 'questions' / (task['id'] + '.md'), folder / 'prompt.md')
    meta = {'packet_id': manifest['packet_id'], 'registry_sha256': digest(registry),
            'task_id': task['id'], 'prompt_sha256': task['prompt_sha256'], 'agent': agent['id'],
            'model': agent['model'], 'cli_version': agent['cli_version'], 'repeat': repeat,
            'timeout_seconds': registry['timeout_seconds'], 'status': 'running', 'started_at': now(),
            'exit_code': None, 'session_id': None, 'reported_models': [], 'usage': None,
            'model_identity_status': 'not_reported', 'error': None, 'official': False,
            'isolation_verified': False, 'host_configuration_verified': False}
    write(folder / 'run.json', meta)
    started = time.monotonic()
    prompt = (folder / 'prompt.md').read_text(encoding='utf-8')
    meta.update(native_runtime.capture(agent, binary, prompt, task['web_mode'], registry['timeout_seconds'],
        folder / 'events.jsonl', folder / 'stderr.log', folder / 'answer.md',
        folder / 'execution.json', folder / 'trace.jsonl'))
    meta['elapsed_seconds'] = round(time.monotonic() - started, 3)
    meta['finished_at'] = now()
    meta['files'] = {n: sha(folder / n) for n in FILES if (folder / n).is_file() and not (folder / n).is_symlink()}
    write(folder / 'run.json', meta)
    return meta


def jobs(manifest, registry):
    return [(t, repeat, f"runs/{t['id']}/r{repeat}")
            for repeat in range(1, registry['repeats'] + 1) for t in manifest['tasks']]


def inspect_results(out, manifest, registry):
    records, sessions = [], set()
    for task, repeat, relative in jobs(manifest, registry):
        folder = Path(out) / relative
        if folder.exists() or folder.is_symlink():
            # Check from the results root too, so a symlinked runs/task parent is refused.
            regular(out, relative + '/run.json')
            meta = validate_attempt(folder, task, registry, manifest, repeat)
            session = meta.get('session_id')
            if session:
                require(session not in sessions, 'Session reused between questions')
                sessions.add(session)
        else:
            meta = {'status': 'not_run'}
        records.append({'task_id': task['id'], 'repeat': repeat, 'path': relative, 'status': meta['status']})
    return records


def report(out, manifest, registry):
    records = inspect_results(out, manifest, registry)
    result = {'packet_id': manifest['packet_id'], 'agent': registry['agents'][0]['id'],
              'official': False, 'scored': False, 'records': records, 'expected': len(records),
              'completed': sum(r['status'] == 'completed' for r in records)}
    write(out / 'summary.json', result)
    archive = out.with_name(out.name + '-' + uuid.uuid4().hex[:8] + '.zip')
    # Explicit allowlist: never zip local settings, credentials, home directories or runtime cache.
    names = ['packet.json', 'registry.json', 'batch.json', 'summary.json']
    for row in records:
        if row['status'] != 'not_run':
            meta = read(out / row['path'] / 'run.json')
            names += [row['path'] + '/' + n for n in ('run.json', *meta['files'])]
    with ZipFile(archive, 'x', ZIP_DEFLATED) as z:
        for name in names:
            z.write(regular(out, name), name)
    return dict(result, results_zip=str(archive))


def run_packet(packet, out, execute=False, resume=False):
    packet, out = Path(packet).resolve(), Path(out).resolve()
    manifest, registry = load_packet(packet)
    if not execute:
        return {'status': 'preview_no_model_called', 'agent': registry['agents'][0]['id'],
                'attempts': len(jobs(manifest, registry)), 'out': str(out), 'official': False}
    require(os.name == 'posix', 'Use macOS/Linux, or WSL on Windows')
    require(not out.exists() or resume, 'Output exists; use --resume or a new directory')
    local_path, local = local_registry(packet, registry)
    agent = local['agents'][0]
    with cli_setup.credential_environment(local, local_path) as sources, tempfile.TemporaryDirectory(prefix='benchmark-check-') as work:
        checks = cli_adapters.check_agent(agent, work, credential_source=sources[agent['id']])
        require(not checks['issues'], 'CLI setup incomplete; run python3 run_agent.py setup')
        binary = checks['binary']
        out.mkdir(parents=True, exist_ok=resume)
        with file_lock(out / '.lock', blocking=False, busy_message='Another process owns these answers'):
            if (out / 'batch.json').exists():
                require(read(regular(out, 'packet.json')) == manifest and
                        read(regular(out, 'registry.json')) == registry, 'Cannot resume a changed packet')
                require(read(regular(out, 'batch.json'))['packet_id'] == manifest['packet_id'], 'Batch identity changed')
            else:
                require(not any(p.name != '.lock' for p in out.iterdir()), 'Unrecognized answer directory')
                write(out / 'packet.json', manifest)
                write(out / 'registry.json', registry)
                write(out / 'batch.json', {'packet_id': manifest['packet_id'], 'created_at': now(),
                                         'runner_version': VERSION, 'official': False})
            existing = inspect_results(out, manifest, registry)  # Validate before any model request.
            sessions = {read(out / r['path'] / 'run.json').get('session_id')
                        for r in existing if r['status'] != 'not_run'} - {None}
            for task, repeat, relative in jobs(manifest, registry):
                folder = out / relative
                if folder.exists():
                    continue  # Failed attempts are retained too; never select best-of retries.
                print(f"Running {agent['id']} / {task['id']} / r{repeat}", flush=True)
                meta = attempt(packet, task, registry, manifest, agent, binary, repeat, folder)
                if meta.get('session_id') in sessions:
                    # Preserve the duplicate identity in evidence but stop before further calls.
                    meta.update(status='invalid_output', error='Session reused between questions',
                                duplicate_session_id=meta['session_id'], session_id=None)
                    write(folder / 'run.json', meta)
                elif meta.get('session_id'):
                    sessions.add(meta['session_id'])
                print(task['id'] + ': ' + meta['status'], flush=True)
                if meta['status'] != 'completed':
                    break
            return report(out, manifest, registry)


def main(argv=None, packet=None):
    packet = Path(packet or Path(__file__).resolve().parent.parent)
    p = argparse.ArgumentParser(description='独立答题包：setup 配置本机；默认预览；--execute 才调用模型')
    p.add_argument('action', nargs='?', choices=['run', 'setup', 'check'], default='run')
    p.add_argument('--out', type=Path, default=Path('answers'))
    p.add_argument('--execute', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--non-interactive', action='store_true')
    p.add_argument('--import-credentials', type=Path)
    a = p.parse_args(argv)
    manifest, registry = load_packet(packet)
    if a.action == 'setup':
        return cli_setup.setup(packet / 'registry.json', packet / '.local/registry.json',
                               interactive=cli_setup.interactive_terminal(a.non_interactive), frozen=True,
                               import_path=a.import_credentials)
    if a.action == 'check':
        path, local = local_registry(packet, registry)
        with cli_setup.credential_environment(local, path) as sources, tempfile.TemporaryDirectory(prefix='benchmark-check-') as work:
            checks = cli_adapters.check_agent(local['agents'][0], work, check_network=True,
                                              credential_source=sources[local['agents'][0]['id']])
        return {'status': 'setup_required' if checks['issues'] else 'ready_for_trial', 'agents': [checks], 'model_calls': 0}
    return run_packet(packet, a.out, a.execute, a.resume)


def entrypoint(packet):
    try:
        result = main(packet=packet)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(result_exit_code(result))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Error messages never include credential values or full provider responses.
        print('Error: ' + str(error), file=sys.stderr)
        sys.exit(2)
