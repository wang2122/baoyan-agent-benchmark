"""Opt-in same-session episodes using the existing isolated native capture path."""
from pathlib import Path
import tempfile
import time
import uuid
import sys

from . import cli_adapters, cli_setup, native_runtime
from .core import digest, file_hash, read, require, write
from .runtime_common import file_lock, now
from .workflow_versions import components, component_files
from .longitudinal import SCHEMA, load_run, record_answer, release, snapshot

SCHEMA_EXECUTION = 'longitudinal-execution.1'
ARTIFACT_INSTRUCTION = (
    '\n\n连续执行交付要求：将本轮更新后的行动记录保存为 artifacts/action-record.md；'
    '可在 artifacts/ 下保存项目表、来源清单和时间安排。沿用相对路径，后续轮读取并修订这些文件。'
    '在最终回复中完整说明本轮结论与修改依据，不自评分。工作目录可能迁移，只能使用本轮工作目录。'
)


def identity(root, agent, timeout, snap):
    return {'schema_version': SCHEMA_EXECUTION, 'agent': agent, 'stage_timeout_seconds': timeout,
            'episode_timeout_seconds': timeout * len(snap['stages']),
            'snapshot_sha256': digest(snap), 'components': components(root, 'native_cli'),
            'episode_components': component_files(root, ['benchmark/' + name for name in
                ('episode_runner.py', 'episode_state.py', 'native_sessions.py', 'longitudinal.py', 'task_sources.py')]),
            'attempt_policy': 'one_attempt_per_stage_no_automatic_retry',
            'aggregation': 'unique_source_macro_mean.1', 'domain': 'baoyan', 'official': False}


def check_continuation(agent, binary):
    from .native_sessions import check
    return check(agent, binary)


def verify_attempts(out, execution, state):
    require(execution.get('contract_sha256') == digest(execution['contract'])
            == state.get('execution_contract_sha256'), 'Episode execution binding changed')
    contract = execution['contract']
    require(contract['snapshot_sha256'] == state['snapshot_sha256']
            and contract['aggregation'] == state.get('aggregation')
            and contract['agent']['id'] == state['product']
            and contract['agent']['model'] == state['model'], 'Episode state differs from its execution contract')
    attempts = execution['attempts']
    require(len(attempts) <= len(state['snapshot']['stages']), 'Too many stage attempts')
    completed = []
    for i, row in enumerate(attempts):
        require(row['stage_id'] == f'S{i}', 'Stage attempts out of order')
        require(row['status'] != 'running', 'Interrupted capture has uncertain outcome; preserve it and start a new episode')
        require(i == len(attempts) - 1 or row['status'] == 'completed', 'Stage released after a failed attempt')
        require(isinstance(row.get('files'), dict) and row['files'], 'Missing stage capture fingerprints')
        stage = out / 'stages' / row['stage_id']
        require(set(row['files']) <= {'answer.md', 'final-response.md', 'events.jsonl', 'stderr.log', 'execution.json', 'trace.jsonl', 'checkpoint.json'},
                'Unexpected stage capture file')
        for name, expected in row['files'].items():
            path = stage / name
            require(path.is_file() and not path.is_symlink() and file_hash(path) == expected, 'Stage capture changed')
        if row['status'] == 'completed':
            expected_files = {'answer.md', 'events.jsonl', 'stderr.log', 'execution.json', 'trace.jsonl', 'checkpoint.json'}
            if read(stage / 'execution.json').get('answer_capture') is not None:
                expected_files.add('final-response.md')
            require(set(row['files']) == expected_files,
                    'Incomplete stage checkpoint')
            from .episode_state import load_checkpoint
            checkpoint = load_checkpoint(stage / 'checkpoint.json')
            require(checkpoint['session_id'] == state['conversation_id'], 'Native episode session changed')
            if state['snapshot'].get('kind') != 'staged':
                require('artifacts/action-record.md' in checkpoint['files'], 'Missing mandatory action record')
            completed.append(row)
    require(len(completed) == len(state['records']), 'Native captures and recorded answers differ')
    for row, record in zip(completed, state['records']):
        import base64
        require((out / 'stages' / row['stage_id'] / 'answer.md').read_bytes()
                == base64.b64decode(record['answer_base64'], validate=True), 'Captured answer differs from review record')
        require(record['receipt'].get('execution_evidence_sha256') == row['files']['execution.json'],
                'Execution evidence differs from receipt')


