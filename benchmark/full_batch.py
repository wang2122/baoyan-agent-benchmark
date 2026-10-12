"""Unified public native batches. Original tasks and derived episodes stay distinct."""
from collections import Counter
from pathlib import Path
import sys

from .core import digest, file_hash, read, require, safe_id, write
from .runtime_common import file_lock
from .native import selection, run_native
from .longitudinal import catalog, snapshot, load_run
from .episode_runner import ARTIFACT_INSTRUCTION, identity as episode_identity, run_episode, verify_attempts
from .workflow_versions import component_files, context_identity

SCHEMA = 'benchmark-full-batch.1'
COMPONENTS = ('full_batch.py', 'full_judge.py', 'full_leaderboard.py', 'native_sessions.py',
              'episode_runner.py', 'episode_state.py', 'longitudinal.py', 'task_sources.py',
              'full_packet.py', 'full_packet_runner.py', 'full_packet_io.py')


def is_full(directory):
    return (Path(directory) / 'full-batch.json').is_file()


def plan_identity(plan):
    """Release audit and unselected registry entries do not change execution."""
    return dict({k: v for k, v in plan.items() if k not in ('plan_sha256', 'context')},
                context=context_identity(plan['context']))


def validate_selection(root, value):
    """Bind an explicit reporting scope to canonical public tasks and rubrics."""
    root = Path(root).resolve()
    safe_id(value['id'])
    ids = value['task_ids']
    require(isinstance(ids, list) and ids and all(isinstance(t, str) for t in ids)
            and len(ids) == len(set(ids)) == value['task_count'], 'Invalid selection task IDs/count')
    require([t['task_id'] for t in value['tasks']] == ids, 'Selection entries differ from task IDs')
    bindings = value['source_files_sha256']
    require({'data/dev/manifest.json', 'data/staged-dev/manifest.json'} <= set(bindings),
            'Selection must bind public task manifests')
    for name, expected in bindings.items():
        path = (root / name).resolve()
        require(path.is_relative_to(root) and file_hash(path) == expected, 'Selection source changed: ' + name)
    available = {track: set(read(root / 'data' / track / 'manifest.json')['tasks'])
                 for track in ('dev', 'staged-dev')}
    originals, stages = [], 0
    for entry in value['tasks']:
        tid = safe_id(entry['task_id']); track = entry['track']
        require(track in available and tid in available[track], 'Selection task is outside public dev: ' + tid)
        for kind in ('task', 'rubric'):
            name = f'data/{track}/{tid}/{kind}.json'
            require(entry[kind + '_path'] == name, 'Selection must use canonical task paths')
            require(digest(read(root / name)) == entry['fingerprints'][kind],
                    'Selected task or rubric changed: ' + tid)
        task = read(root / entry['task_path'])
        count = len(task['stages']) if track == 'staged-dev' else 1
        require(entry['case_group_id'] == task['case_group_id'] and entry['stage_count'] == count,
                'Selection case or stage count changed: ' + tid)
        originals.append({'id': tid, 'kind': 'staged' if track == 'staged-dev' else 'single',
                          'case_group_id': task['case_group_id']})
        stages += count
    require(stages == value['stage_count'] and
            len({t['case_group_id'] for t in originals}) == value['case_group_count'],
            'Selection totals differ from source tasks')
    return originals


