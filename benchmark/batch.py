"""Sequential batch runs using the same contract for every baseline."""
from collections import Counter
from pathlib import Path
from .core import digest, read, require, safe_id, write
from .runner import execute, prepare


def experiment_settings(registry, baseline):
    # Explicit public fields only: never persist arbitrary argv, env, or credentials.
    return {**{k: registry.get(k) for k in ('profile', 'comparison_unit', 'evidence_policy',
            'human_intervention', 'attempt_policy', 'adapter_build_sha256')},
            **{k: baseline.get(k) for k in ('id', 'label', 'agent_product', 'client', 'client_version',
                'model', 'requested_model', 'accepted_ui_model_labels', 'auth', 'reasoning',
                'base_url', 'tool_policy', 'execution_mode')}}


def batch_run(rows, protocol, registry, out, execute_commands=False, resume=False):
    out = Path(out).resolve()
    require(resume or not out.exists(), 'Batch directory already exists; use --resume for unattempted runs')
    require(not resume or (out / 'batch.json').is_file(), 'Cannot resume without a batch manifest')
    baselines = registry['baselines']
    repeats = registry['repeats']
    require(rows and baselines, 'No tasks or baselines selected')
    require(type(repeats) is int and repeats > 0, 'Positive repeat count required')
    ids = [safe_id(b['id']) for b in baselines]
    require(len(ids) == len(set(ids)), 'Duplicate baseline IDs')
    for b in baselines:
        require(b['kind'] in ('model', 'agent'), 'Baseline kind must be model or agent')
        if b.get('execution_mode') == 'manual_app':
            require(b.get('command') is None, 'Manual App baseline cannot execute a command')
        elif execute_commands:
            require(all(isinstance(b.get(k), str) and b[k].strip() for k in ('version', 'model')),
                    'Pin adapter version and exact model before execution')
            require(isinstance(b.get('command'), list) and b['command'] and
                    all(isinstance(a, str) for a in b['command']), 'Configure argv command before execution')
    timeout = registry.get('timeout_seconds')
    if protocol.get('schema_version') == 'benchmark-study-protocol.1':
        from .study import validate_policy
        validate_policy(protocol)
        require(repeats == protocol['repeats'] and timeout == protocol['time_limit_seconds'],
                'Study repeat/time policy differs from registry')
        modes = protocol.get('required_web_modes')
        require(modes and all(task['execution']['web_mode'] in modes for task, _ in rows),
                'Study protocol must declare every selected task web mode')
    if execute_commands:
        require(type(timeout) is int and timeout > 0, 'Positive common timeout required')
        if registry.get('evidence_policy') == 'provided_materials_only':
            require(all(t['execution'].get('web_mode') == 'provided_materials_only' for t, _ in rows),
                    'This registry only executes provided_materials_only tasks')
    plan = {'protocol_sha256': digest(protocol), 'repeats': repeats, 'timeout_seconds': timeout,
            'tasks': {t['id']: digest(t) for t, _ in rows},
            'baselines': [{**experiment_settings(registry, b), 'version': b.get('version')} for b in baselines]}
    if resume:
        require(read(out / 'batch.json').get('plan_sha256') == digest(plan),
                'Batch plan/configuration changed; resume with the same resolved registry and selection')
    outcomes = []
    pending = []
    for b in baselines:
        for repeat in range(1, repeats + 1):
            for task, rubric in rows:
                run = out / b['id'] / task['id'] / f'r{repeat}'
                if not resume:
                    prepare(task, rubric, run, protocol)
                meta = read(run / 'run.json')
                if not resume:
                    settings = experiment_settings(registry, b)
                    meta.update(adapter=b['id'], adapter_version=b.get('version'), model=b.get('model'),
                                repeat=repeat, baseline_kind=b['kind'], timeout_seconds=timeout,
                                experiment=settings, experiment_sha256=digest(settings),
                                prepared_experiment_sha256=digest(settings),
                                execution_mode=b.get('execution_mode', 'command'))
                    write(run / 'run.json', meta)
                    if b.get('execution_mode') == 'manual_app':
                        from .manual import prepare_app_session
                        prepare_app_session(run, task, b)
                else:
                    require(meta['task_sha256'] == digest(task) and meta['protocol_sha256'] == digest(protocol)
                            and meta.get('prepared_experiment_sha256', meta.get('experiment_sha256'))
                            == digest(experiment_settings(registry, b)),
                            'Run/configuration changed; cannot resume')
                outcomes.append({'path': str(run.relative_to(out)), 'status': meta['status']})
                pending.append((run, b, repeat))
    manifest = {'official': False, 'plan_sha256': digest(plan), 'plan': plan, 'runs': outcomes}
    write(out / 'batch.json', manifest)
    for row, (run, b, repeat) in zip(outcomes, pending):
        if execute_commands and b.get('execution_mode') != 'manual_app' and read(run / 'run.json')['status'] == 'prepared':
            meta = execute(run, b['command'], b['id'], b['version'], b['model'], repeat, timeout)
            row['status'] = meta['status']
            write(out / 'batch.json', manifest)
            if meta['status'] == 'interrupted':
                break
    counts = dict(Counter(row['status'] for row in outcomes))
    manual_pending = sum(b.get('execution_mode') == 'manual_app' and read(run / 'run.json')['status'] == 'prepared'
                         for run, b, _ in pending)
    command_pending = sum(b.get('execution_mode') != 'manual_app' and read(run / 'run.json')['status'] in ('prepared', 'running')
                          for run, b, _ in pending)
    failed = sum(n for status, n in counts.items() if status not in ('completed', 'prepared', 'running'))
    exit_code = (130 if counts.get('interrupted') else 1 if failed or command_pending else 0) if execute_commands else 0
    status = ('prepared' if not execute_commands else 'interrupted' if exit_code == 130 else
              'failed' if exit_code else 'waiting_for_app' if manual_pending else 'completed')
    return {'batch': str(out), 'attempts': len(outcomes), 'executed': execute_commands, 'official': False,
            'status': status, 'exit_code': exit_code, 'statuses': counts,
            'completed_attempts': counts.get('completed', 0), 'failed_attempts': failed,
            'command_pending': command_pending, 'manual_pending': manual_pending}



def batch_status(out):
    out = Path(out).resolve()
    manifest = read(out / 'batch.json')
    groups = {}
    for row in manifest['runs']:
        run = (out / row['path']).resolve()
        require(run.is_relative_to(out), 'Invalid batch run path')
        meta = read(run / 'run.json')
        group = groups.setdefault(meta['adapter'], {'model': meta['model'], 'statuses': {},
                                 'model_identity': {}, 'scored': 0})
        statuses = group['statuses']
        statuses[meta['status']] = statuses.get(meta['status'], 0) + 1
        observation = meta.get('model_observation', {}).get('status', 'not_observed')
        group['model_identity'][observation] = group['model_identity'].get(observation, 0) + 1
        group['scored'] += (run / 'score.json').is_file()
    return {'batch': str(out), 'official': False, 'baselines': groups,
            'note': 'Read from run.json; prepared runs can resume, attempted/running runs are never retried automatically.'}
