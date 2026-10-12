"""Portable single/staged/episode runner. Contains no task bank or review material."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from . import cli_adapters, cli_setup, native_runtime, native_sessions
from .cli_common import digest, now, read, regular, require, result_exit_code, safe_id, sha, write
from .cli_packet_runner import RUNTIME as SINGLE_RUNTIME, local_registry
from .full_packet_io import export_archive
from .runtime_common import file_lock

VERSION = 'native-full-packet.1'
RUNTIME = (*SINGLE_RUNTIME, 'full_packet_io.py', 'full_packet_runner.py')
FILES = ('prompt.md', 'answer.md', 'final-response.md', 'events.jsonl', 'stderr.log',
         'execution.json', 'trace.jsonl', 'checkpoint.json')
TERMINAL = {'completed', 'failed', 'timeout', 'interrupted', 'invalid_output', 'model_mismatch'}


def unit_key(unit):
    require(unit['kind'] in ('single', 'staged', 'episode'), 'Unknown task kind')
    return unit['kind'] + '/' + safe_id(unit['id'])


def prompt_name(unit, stage):
    return 'questions/' + unit_key(unit) + '/' + stage['id'] + '.md'


def load_packet(packet):
    packet = Path(packet)
    manifest = read(regular(packet, 'packet.json')); registry = read(regular(packet, 'registry.json'))
    validate_definition(manifest, registry)
    require(set(manifest['runtime_sha256']) == {'run_agent.py', *(f'native_cli/{n}' for n in RUNTIME)},
            'Unexpected portable runtime files')
    for name, expected in manifest['runtime_sha256'].items():
        require(sha(regular(packet, name)) == expected, 'Portable runtime changed')
    for unit in manifest['units']:
        for stage in unit['stages']:
            require(sha(regular(packet, prompt_name(unit, stage))) == stage['prompt_sha256'],
                    'Portable stage prompt changed')
    return manifest, registry


def validate_definition(manifest, registry):
    require(isinstance(manifest, dict) and set(manifest) ==
            {'schema', 'registry_sha256', 'units', 'runtime_sha256', 'packet_id'}, 'Invalid full packet fields')
    require(manifest['schema'] == VERSION and manifest['packet_id'] ==
            digest({k: v for k, v in manifest.items() if k != 'packet_id'}), 'Full packet identity changed')
    cli_adapters.validate_registry(registry)
    require(len(registry['agents']) == 1 and registry['agents'][0]['cli_version']
            and digest(registry) == manifest['registry_sha256'], 'Frozen portable recipe changed')
    units = manifest['units']
    require(isinstance(units, list) and units and len({unit_key(u) for u in units}) == len(units),
            'Empty or duplicate portable tasks')
    for unit in units:
        require(set(unit) == {'id', 'kind', 'stages'}, 'Unexpected candidate task fields')
        stages = unit['stages']
        expected_count = 1 if unit['kind'] == 'single' else 2 if unit['kind'] == 'staged' else None
        require(isinstance(stages, list) and len(stages) >= 1
                and (len(stages) == expected_count if expected_count else len(stages) > 1)
                and [s['id'] for s in stages] == [f'S{i}' for i in range(len(stages))], 'Invalid stage sequence')
        for stage in stages:
            require(set(stage) == {'id', 'prompt_sha256', 'web_mode', 'require_answer_file'}
                    and type(stage['require_answer_file']) is bool, 'Unexpected candidate stage fields')
            native_runtime.network_policy(registry['agents'][0], stage['web_mode'])
    return manifest


def jobs(manifest, registry):
    return [(u, repeat, 'runs/' + unit_key(u) + f'/r{repeat}')
            for repeat in range(1, registry['repeats'] + 1) for u in manifest['units']]


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.utcoffset() is not None, 'Missing timestamp timezone')
    return parsed


def validate_stage(folder, unit, stage, repeat, manifest, registry, previous=None):
    meta = read(regular(folder, 'run.json')); agent = registry['agents'][0]
    expected = {'schema': VERSION, 'packet_id': manifest['packet_id'], 'registry_sha256': digest(registry),
                'unit_id': unit['id'], 'kind': unit['kind'], 'stage_id': stage['id'], 'repeat': repeat,
                'prompt_sha256': stage['prompt_sha256'], 'agent': agent['id'], 'model': agent['model'],
                'cli_version': agent['cli_version'], 'stage_timeout_seconds': registry['timeout_seconds']}
    require(all(meta.get(k) == v for k, v in expected.items()), 'Returned stage identity changed')
    require(meta.get('status') in TERMINAL, 'Unresolved running stage; never automatically retry it')
    require(type(meta.get('elapsed_seconds')) in (int, float) and math.isfinite(meta['elapsed_seconds'])
            and meta['elapsed_seconds'] >= 0, 'Invalid stage duration')
    require(type(meta.get('allocated_seconds')) in (int, float) and math.isfinite(meta['allocated_seconds'])
            and 0 < meta['allocated_seconds'] <= registry['timeout_seconds'], 'Invalid allocated stage budget')
    require(timestamp(meta['finished_at']) >= timestamp(meta['started_at']), 'Reversed stage timestamps')
    previous_sha = sha(previous) if previous is not None else None
    require(meta.get('previous_checkpoint_sha256') == previous_sha, 'Stage checkpoint chain changed')
    files = meta.get('files')
    allowed = set(FILES) - ({'checkpoint.json'} if unit['kind'] == 'single' else set())
    require(isinstance(files, dict) and set(files) <= allowed
            and files.get('prompt.md') == stage['prompt_sha256'], 'Unexpected stage files or changed prompt')
    require({p.name for p in folder.iterdir()} == set(files) | {'run.json'}, 'Undeclared or missing stage capture')
    for name, expected_hash in files.items():
        require(sha(regular(folder, name)) == expected_hash, 'Returned stage file changed: ' + name)
    require({'execution.json', 'trace.jsonl'} <= set(files), 'Missing stage execution evidence')
    evidence = read(regular(folder, 'execution.json'))
    rejection = meta.get('runner_rejection')
    require(rejection in (None, 'duplicate_session', 'missing_session'), 'Unknown runner rejection')
    require(evidence.get('schema') == 'native-execution.1'
            and evidence.get('cli') == agent['cli'] and evidence.get('cli_version') == agent['cli_version']
            and evidence.get('requested_model') == agent['model'] and evidence.get('web_mode') == stage['web_mode']
            and evidence.get('continuation_input') == previous_sha
            and evidence.get('isolation') == meta.get('isolation')
            and evidence.get('auth_mode') == meta.get('auth_mode') == cli_adapters.native_auth.auth_mode(agent)
            and evidence.get('credentials_included') is False
            and evidence.get('status') == ('completed' if rejection else meta['status']),
            'Inconsistent submitted execution evidence')
    if rejection:
        require(meta['status'] == 'invalid_output' and meta.get('session_id') is None, 'Invalid rejected stage')
    if meta['status'] == 'completed':
        require({'answer.md', 'events.jsonl'} <= set(files) and meta.get('exit_code') == 0
                and isinstance(meta.get('session_id'), str) and meta['session_id'].strip(), 'Missing successful capture')
        require(evidence['isolation'].get('status') == 'passed'
                and evidence['isolation'].get('cleanup') == 'complete', 'Missing isolation/cleanup evidence')
        if evidence.get('answer_capture') is not None:
            require('final-response.md' in files and evidence['answer_capture'] == meta.get('answer_capture'),
                    'Missing answer provenance')
            require(not stage['require_answer_file'] or evidence['answer_capture']['source'] == 'workspace_file',
                    'Required answer.md was not delivered')
        prior = native_sessions.load(previous) if previous is not None else None
        result = native_runtime.parse_capture(agent['cli'], folder / 'events.jsonl', folder / 'answer.md', evidence,
                                              previous_antigravity=prior.get('antigravity_state') if prior else None)
        require(result.pop('answer') == (folder / 'answer.md').read_text(encoding='utf-8')
                and all(meta.get(k) == v for k, v in result.items()), 'Answer/event metadata differs')
        require(not result['reported_models'] or result['reported_models'] == [agent['model']], 'Returned model differs')
        if unit['kind'] != 'single':
            require('checkpoint.json' in files, 'Missing continuation checkpoint')
            checkpoint = native_sessions.load(regular(folder, 'checkpoint.json'))
            require(checkpoint['cli'] == agent['cli'] and checkpoint['session_id'] == meta['session_id']
                    and meta.get('continuation', {}).get('sha256') == digest(checkpoint), 'Checkpoint identity changed')
            if prior is not None:
                require(all(checkpoint[k] == prior[k] for k in ('cli', 'session_id', 'directory', 'temporary_parent')),
                        'Native session changed between stages')
            if unit['kind'] == 'episode':
                require('artifacts/action-record.md' in checkpoint['files'], 'Missing episode action record')
    return meta


def inspect_results(out, manifest, registry):
    out = Path(out)
    require(read(regular(out, 'packet.json')) == manifest and read(regular(out, 'registry.json')) == registry,
            'Returned packet or recipe differs')
    batch = read(regular(out, 'batch.json'))
    planned = jobs(manifest, registry)
    require(batch.get('schema') == VERSION and batch.get('packet_id') == manifest['packet_id']
            and set(batch.get('jobs', {})) == {p for _, _, p in planned}, 'Missing or added planned jobs')
    records, names, sessions = [], ['packet.json', 'registry.json', 'batch.json'], set()
    for unit, repeat, relative in planned:
        folder = out / relative; marker = batch['jobs'][relative]
        require(set(marker) == {'stages', 'status'}, 'Invalid batch job marker')
        stages = marker['stages']
        require(stages == [s['id'] for s in unit['stages'][:len(stages)]], 'Stage attempts out of order')
        captures = []; previous = None; spent = 0; conversation = None
        for i, sid in enumerate(stages):
            stage_folder = folder / 'stages' / sid
            regular(out, relative + '/stages/' + sid + '/run.json')
            meta = validate_stage(stage_folder, unit, unit['stages'][i], repeat, manifest, registry, previous)
            require(not captures or captures[-1]['status'] == 'completed', 'Later stage released after failure')
            remaining = registry['timeout_seconds'] * len(unit['stages']) - spent
            require(remaining > 0 and meta['allocated_seconds'] == min(registry['timeout_seconds'], remaining),
                    'Episode total budget changed')
            if captures:
                require(timestamp(meta['started_at']) >= timestamp(captures[-1]['finished_at']), 'Stage timeline changed')
            if meta['status'] == 'completed':
                if conversation is None:
                    conversation = meta['session_id']
                    require(conversation not in sessions, 'Native session reused across tasks/repeats')
                else:
                    require(meta['session_id'] == conversation, 'Native session changed between stages')
            if meta.get('runner_rejection') == 'duplicate_session':
                require(meta.get('duplicate_session_id') in sessions, 'Unsubstantiated duplicate-session rejection')
            captures.append(meta); spent += meta['elapsed_seconds']
            previous = stage_folder / 'checkpoint.json'
            names += [relative + '/stages/' + sid + '/' + n for n in ('run.json', *meta['files'])]
        if conversation:
            sessions.add(conversation)
        status = ('completed' if len(captures) == len(unit['stages']) and captures[-1]['status'] == 'completed'
                  else captures[-1]['status'] if captures and captures[-1]['status'] != 'completed'
                  else 'budget_exhausted' if spent >= registry['timeout_seconds'] * len(unit['stages'])
                  else 'partial' if captures else 'not_run')
        require(marker['status'] == status, 'Batch status differs from recorded stages')
        if folder.exists():
            require(not folder.is_symlink() and {p.name for p in folder.iterdir()} == {'stages'}, 'Unexpected task capture files')
            require({p.name for p in (folder / 'stages').iterdir()} == set(stages), 'Unrecorded stage directory')
        else:
            require(not stages, 'Planned capture is missing')
        records.append({'unit_id': unit['id'], 'kind': unit['kind'], 'repeat': repeat, 'path': relative,
                        'status': status, 'completed_stages': sum(m['status'] == 'completed' for m in captures),
                        'total_stages': len(unit['stages']), 'captures': captures})
    return records, names


def summary(records, manifest, registry):
    clean = [{k: v for k, v in r.items() if k != 'captures'} for r in records]
    complete = sum(r['status'] == 'completed' for r in records)
    return {'schema': VERSION, 'packet_id': manifest['packet_id'], 'agent': registry['agents'][0]['id'],
            'official': False, 'scored': False, 'expected': len(records), 'completed': complete,
            'status': 'completed' if complete == len(records) else 'incomplete', 'records': clean,
            'exit_code': 130 if any(r['status'] == 'interrupted' for r in records) else int(complete != len(records))}


def report(out, manifest, registry):
    records, names = inspect_results(out, manifest, registry)
    value = summary(records, manifest, registry); write(out / 'summary.json', value)
    archive = out.with_name(out.name + '-' + uuid.uuid4().hex[:8] + '.zip')
    export_archive(out, [*names, 'summary.json'], archive, manifest['packet_id'])
    return dict(value, results_zip=str(archive))


def capture_stage(packet, out, unit, stage, repeat, manifest, registry, agent, binary, previous, remaining):
    out.mkdir(parents=True)
    shutil.copyfile(regular(packet, prompt_name(unit, stage)), out / 'prompt.md')
    allocated = min(registry['timeout_seconds'], remaining)
    meta = {'schema': VERSION, 'packet_id': manifest['packet_id'], 'registry_sha256': digest(registry),
            'unit_id': unit['id'], 'kind': unit['kind'], 'stage_id': stage['id'], 'repeat': repeat,
            'prompt_sha256': stage['prompt_sha256'], 'agent': agent['id'], 'model': agent['model'],
            'cli_version': agent['cli_version'], 'stage_timeout_seconds': registry['timeout_seconds'],
            'allocated_seconds': allocated, 'previous_checkpoint_sha256': sha(previous) if previous else None,
            'status': 'running', 'started_at': now(), 'official': False}
    write(out / 'run.json', meta); began = time.monotonic()
    multi = unit['kind'] != 'single'
    meta.update(native_runtime.capture(agent, binary, (out / 'prompt.md').read_text(encoding='utf-8'),
        stage['web_mode'], allocated, out / 'events.jsonl', out / 'stderr.log', out / 'answer.md',
        out / 'execution.json', out / 'trace.jsonl', previous_checkpoint=previous,
        checkpoint=out / 'checkpoint.json' if multi else None, require_artifact=unit['kind'] == 'episode',
        require_answer_file=stage['require_answer_file']))
    meta.update(elapsed_seconds=time.monotonic() - began, finished_at=now())
    meta['files'] = {n: sha(regular(out, n)) for n in FILES if (out / n).exists()}
    write(out / 'run.json', meta)
    return meta


def run_packet(packet, out, execute=False, resume=False, max_stages=None):
    packet, out = Path(packet).resolve(), Path(out).resolve()
    manifest, registry = load_packet(packet)
    require(not resume or execute, '--resume requires --execute')
    require(max_stages is None or type(max_stages) is int and max_stages > 0, 'max-stages must be positive')
    if not execute:
        return {'status': 'preview_no_model_called', 'agent': registry['agents'][0]['id'], 'official': False,
                'attempts': len(jobs(manifest, registry)), 'stages': registry['repeats'] *
                sum(len(u['stages']) for u in manifest['units']), 'out': str(out)}
    require(not out.is_relative_to(packet / '.local'), 'Answers cannot overlap local credentials')
    require(not out.exists() or resume, 'Answer output exists; use --resume or a new directory')
    with file_lock(out.parent / ('.' + out.name + '.lock'), blocking=False, busy_message='Answer batch already running'):
        existing = []
        if out.exists():
            existing, _ = inspect_results(out, manifest, registry)  # Before auth checks or inference.
        local_path, local = local_registry(packet, registry); agent = local['agents'][0]
        with cli_setup.credential_environment(local, local_path) as sources, tempfile.TemporaryDirectory(prefix='full-packet-check-') as work:
            checks = cli_adapters.check_agent(agent, work, credential_source=sources[agent['id']])
            require(not checks['issues'], 'CLI setup incomplete; run python3 run_agent.py setup')
            if any(u['kind'] != 'single' for u in manifest['units']):
                native_sessions.check(agent, checks['binary'])
            if not out.exists():
                out.mkdir(parents=True)
                write(out / 'packet.json', manifest); write(out / 'registry.json', registry)
                write(out / 'batch.json', {'schema': VERSION, 'packet_id': manifest['packet_id'], 'created_at': now(),
                    'jobs': {p: {'status': 'not_run', 'stages': []} for _, _, p in jobs(manifest, registry)}})
            batch = read(out / 'batch.json'); used = set()
            for row in existing:
                used.update(m['session_id'] for m in row['captures'] if m.get('session_id') and m['status'] == 'completed')
            count = 0
            for unit, repeat, relative in jobs(manifest, registry):
                marker = batch['jobs'][relative]
                if marker['status'] not in ('not_run', 'partial'):
                    continue  # Completed, failed and uncertain attempts are never retried.
                first = len(marker['stages']); folder = out / relative
                prior = [read(folder / 'stages' / sid / 'run.json') for sid in marker['stages']]
                spent = sum(m['elapsed_seconds'] for m in prior)
                for i in range(first, len(unit['stages'])):
                    if max_stages is not None and count >= max_stages:
                        return report(out, manifest, registry)
                    remaining = registry['timeout_seconds'] * len(unit['stages']) - spent
                    if remaining <= 0:
                        marker['status'] = 'budget_exhausted'; write(out / 'batch.json', batch); break
                    stage = unit['stages'][i]; sid = stage['id']
                    marker['stages'].append(sid); marker['status'] = 'running'; write(out / 'batch.json', batch)
                    previous = folder / 'stages' / f'S{i-1}' / 'checkpoint.json' if i else None
                    print(f"Running {agent['id']} / {unit_key(unit)} / r{repeat} / {sid}", file=sys.stderr, flush=True)
                    meta = capture_stage(packet, folder / 'stages' / sid, unit, stage, repeat,
                                         manifest, registry, agent, checks['binary'], previous, remaining)
                    if meta['status'] == 'completed' and (not meta.get('session_id') or i == 0 and meta['session_id'] in used):
                        reason = 'duplicate_session' if meta.get('session_id') else 'missing_session'
                        meta.update(status='invalid_output', error=reason, runner_rejection=reason,
                                    duplicate_session_id=meta.get('session_id'), session_id=None)
                        write(folder / 'stages' / sid / 'run.json', meta)
                    elif meta['status'] == 'completed' and i == 0:
                        used.add(meta['session_id'])
                    spent += meta['elapsed_seconds']; count += 1
                    marker['status'] = ('completed' if i == len(unit['stages']) - 1 else
                                        'budget_exhausted' if spent >= registry['timeout_seconds'] * len(unit['stages'])
                                        else 'partial') if meta['status'] == 'completed' else meta['status']
                    write(out / 'batch.json', batch)
                    if meta['status'] == 'interrupted':
                        return report(out, manifest, registry)
                    if meta['status'] != 'completed':
                        break
            return report(out, manifest, registry)


def main(argv=None, packet=None):
    packet = Path(packet or Path(__file__).resolve().parent.parent)
    parser = argparse.ArgumentParser(description='完整答题包：单阶段、两阶段与连续任务；仅 --execute 调用模型')
    parser.add_argument('action', nargs='?', choices=['run', 'setup', 'check', 'export'], default='run')
    parser.add_argument('--out', type=Path, default=Path('answers'))
    parser.add_argument('--execute', action='store_true'); parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-stages', type=int)
    parser.add_argument('--non-interactive', action='store_true'); parser.add_argument('--import-credentials', type=Path)
    args = parser.parse_args(argv); manifest, registry = load_packet(packet)
    if args.action == 'setup':
        return cli_setup.setup(packet / 'registry.json', packet / '.local/registry.json',
            interactive=cli_setup.interactive_terminal(args.non_interactive), frozen=True, import_path=args.import_credentials)
    if args.action == 'export':
        out = args.out.resolve()
        with file_lock(out.parent / ('.' + out.name + '.lock'), blocking=False, busy_message='Answer batch is running'):
            return report(out, manifest, registry)
    if args.action == 'check':
        path, local = local_registry(packet, registry)
        with cli_setup.credential_environment(local, path) as sources, tempfile.TemporaryDirectory(prefix='full-packet-check-') as work:
            check = cli_adapters.check_agent(local['agents'][0], work, check_network=True,
                                            credential_source=sources[local['agents'][0]['id']])
            if not check['issues'] and any(u['kind'] != 'single' for u in manifest['units']):
                native_sessions.check(local['agents'][0], check['binary'])
        return {'status': 'setup_required' if check['issues'] else 'ready_for_trial', 'agents': [check], 'model_calls': 0}
    return run_packet(packet, args.out, args.execute, args.resume, args.max_stages)


def entrypoint(packet):
    try:
        result = main(packet=packet); print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(result_exit_code(result))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print('Error: ' + str(error), file=sys.stderr); sys.exit(2)