def make_plan(root, registry_path, suite, agent_ids, task_ids, protocol_path,
              include_staged, include_episodes, episode_ids, selection_path=None):
    require(suite == 'dev', 'Full native batches currently require the public dev suite')
    selected_manifest = read(selection_path) if selection_path else None
    if selected_manifest is not None:
        require(task_ids is None and not include_episodes and episode_ids is None,
                '--selection cannot be combined with --task or derived episodes')
        selected_originals = validate_selection(root, selected_manifest)
        task_ids = selected_manifest['task_ids']
        include_staged = True
    registry, agents, singles, protocol, resolved, context = selection(
        root, registry_path, suite, agent_ids, None, protocol_path)
    require(protocol.get('schema_version') != 'benchmark-study-protocol.1',
            'Full batches require a new study protocol before formal experiments')
    staged_ids = read(root / 'data/staged-dev/manifest.json')['tasks'] if include_staged else []
    original_ids = [t['id'] for t, _ in singles] + list(staged_ids)
    require(len(original_ids) == len(set(original_ids)), 'Duplicate original task IDs')
    if task_ids is not None:
        require(task_ids and len(task_ids) == len(set(task_ids)) and set(task_ids) <= set(original_ids),
                'Unknown or duplicate task selection (use --include-staged for two-stage tasks)')
    selected = set(task_ids or original_ids)
    available_episodes = [e['id'] for e in catalog(root)['episodes']] if include_episodes else []
    require(episode_ids is None or (include_episodes and episode_ids and
            len(episode_ids) == len(set(episode_ids)) and set(episode_ids) <= set(available_episodes)),
            'Unknown episodes or missing --include-episodes')
    chosen_episodes = episode_ids if episode_ids is not None else available_episodes
    originals = [{'id': t['id'], 'kind': 'single', 'case_group_id': t['case_group_id']}
                 for t, _ in singles]
    originals += [{'id': tid, 'kind': 'staged',
                   'case_group_id': snapshot(root, 'staged:' + tid)['episode']['case_group_id']}
                  for tid in staged_ids]
    if selected_manifest is not None:
        require({t['id']: t for t in originals if t['id'] in selected} ==
                {t['id']: t for t in selected_originals}, 'Selection differs from canonical task scope')
        originals = selected_originals
    definitions = [dict(t, selector=t['id'] if t['kind'] == 'single' else 'staged:' + t['id'])
                   for t in originals if t['id'] in selected]
    definitions += [{'id': eid, 'kind': 'episode', 'selector': eid,
                     'case_group_id': snapshot(root, eid)['episode']['case_group_id']}
                    for eid in chosen_episodes]
    sources = {d['selector']: digest(snapshot(root, d['selector']))
               for d in definitions if d['kind'] != 'single'}
    jobs = []
    for agent in agents:
        for repeat in range(1, registry['repeats'] + 1):
            for unit in definitions:
                jobs.append(dict(unit, agent=agent['id'], repeat=repeat,
                    path=f"{unit['kind']}/{agent['id']}/{unit['id']}/r{repeat}"))
    jobs.sort(key=lambda job: {'single': 0, 'staged': 1, 'episode': 2}[job['kind']])
    require(jobs, 'Empty full batch selection')
    plan = {'schema_version': SCHEMA, 'official': False, 'agents': agents,
            'repeats': registry['repeats'], 'timeout_seconds': registry['timeout_seconds'],
            'context': context, 'resolved_registry': resolved, 'protocol': protocol,
            'components': component_files(root, ['benchmark/' + n for n in COMPONENTS]),
            'source_snapshots': sources, 'originals': originals,
            'expected_episodes': available_episodes, 'jobs': jobs}
    if selected_manifest is not None:
        plan['selection'] = selected_manifest
    plan['plan_sha256'] = digest(plan)
    return plan


def job_path(directory, job):
    expected = f"{job['kind']}/{safe_id(job['agent'])}/{safe_id(job['id'])}/r{job['repeat']}"
    require(job['kind'] in ('single', 'staged', 'episode') and job['path'] == expected and
            type(job['repeat']) is int and job['repeat'] > 0, 'Invalid full batch job path')
    path = Path(directory)
    require(not path.is_symlink(), 'Linked full batch directory')
    for part in Path(expected).parts:
        path = path / part
        require(not path.is_symlink(), 'Linked full batch job path')
    return path


