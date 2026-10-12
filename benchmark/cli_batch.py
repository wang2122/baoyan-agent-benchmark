"""Opt-in native CLI batches, reusing the existing frozen run/judge/score contract."""
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import cli_adapters, native_runtime
from .core import candidate_prompt, digest, file_hash, read, require
from .cli_common import now, stop_owned, write
from .packaging import judge_package
from .runner import prepare

# Atomic metadata writes make interrupted batches distinguishable from unattempted jobs.
VERSION = 'native-cli-runner.2'
TERMINAL = {'completed', 'failed', 'timeout', 'interrupted', 'invalid_output', 'model_mismatch'}


def source_identity():
    directory = Path(__file__).parent
    return digest({name: file_hash(directory / name) for name in
                   ('cli_batch.py', 'cli_adapters.py', 'cli_common.py', 'runtime_common.py', 'runner.py',
                    'core.py', 'credentials.py', 'packaging.py', 'metrics.py', 'native_auth.py', 'native_runtime.py', 'isolation.py', 'network.py')})


def inspect_agents(agents, *, check_network=False, credential_sources=None):
    reports = []
    with tempfile.TemporaryDirectory(prefix='benchmark-cli-check-') as work:
        for agent in agents:
            reports.append(cli_adapters.check_agent(agent, work, check_network=check_network,
                credential_source=(credential_sources or {}).get(agent['id'])))
    return {'status': 'ready_for_trial' if all(not r['issues'] for r in reports) else 'setup_required',
            'agents': reports, 'model_calls': 0, 'official': False,
            'network_checked': check_network and all('network' in r for r in reports),
            'note': 'Local boundary probes, sandboxed CLI startup/flags and selected credential availability checked; account entitlements, inference and tool behavior unverified.'}


def recipe_identity(agent):
    return digest({'runner': VERSION, 'source_sha256': source_identity(), 'agent': agent})


def attempt(task, rubric, protocol, agent, binary, repeat, timeout, out, prepared=False):
    run = Path(out) if prepared else prepare(task, rubric, out, protocol)
    work = run / 'workspace'
    meta = read(run / 'run.json')
    meta.update(adapter=agent['id'], adapter_version=recipe_identity(agent), baseline_kind='agent',
                model=agent['model'], repeat=repeat, timeout_seconds=timeout,
                cli=agent['cli'], cli_version=agent['cli_version'], execution_config=agent,
                model_identity_status='not_reported', reported_models=[], session_id=None,
                host_configuration_verified=False, status='running', started_at=now(),
                usage=None, usage_status='missing', trajectory_status='missing', error=None)
    write(run / 'run.json', meta)
    start = time.monotonic()
    meta.update(native_runtime.capture(agent, binary, candidate_prompt(task), task['execution']['web_mode'],
        timeout, work / 'trajectory.jsonl', run / 'cli.stderr.log', work / 'answer.md',
        run / 'execution.json', run / 'trace.jsonl'))
    meta['elapsed_seconds'] = round(time.monotonic() - start, 3)
    meta['finished_at'] = now()
    if (work / 'trajectory.jsonl').is_file():
        meta['trajectory_status'] = 'recorded_unverified'
    if meta.get('usage') is not None:
        meta['usage_status'] = 'cli_reported_unverified'
        write(work / 'usage.json', meta['usage'])
    answer = work / 'answer.md'
    meta['answer_file_sha256'] = file_hash(answer) if answer.is_file() else None
    paths = ('workspace/query.md', 'workspace/task.json', 'workspace/answer.md', 'workspace/final-response.md',
             'workspace/trajectory.jsonl', 'workspace/usage.json', 'cli.stderr.log', 'execution.json', 'trace.jsonl')
    meta['artifact_sha256'] = {p: file_hash(run / p) for p in paths if (run / p).is_file()}
    write(run / 'run.json', meta)
    return meta


def validate_saved(run, task, rubric, protocol, agent, repeat, timeout):
    require(not run.is_symlink() and (run / 'run.json').is_file(), 'Incomplete run directory; inspect before continuing')
    meta = read(run / 'run.json')
    require(meta['status'] in TERMINAL, 'Unresolved running/prepared attempt; inspect process and preserve its record')
    expected = {'task_id': task['id'], 'task_sha256': digest(task), 'rubric_sha256': digest(rubric),
                'protocol_sha256': digest(protocol), 'adapter': agent['id'], 'adapter_version': recipe_identity(agent),
                'model': agent['model'], 'repeat': repeat, 'timeout_seconds': timeout, 'execution_config': agent}
    require(all(meta.get(k) == v for k, v in expected.items()), 'Saved run identity changed')
    for name, value in (('task', task), ('rubric', rubric), ('protocol', protocol)):
        path = run / (name + '.json')
        require(path.is_file() and not path.is_symlink() and read(path) == value, 'Frozen run snapshot changed')
    hashes = meta.get('artifact_sha256')
    require(isinstance(hashes, dict) and 'workspace/query.md' in hashes and 'workspace/task.json' in hashes,
            'Missing captured artifact hashes')
    for relative, sha in hashes.items():
        path = run / relative
        require(path.resolve().is_relative_to(run.resolve()) and path.is_file() and not path.is_symlink()
                and file_hash(path) == sha, 'Captured run artifact changed')
    if meta['status'] == 'completed':
        require(meta['answer_file_sha256'] and meta['answer_file_sha256'] == hashes.get('workspace/answer.md'),
                'Completed run is missing its original answer')
    return meta