def report(out, execution, state):
    done = len(state['records']); total = len(state['snapshot']['stages'])
    attempts = execution['attempts']
    terminal = execution.get('terminal_status') or (attempts[-1]['status'] if attempts else None)
    status = 'completed' if done == total else terminal if terminal not in (None, 'completed') else 'partial'
    return {'status': status, 'run_dir': str(out), 'episode': state['snapshot']['episode']['id'],
            'sample_unit': 'original_task' if state['snapshot'].get('kind') == 'staged' else 'episode',
            'planned_episodes': int(state['snapshot'].get('kind') != 'staged'),
            'completed_episodes': int(done == total and state['snapshot'].get('kind') != 'staged'),
            'completed_stages': done, 'total_stages': total, 'official': False,
            'exit_code': 0 if done == total else 130 if status == 'interrupted' else 1}


def run_episode(root, registry_path, episode_id, agent_id, out, *, execute=False, resume=False, max_stages=None):
    root, out, registry_path = Path(root).resolve(), Path(out).resolve(), Path(registry_path).resolve()
    registry = read(registry_path)
    require(not resume or execute, '--resume requires --execute')
    agent = cli_adapters.select_agents(registry, [agent_id])[0]
    from .native_sessions import capability
    capability(agent['cli'])
    require(max_stages is None or type(max_stages) is int and max_stages > 0, 'max-stages must be positive')
    snap = snapshot(root, episode_id)
    needs_artifact = snap.get('kind') != 'staged'
    if needs_artifact:
        for stage in snap['stages']:
            stage['prompt'] += ARTIFACT_INSTRUCTION
    contract = identity(root, agent, registry['timeout_seconds'], snap)
    if not execute:
        return {'status': 'preview_no_model_called', 'episode': episode_id, 'agent': agent_id,
                'sample_unit': 'original_task' if snap.get('kind') == 'staged' else 'episode',
                'planned_episodes': int(snap.get('kind') != 'staged'), 'stages': len(snap['stages']),
                'stage_timeout_seconds': registry['timeout_seconds'],
                'episode_timeout_seconds': contract['episode_timeout_seconds'], 'out': str(out),
                'session_policy': 'same_native_session_preserved_artifacts', 'official': False}
    with file_lock(out.parent / ('.' + out.name + '.lock'), blocking=False, busy_message='Another process owns this episode'):
        if resume:
            execution = read(out / 'native-episode.json'); state = load_run(out)
            require(execution.get('contract') == contract and execution.get('contract_sha256') == digest(contract),
                    'Episode recipe, sources, budgets or code changed')
            require(state.get('execution_contract_sha256') == digest(contract), 'Episode execution binding changed')
            verify_attempts(out, execution, state)
            if execution.get('terminal_status'):
                return report(out, execution, state)
            if execution['attempts'] and execution['attempts'][-1]['status'] != 'completed':
                return report(out, execution, state)  # Never retry a failed/uncertain turn.
            if len(state['records']) == len(snap['stages']):
                return report(out, execution, state)
            require(not state.get('review_only'), 'Imported episodes are review-only; resume on the answer computer')
        else:
            require(not out.exists(), 'Episode output exists; preserve it or use --resume')
            state = {'schema_version': SCHEMA, 'snapshot': snap, 'snapshot_sha256': digest(snap),
                'candidate_dir': str(out / 'visible'), 'product': agent_id, 'model': agent['model'],
                'conversation_id': 'not_started_' + str(uuid.uuid4()), 'created_at': now(),
                'released': 0, 'records': [], 'official': False, 'isolation_verified': False,
                'session_evidence': 'native_cli_reported_not_authenticated',
                'execution_backend': 'native_cli', 'execution_contract_sha256': digest(contract),
                'aggregation': contract['aggregation']}
            execution = {'contract': contract, 'contract_sha256': digest(contract), 'attempts': []}
        selected = dict(registry, agents=[agent])
        with cli_setup.credential_environment(selected, registry_path) as sources, tempfile.TemporaryDirectory() as work:
            readiness = cli_adapters.check_agent(agent, work, credential_source=sources[agent_id])
            if readiness['issues']:
                return {'status': 'setup_required', 'agents': [readiness], 'exit_code': 1, 'model_calls': 0}
            check_continuation(agent, readiness['binary'])
            if not resume:
                out.mkdir(parents=True)
                write(out / 'run.json', state); write(out / 'native-episode.json', execution)
                write(out / 'readiness.json', readiness)
            initial_count = len(state['records'])
            for i in range(initial_count, len(snap['stages'])):
                if max_stages is not None and i - initial_count >= max_stages:
                    break
                spent = sum(a.get('elapsed_seconds', 0) for a in execution['attempts'])
                remaining = contract['episode_timeout_seconds'] - spent
                if remaining <= 0:
                    execution['terminal_status'] = 'budget_exhausted'
                    write(out / 'native-episode.json', execution)
                    break
                release(out); state = load_run(out)
                sid = snap['stages'][i]['id']; stage = out / 'stages' / sid
                require(not stage.exists(), 'Stage output already exists; preserve it')
                stage.mkdir(parents=True)
                row = {'stage_id': sid, 'status': 'running', 'started_at': now()}
                execution['attempts'].append(row); write(out / 'native-episode.json', execution)
                print(f"Running {agent_id} / {episode_id} / stage {i + 1}/{len(snap['stages'])}", file=sys.stderr, flush=True)
                began = time.monotonic()
                previous = out / 'stages' / f'S{i-1}' / 'checkpoint.json' if i else None
                source = snap['sources'][snap['stages'][i]['source']]['task']
                mode = source.get('execution', {}).get('web_mode', 'historical_source_rules_in_prompt')
                meta = native_runtime.capture(agent, readiness['binary'], snap['stages'][i]['prompt'],
                    mode, min(contract['stage_timeout_seconds'], remaining),
                    stage / 'events.jsonl', stage / 'stderr.log', stage / 'answer.md', stage / 'execution.json', stage / 'trace.jsonl',
                    previous_checkpoint=previous, checkpoint=stage / 'checkpoint.json', require_artifact=needs_artifact,
                    require_answer_file=any(d.get('path') == 'answer.md' and d.get('required')
                                            for d in source.get('deliverables', [])))
                row.update(status=meta['status'], finished_at=now(), elapsed_seconds=time.monotonic() - began,
                           native_session_id=meta.get('session_id'), error=meta.get('error'),
                           failure_origin='infrastructure_or_output_unresolved' if meta['status'] != 'completed' else None)
                row['files'] = {p.name: file_hash(p) for p in stage.iterdir() if p.is_file()}
                if meta['status'] == 'completed':
                    if i == 0:
                        state['conversation_id'] = meta['session_id']; write(out / 'run.json', state)
                    require(meta['session_id'] == state['conversation_id'], 'Native episode session changed')
                    receipt = {'stage_id': sid, 'conversation_id': state['conversation_id'], 'product': agent_id,
                        'model': agent['model'], 'same_conversation': True, 'answer_unedited': True,
                        'extra_user_messages': 0, 'started_at': row['started_at'], 'finished_at': row['finished_at'],
                        'native_session_id': meta['session_id'], 'execution_evidence_sha256': row['files']['execution.json']}
                    write(out / 'receipt.json', receipt)
                    record_answer(out, sid, stage / 'answer.md', out / 'receipt.json', native=True)
                    state = load_run(out)
                write(out / 'native-episode.json', execution)
                print(f"{agent_id} / {episode_id} / {sid}: {meta['status']} ({row['elapsed_seconds']:.1f}s)", file=sys.stderr, flush=True)
                if meta['status'] != 'completed':
                    break
            state = load_run(out)
            result = report(out, execution, state)
            write(out / 'status.json', result)
            return result