def report(directory, plan):
    require(plan.get('plan_sha256') == digest({k: v for k, v in plan.items() if k != 'plan_sha256'}),
            'Full batch plan changed')
    rows = []
    for job in plan['jobs']:
        path = job_path(directory, job)
        status = 'prepared'; stages = None
        if job['kind'] == 'single' and (path / 'run.json').is_file():
            status = read(path / 'run.json')['status']
        elif (path / 'native-episode.json').is_file():
            execution, state = read(path / 'native-episode.json'), load_run(path)
            stages = {'completed': len(state['records']), 'total': len(state['snapshot']['stages'])}
            attempts = execution['attempts']
            status = ('completed' if stages['completed'] == stages['total'] else
                      execution.get('terminal_status') or
                      (attempts[-1]['status'] if attempts and attempts[-1]['status'] != 'completed' else 'partial'))
        rows.append(dict(job, status=status, stages=stages))
    counts = dict(Counter(r['status'] for r in rows))
    complete = counts.get('completed', 0) == len(rows)
    return {'status': 'completed' if complete else 'incomplete', 'official': False,
            'batch': str(Path(directory).resolve()), 'planned': len(rows),
            'completed': counts.get('completed', 0), 'statuses': counts, 'jobs': rows,
            'original_tasks': len(plan['originals']), 'derived_episodes': len(plan['expected_episodes']),
            'exit_code': 0 if complete else 130 if counts.get('interrupted') else 1}


def verify(root, directory):
    """Verify frozen sources and captured native records before reviewing/exporting."""
    from .submissions import verify_results
    from .reproduction import verify_context
    root, directory = Path(root).resolve(), Path(directory).resolve()
    from .full_packet import MARKER, verify_import
    if (directory / MARKER).exists():
        verify_import(directory)
    plan = read(directory / 'full-batch.json')
    require(plan.get('schema_version') == SCHEMA, 'Wrong full batch schema')
    require(plan.get('plan_sha256') == digest({k: v for k, v in plan.items() if k != 'plan_sha256'}),
            'Full batch plan changed')
    require(plan['components'] == component_files(root, ['benchmark/' + n for n in COMPONENTS]),
            'Full workflow changed; use matching release')
    verify_context(root, plan['context'])
    if 'selection' in plan:
        require(plan['originals'] == validate_selection(root, plan['selection']) and
                not plan['expected_episodes'], 'Selection reporting scope changed')
        expected = {(a['id'], t['id'], t['kind'], r) for a in plan['agents']
                    for t in plan['originals'] for r in range(1, plan['repeats'] + 1)}
        require({(j['agent'], j['id'], j['kind'], j['repeat']) for j in plan['jobs']} == expected,
                'Selected batch jobs differ from reporting scope')
    require(len({j['path'] for j in plan['jobs']}) == len(plan['jobs']), 'Duplicate full batch jobs')
    for selector, sha in plan['source_snapshots'].items():
        require(digest(snapshot(root, selector)) == sha, 'Full batch source changed: ' + selector)
    require(read(directory / 'agents.frozen.json') == {
        'schema': 'native-cli-batch.1', 'agents': plan['agents'],
        'repeats': plan['repeats'], 'timeout_seconds': plan['timeout_seconds']}, 'Frozen native recipes changed')
    if (directory / 'single/batch.json').exists():
        verify_results(root, directory / 'single')
        from .reproduction import batch_runs
        _, runs = batch_runs(directory / 'single')
        expected = {j['path'][7:]: j for j in plan['jobs'] if j['kind'] == 'single'}
        require({r.relative_to(directory / 'single').as_posix() for r in runs} == set(expected),
                'Single-stage child plan differs from full batch')
        require(read(directory / 'single/resolved-registry.json') == plan['resolved_registry'],
                'Single-stage recipes differ from full batch')
    agents = {a['id']: a for a in plan['agents']}
    for job in plan['jobs']:
        path = job_path(directory, job)
        if job['kind'] == 'single' or not (path / 'native-episode.json').exists():
            continue
        state, execution = load_run(path), read(path / 'native-episode.json')
        snap = snapshot(root, job['selector'])
        if job['kind'] == 'episode':
            for stage in snap['stages']:
                stage['prompt'] += ARTIFACT_INSTRUCTION
        expected = episode_identity(root, agents[job['agent']], plan['timeout_seconds'], snap)
        require(execution['contract'] == expected and state['snapshot'] == snap,
                'Full batch episode differs from frozen job')
        verify_attempts(path, execution, state)
    return plan


def run(root, registry_path, out, suite='dev', agent_ids=None, task_ids=None, protocol_path=None,
        execute=False, resume=False, include_staged=False, include_episodes=False, episode_ids=None,
        selection_path=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    require(not resume or execute, '--resume requires --execute')
    plan = make_plan(root, registry_path, suite, agent_ids, task_ids, protocol_path,
                     include_staged, include_episodes, episode_ids, selection_path)
    if not execute:
        return {'status': 'preview_no_model_called', 'official': False, 'exit_code': 0,
                'agents': [a['id'] for a in plan['agents']], 'planned': len(plan['jobs']),
                'original_tasks': len(plan['originals']), 'derived_episodes': len(plan['expected_episodes']),
                'jobs': plan['jobs'], 'out': str(out)}
    from . import cli_batch, cli_setup, native_sessions
    from .full_packet import MARKER
    require(not (out / MARKER).exists(), 'Imported full batches are review-only; resume on the answer computer')
    with file_lock(out.parent / ('.' + out.name + '.full.lock'), blocking=False, busy_message='Full batch already running'):
        if out.exists():
            require(resume, 'Full batch exists; use matching --resume')
            saved = verify(root, out)
            require(plan_identity(saved) == plan_identity(plan), 'Full batch plan changed')
            plan = saved
        else:
            require(not resume, 'No full batch to resume')
        # All selected products must be ready before any inference, including multi-turn flags.
        registry = read(registry_path)
        with cli_setup.credential_environment(dict(registry, agents=plan['agents']), registry_path) as sources:
            readiness = cli_batch.inspect_agents(plan['agents'], credential_sources=sources)
            if readiness['status'] != 'ready_for_trial':
                return dict(readiness, exit_code=1)
            by_agent = {r['id']: r for r in readiness['agents']}
            for agent in plan['agents']:
                by_agent[agent['id']]['continuation'] = native_sessions.check(agent, by_agent[agent['id']]['binary'])
            if not out.exists():
                out.mkdir(parents=True)
                write(out / 'full-batch.json', plan); write(out / 'readiness.json', readiness)
            frozen_path = out / 'agents.frozen.json'
            frozen = {'schema': 'native-cli-batch.1', 'agents': plan['agents'],
                      'repeats': plan['repeats'], 'timeout_seconds': plan['timeout_seconds']}
            if frozen_path.exists():
                require(read(frozen_path) == frozen, 'Frozen native recipes changed')
            else:
                require(not resume, 'Missing frozen native recipes')
                write(frozen_path, frozen)
            singles = sorted({j['id'] for j in plan['jobs'] if j['kind'] == 'single'})
            if singles:
                result = run_native(root, frozen_path, out / 'single', suite, agent_ids, singles, protocol_path,
                                    execute=True, resume=(out / 'single').exists(), continue_on_error=True)
                if result.get('exit_code') == 130 or result.get('status') == 'setup_required':
                    return result
            for i, job in enumerate(plan['jobs'], 1):
                if job['kind'] == 'single':
                    continue
                path = job_path(out, job)
                print(f"Batch {i}/{len(plan['jobs'])}: {job['agent']} / {job['id']} / r{job['repeat']}",
                      file=sys.stderr, flush=True)
                result = run_episode(root, frozen_path, job['selector'], job['agent'], path,
                                     execute=True, resume=path.exists())
                write(out / 'full-status.json', report(out, plan))
                if result.get('exit_code') == 130 or result.get('status') == 'setup_required':
                    return result
            result = report(out, plan); write(out / 'full-status.json', result)
            return result
