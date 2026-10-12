"""Bounded recovery campaigns over immutable native attempts.

Each worker owns one independent task (all turns of an episode). Historical formal
batches remain untouched. A recovery campaign is not a new formal registration.
"""
from collections import Counter
from contextlib import ExitStack
import os
import json
from pathlib import Path
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time

from .core import digest, file_hash, read, require, write
from .runtime_common import file_lock, now, regular_bytes

MARKER = 'parallel-batch.json'
LIMITS = {'codex': 2, 'dsh': 2, 'qwen': 2, 'kimi': 2, 'antigravity': 2, 'claude': 2}
WORKER = 'from benchmark.parallel_batch import worker; worker()'


def prepare_subset(root, registry_path, selection_path, agent_ids, out, *,
                   reuse_runs=(), superseded_campaign=None, account_recovery_attempts=()):
    """Freeze a selected subset, optionally carrying verified file captures."""
    from . import formal_public, full_batch
    from .cli_adapters import select_agents
    root, out = Path(root).resolve(), Path(out).absolute()
    require(not out.exists() and not out.is_symlink(), 'Choose a new subset directory')
    selection = read(selection_path)
    ids = selection['task_ids']
    require(ids and len(ids) == len(set(ids)) == selection['task_count'], 'Invalid subset IDs')
    require([t['task_id'] for t in selection['tasks']] == ids, 'Subset entries differ from IDs')
    for name, expected in selection['source_files_sha256'].items():
        path = (root / name).resolve()
        require(path.is_relative_to(root) and file_hash(path) == expected, 'Subset source changed')
    for entry in selection['tasks']:
        for kind in ('task', 'rubric'):
            path = (root / entry[kind + '_path']).resolve()
            require(path.is_relative_to(root) and digest(read(path)) == entry['fingerprints'][kind],
                    'Selected task or rubric changed: ' + entry['task_id'])
    registry = read(registry_path)
    agents = select_agents(registry, agent_ids)
    require(registry['repeats'] == 1, 'Subset campaign currently requires one repeat')
    require(all(a.get('cli_version') and a['cli'] in LIMITS for a in agents), 'Pin supported clients first')
    registry = dict(registry, agents=agents)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.subset-prepare-', dir=out.parent) as tmp:
        stage = Path(tmp) / 'campaign'; stage.mkdir()
        write(stage / 'agents.frozen.json', registry)
        plan = full_batch.make_plan(root, stage / 'agents.frozen.json', 'dev', None, ids,
                                    None, True, False, None)
        require(len(plan['jobs']) == len(ids) * len(agents), 'Subset plan has unexpected jobs')
        cases = {j['case_group_id'] for j in plan['jobs']}
        require(len(cases) == selection['case_group_count'], 'Subset case count changed')
        order = {tid: i for i, tid in enumerate(ids)}
        jobs = sorted(plan['jobs'], key=lambda j: (j['agent'], order[j['id']]))
        jobs = [dict(job, index=i, status='prepared') for i, job in enumerate(jobs)]
        reused = {}
        carried = {}
        if reuse_runs or account_recovery_attempts:
            from . import native
            from .formal_public import tree_hashes
            _, _, rows, _, _, context = native.selection(root, stage / 'agents.frozen.json', 'dev')
            tasks = {t['id']: (t, r) for t, r in rows}
            recipes = {a['id']: a for a in agents}
            for run in map(lambda p: Path(p).resolve(), reuse_runs):
                meta = read(run / 'run.json')
                key = (meta['adapter'], meta['task_id'])
                matches = [j for j in jobs if (j['agent'], j['id']) == key and j['kind'] == 'single']
                require(len(matches) == 1, 'Reused capture is outside the selected subset')
                index = str(matches[0]['index'])
                require(index not in reused, 'Duplicate reused capture')
                meta = native.inspect_run(run, *tasks[meta['task_id']], context)
                require(meta['status'] == 'completed' and
                        meta.get('answer_capture', {}).get('source') == 'workspace_file' and
                        meta['execution_config'] == recipes[meta['adapter']],
                        'Reuse requires a completed file capture with matching configuration')
                reused[index] = {'path': str(run), 'files': tree_hashes(run),
                                 'answer_sha256': file_hash(run / 'workspace/answer.md')}
            for attempt in map(lambda p: Path(p).resolve(), account_recovery_attempts):
                result = read(attempt / 'result.json')
                require(result.get('category') == 'account_blocked', 'Only resolved account blocks can be carried')
                runs = list((attempt / 'capture').glob('*/*/r1'))
                require(len(runs) == 1, 'Account recovery requires one single-task capture')
                run = runs[0]
                meta = read(run / 'run.json')
                meta = native.inspect_run(run, *tasks[meta['task_id']], context)
                matches = [j for j in jobs if (j['agent'], j['id'], j['kind']) ==
                           (meta['adapter'], meta['task_id'], 'single')]
                require(len(matches) == 1 and meta['status'] != 'completed' and
                        meta['execution_config'] == recipes[meta['adapter']], 'Account recovery configuration changed')
                job = matches[0]; index = str(job['index'])
                require(index not in carried and index not in reused, 'Duplicate carried attempt')
                carried[index] = {'path': str(attempt), 'files': tree_hashes(attempt)}
                job['status'] = 'failed'  # The original failure consumes one attempt.
        superseded = None
        if superseded_campaign:
            from .formal_public import tree_hashes
            previous = Path(superseded_campaign).resolve()
            old, state = read(previous / MARKER), read(previous / 'parallel-state.json')
            require(old['schema_version'] == 'native-subset-campaign.1' and
                    not old.get('superseded_campaign') and not state['running'] and
                    not any(r['status'] == 'running' for r in state['jobs']),
                    'Stop the original subset before continuing; only one rescope is supported')
            require(old['registry'] == registry, 'Continuation configuration changed')
            require(old['manifest_sha256'] == digest({k: v for k, v in old.items() if k != 'manifest_sha256'})
                    and tree_hashes(previous / 'source') == old['source_files'],
                    'Previous frozen campaign changed')
            validate_state(previous, old, state)
            require(read(previous / 'selection.json') == selection, 'Continuation selection changed')
            require([(j['agent'], j['id']) for j in old['jobs']] ==
                    [(j['agent'], j['id']) for j in jobs], 'Continuation job order changed')
            for job, row in zip(jobs, state['jobs']):
                if row['attempts'] and str(job['index']) not in reused:
                    require(len(row['attempts']) == 1 and row['status'] == 'interrupted',
                            'Carry every completed capture and resolve failures before continuing')
                    job['status'] = 'interrupted'  # The previous attempt consumes one retry.
            superseded = {'path': str(previous), 'files': tree_hashes(previous)}
        source_files = formal_public.snapshot(root, stage / 'source')
        write(stage / 'selection.json', selection)
        m = {'schema_version': 'native-subset-campaign.1', 'official': False,
             'created_at': now(), 'source_files': source_files, 'registry': registry,
             'prior_batches': [], 'jobs': jobs, 'selection_sha256': file_hash(stage / 'selection.json'),
             'concurrency_by_product': {a['cli']: LIMITS[a['cli']] for a in agents},
             'policy': {'max_total_attempts': 2, 'retry_delay_seconds': 30,
                        'failure_scope': 'task_failure_isolated_setup_or_account_pauses_product',
                        'episode_retry': 'new_session_from_first_stage',
                        'selection': 'first_completed_capture_never_best_score',
                        'publication': 'public_subset_only_requires_separate_review',
                        'prior_answers': 'fresh_attempts_no_prior_answer_reuse'}}
        if reused:
            m['reused_runs'] = reused
            m['policy']['prior_answers'] = 'explicit_verified_file_captures_with_original_provenance'
        if carried:
            m['account_recovery_attempts'] = carried
            m['policy']['account_recovery'] = 'explicit_resume_original_account_failure_consumes_one_attempt'
        if superseded:
            m['superseded_campaign'] = superseded
            m['policy']['rescope'] = 'all_previous_attempts_preserved_interrupted_attempts_consume_retry_budget'
        m['manifest_sha256'] = digest(m)
        write(stage / MARKER, m)
        write(stage / 'parallel-state.json', {
            'jobs': [{'index': j['index'], 'status': 'completed' if str(j['index']) in reused else 'queued',
                      'attempts': [], 'selected': {'reused': str(j['index'])} if str(j['index']) in reused else None,
                      'retry_at': 0} for j in jobs],
            'blocked_groups': {}, 'running': False, 'updated_at': now()})
        stage.rename(out)
    return status(out)


def prepare(root, prior_batches, out, *, limits=None, retry_delay=30):
    from . import formal_public, full_batch
    root, out = Path(root).resolve(), Path(out).absolute()
    require(not out.exists() and not out.is_symlink(), 'Choose a new recovery directory')
    require(prior_batches and len(set(map(str, prior_batches))) == len(prior_batches), 'Duplicate or empty sources')
    require(type(retry_delay) is int and retry_delay >= 0, 'Invalid retry delay')
    agents, jobs, priors = [], [], []
    budget = None
    for directory in prior_batches:
        directory = Path(directory).resolve()
        registered = formal_public.verify(directory)
        formal_public.worker(directory, 'verify')
        registry = registered['registry']
        this_budget = (registry['repeats'], registry['timeout_seconds'])
        require(budget is None or budget == this_budget, 'Source batch budgets differ')
        budget = this_budget
        require(registry['repeats'] == 1, 'Recovery currently requires one original repeat')
        require(not {a['id'] for a in agents} & {a['id'] for a in registry['agents']}, 'Duplicate agent sources')
        agents.extend(registry['agents'])
        old = read(directory / 'answers/full-batch.json')
        current = full_batch.make_plan(root, directory / 'agents.frozen.json', 'dev', None, None,
                                      None, True, True, None)
        require(old['originals'] == current['originals'] and
                old['source_snapshots'] == current['source_snapshots'] and
                old['context']['fingerprints'] == current['context']['fingerprints'] and
                old['protocol'] == current['protocol'], 'Recovery must preserve tasks, rubrics and budgets')
        prior_index = len(priors)
        priors.append({'path': str(directory), 'manifest_sha256': file_hash(directory / formal_public.MARKER)})
        for job in full_batch.report(directory / 'answers', old)['jobs']:
            require(job['status'] != 'running', 'Stop source runs before recovery')
            path = full_batch.job_path(directory / 'answers', job)
            jobs.append(dict(job, index=len(jobs), prior_index=prior_index,
                             prior_files=formal_public.tree_hashes(path) if path.exists() else {}))
    caps = dict(LIMITS if limits is None else limits)
    require(all(a['cli'] in caps for a in agents) and
            all(type(v) is int and 1 <= v <= 10 for v in caps.values()), 'Invalid concurrency limits')
    # Product grouping deliberately combines configurations sharing an account.
    # This conservative grouping also combines multiple accounts for that product.
    registry = {'schema': 'native-cli-batch.1', 'agents': agents,
                'repeats': budget[0], 'timeout_seconds': budget[1]}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.parallel-prepare-', dir=out.parent) as tmp:
        stage = Path(tmp) / 'campaign'; stage.mkdir()
        source_files = formal_public.snapshot(root, stage / 'source')
        write(stage / 'agents.frozen.json', registry)
        manifest = {'schema_version': 'native-recovery-campaign.1', 'official': False,
            'created_at': now(), 'source_files': source_files, 'registry': registry,
            'prior_batches': priors, 'jobs': jobs, 'concurrency_by_product': caps,
            'policy': {'max_total_attempts': 2, 'retry_delay_seconds': retry_delay,
                'failure_scope': 'task_failure_isolated_setup_or_account_pauses_product',
                'old_interruption_counts_as_attempt': True, 'episode_retry': 'new_session_from_first_stage',
                'selection': 'first_completed_capture_never_best_score',
                'legacy_completed': 'explicit_capture_audit_before_reuse_or_recapture',
                'publication': 'recovery_results_require_separate_review_and_protocol_disclosure'}}
        manifest['manifest_sha256'] = digest(manifest)
        write(stage / MARKER, manifest)
        state = {'jobs': [{'index': j['index'], 'status': 'legacy_review' if j['status'] == 'completed' else 'queued',
                          'attempts': [], 'selected': None, 'retry_at': 0} for j in jobs],
                 'blocked_groups': {}, 'running': False, 'updated_at': now()}
        write(stage / 'parallel-state.json', state)
        stage.rename(out)
    return status(out)


def verify(directory, *, prior_files=False):
    from .formal_public import tree_hashes, MARKER as FORMAL_MARKER
    directory = Path(directory).resolve()
    m = read(directory / MARKER)
    require(m.get('schema_version') in ('native-recovery-campaign.1', 'native-subset-campaign.1')
            and m.get('official') is False,
            'Unsupported recovery campaign')
    require(m['manifest_sha256'] == digest({k: v for k, v in m.items() if k != 'manifest_sha256'}),
            'Recovery plan changed')
    require(tree_hashes(directory / 'source') == m['source_files'], 'Frozen recovery engine changed')
    require(read(directory / 'agents.frozen.json') == m['registry'], 'Recovery recipes changed')
    require(file_hash(Path(__file__)) == m['source_files']['benchmark/parallel_batch.py'],
            'Use the matching frozen recovery scheduler')
    if m['schema_version'] == 'native-subset-campaign.1':
        require(not m['prior_batches'] and not m.get('previous_recovery'), 'Subset cannot carry recovery sources')
        require(file_hash(directory / 'selection.json') == m['selection_sha256'], 'Subset selection changed')
        require(all(j['status'] == 'prepared' or
                    (j['status'] == 'interrupted' and m.get('superseded_campaign')) or
                    (j['status'] == 'failed' and str(j['index']) in m.get('account_recovery_attempts', {}))
                    for j in m['jobs']),
                'Invalid subset starting status')
    for p in m['prior_batches']:
        require(file_hash(Path(p['path']) / FORMAL_MARKER) == p['manifest_sha256'], 'Prior registration changed')
    if prior_files:
        for reused in m.get('reused_runs', {}).values():
            require(tree_hashes(reused['path']) == reused['files'], 'Reused capture changed')
        for carried in m.get('account_recovery_attempts', {}).values():
            require(tree_hashes(carried['path']) == carried['files'], 'Carried account failure changed')
        superseded = m.get('superseded_campaign')
        if superseded:
            require(tree_hashes(superseded['path']) == superseded['files'], 'Superseded campaign changed')
        previous = m.get('previous_recovery')
        if previous:
            require(tree_hashes(previous['path']) == previous['files'], 'Previous recovery evidence changed')
        for job in m['jobs'] if m['schema_version'] == 'native-recovery-campaign.1' else []:
            source = prior_path(m, job)
            require((tree_hashes(source) if source.exists() else {}) == job['prior_files'],
                    'Prior capture changed: ' + job['path'])
    return m


def prior_path(manifest, job):
    from .full_batch import job_path
    return job_path(Path(manifest['prior_batches'][job['prior_index']]['path']) / 'answers', job)


def attempt_path(directory, job, number):
    require(type(number) is int and 1 <= number <= 3, 'Invalid attempt number')
    return Path(directory) / 'attempts' / str(job['index']) / ('a' + str(number))


def remaining_attempts(job, row):
    prior_used = int(job['status'] != 'prepared')
    # A capture-only defect is not a model retry. The audit records this separately.
    if row.get('audit', {}).get('action') == 'recapture':
        prior_used = 0
    return 2 - prior_used - len(row['attempts']) + len(row.get('budget_exclusions', {}))


def rebase(root, previous, out):
    """One explicit scheduler repair, preserving every old byte and attempt.

    Only a single carried controller cancellation or pre-inference authorization
    refusal per job can be excluded from the retry budget. Costs remain recorded.
    """
    from .formal_public import tree_hashes
    root, previous, out = Path(root).resolve(), Path(previous).resolve(), Path(out).absolute()
    require(not out.exists(), 'Choose a new recovery directory')
    with file_lock(previous / '.scheduler.lock', blocking=False, busy_message='Stop previous scheduler first'):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1'); env.pop('PYTHONPATH', None)
        check = subprocess.run([sys.executable, '-B', '-c',
            'from benchmark.parallel_batch import verify,validate_state; from benchmark.core import read; '
            'import sys; from pathlib import Path; p=Path(sys.argv[1]); '
            'm=verify(p,prior_files=True); validate_state(p,m,read(p/"parallel-state.json"))', str(previous)],
            cwd=previous / 'source', env=env, capture_output=True, text=True)
        require(check.returncode == 0, 'Previous frozen recovery validation failed')
        m, state = read(previous / MARKER), read(previous / 'parallel-state.json')
        require(not m.get('previous_recovery'), 'Only one explicit scheduler repair is supported')
        require(not state['running'] and not any(r['status'] == 'running' for r in state['jobs']),
                'Stop previous scheduler first')
        exclusions = {}
        for job, row in zip(m['jobs'], state['jobs']):
            require(len(row['attempts']) <= 1, 'Repair requires at most one carried attempt per job')
            if row['attempts']:
                attempt = row['attempts'][0]
                capture = attempt_path(previous, job, 1) / 'capture'
                category = classify(attempt['result'], capture)
                reason = None
                if category == 'interrupted' and (previous / 'stop-request.json').exists():
                    reason = 'controller_cancelled_during_scheduler_repair'
                elif category == 'account_blocked':
                    events = [json.loads(line) for p in capture.rglob('trajectory.jsonl')
                              for line in regular_bytes(p).decode('utf-8').splitlines() if line.strip()]
                    if events and all(e.get('type') == 'system' and e.get('subtype') == 'version' or
                                      e.get('type') == 'system.version' for e in events):
                        reason = 'authorization_refused_before_inference'
                if reason:
                    row['budget_exclusions'] = {'1': reason}
                    exclusions[str(job['index'])] = row['budget_exclusions']
                    row.update(status='queued', retry_at=0, selected=None)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.parallel-rebase-', dir=out.parent) as tmp:
            stage = Path(tmp) / 'campaign'; stage.mkdir()
            for name in ('attempts', 'recovered'):
                if (previous / name).exists(): shutil.copytree(previous / name, stage / name)
            for row in state['jobs']:
                if row.get('audit', {}).get('action') == 'recover':
                    row['selected']['answer'] = str(out / 'recovered' / str(row['index']) / 'answer.md')
            # Preserve task inputs and every unrelated engine component, including
            # concurrent work in the organizer checkout. Repair only the scheduler.
            shutil.copytree(previous / 'source', stage / 'source')
            shutil.copyfile(root / 'benchmark/parallel_batch.py', stage / 'source/benchmark/parallel_batch.py')
            m['source_files'] = tree_hashes(stage / 'source')
            m['previous_recovery'] = {'path': str(previous), 'files': tree_hashes(previous),
                                      'budget_exclusions': exclusions}
            m['created_at'] = now()
            m['policy']['scheduler_repair'] = 'one_carried_infrastructure_attempt_excluded_all_records_and_costs_retained'
            m['manifest_sha256'] = digest({k: v for k, v in m.items() if k != 'manifest_sha256'})
            write(stage / MARKER, m)
            write(stage / 'agents.frozen.json', m['registry'])
            state.update(blocked_groups={}, running=False, updated_at=now())
            state.pop('scheduler_pid', None)
            write(stage / 'parallel-state.json', state)
            validate_state(stage, m, state)
            stage.rename(out)
    return status(out)


def classify(result, capture):
    """Only transient failures are automatically retried; quota/auth pauses a product."""
    state = result.get('status')
    if state == 'completed':
        return 'completed'
    if state == 'interrupted':
        return 'interrupted'
    logs = []
    for name in ('cli.stderr.log', 'stderr.log'):
        for path in Path(capture).rglob(name):
            logs.append(regular_bytes(path)[-32768:].decode('utf-8', errors='replace'))
    for name in ('trajectory.jsonl', 'events.jsonl'):
        for path in Path(capture).rglob(name):
            for line in regular_bytes(path).decode('utf-8', errors='replace').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get('event', event.get('type'))
                if kind == 'rate_limit_event' and isinstance(event.get('rate_limit_info'), dict):
                    if event['rate_limit_info'].get('status') == 'rejected':
                        return 'account_blocked'
                result = event.get('result') if isinstance(event.get('result'), dict) else event
                if (kind == 'error' or (kind == 'result' and
                    (result.get('is_error') or result.get('error') or result.get('success') is False or
                     result.get('status') in ('error', 'failed'))) or
                    (kind == 'status' and event.get('phase') == 'turn_end' and
                     isinstance(event.get('reason'), dict) and event['reason'].get('kind') == 'error')):
                    logs.append(json.dumps(event, ensure_ascii=False))
    text = '\n'.join(logs).lower()
    if re.search(r'insufficient[_ ](?:quota|balance)|quota.{0,50}(?:exhaust|exceed)|'
                 r'access denied, please make sure your account is in good standing|'
                 r'(?:weekly|monthly|5.hour) usage limit|credit balance is too low|'
                 r'concurrent request limit|authentication failed|authorization grant is invalid|invalid.api.key|unauthorized|'
                 r'(?:http|status(?: code)?|error(?: code)?)[ :]+40[13]\b', text):
        return 'account_blocked'
    for path in Path(capture).rglob('execution.json'):
        e = read(path)
        if e.get('status') == 'completed':
            continue
        if e.get('phase') in ('environment_setup', 'boundary_probes', 'authentication'):
            return 'setup_blocked'
    if state == 'timeout' or re.search(r'\b429\b|\b50[0234]\b|timed? out|timeout|'
                                      r'econnreset|econnrefused|connection (?:error|reset)|'
                                      r'network error|temporarily unavailable|overloaded', text):
        return 'transient'
    return 'failed'


def worker():
    """Fixed trusted entry point, always launched with the frozen release as cwd."""
    from . import native, episode_runner
    from .credentials import credential_environment
    directory, index, number, credentials = sys.argv[1:]
    directory, index, number = Path(directory).resolve(), int(index), int(number)
    manifest = read(directory / MARKER)
    job = manifest['jobs'][index]
    require(job['index'] == index, 'Worker job changed')
    target = attempt_path(directory, job, number)
    agent = next(a for a in manifest['registry']['agents'] if a['id'] == job['agent'])
    selected = dict(manifest['registry'], agents=[agent])
    root = directory / 'source'
    try:
        with credential_environment(selected, Path(credentials)):
            if job['kind'] == 'single':
                result = native.run_native(root, directory / 'agents.frozen.json', target / 'capture',
                    agent_ids=[agent['id']], task_ids=[job['id']], execute=True)
                from .reproduction import batch_runs
                runs = batch_runs(target / 'capture')[1] if (target / 'capture/batch.json').exists() else []
                meta = read(runs[0] / 'run.json') if runs else {}
                result['status'] = meta.get('status', result['status'])
                result['answer_files'] = [str((runs[0] / 'workspace/answer.md').relative_to(directory))] \
                    if runs and (runs[0] / 'workspace/answer.md').is_file() else []
            else:
                result = episode_runner.run_episode(root, directory / 'agents.frozen.json', job['selector'],
                    agent['id'], target / 'capture', execute=True)
                result['answer_files'] = [str(p.relative_to(directory)) for p in sorted(
                    (target / 'capture').glob('stages/*/answer.md'))]
        category = 'setup_blocked' if result['status'] == 'setup_required' else classify(result, target / 'capture')
    except KeyboardInterrupt:
        result, category = {'status': 'interrupted', 'exit_code': 130}, 'interrupted'
    except (ValueError, OSError, KeyError, TypeError) as error:
        import traceback
        traceback.print_tb(error.__traceback__, file=sys.stderr)  # Frames only, no credentials or exception values.
        result, category = {'status': 'worker_error', 'error_type': type(error).__name__, 'exit_code': 1}, 'setup_blocked'
    write(target / 'result.json', dict(result, category=category, finished_at=now()))


def status(directory):
    directory = Path(directory).resolve()
    m, state = read(directory / MARKER), read(directory / 'parallel-state.json')
    counts = dict(Counter(r['status'] for r in state['jobs']))
    return {'batch': str(directory), 'official': False, 'running': state['running'],
            'planned': len(m['jobs']), 'statuses': counts, 'blocked_groups': state['blocked_groups'],
            'concurrency_by_product': m['concurrency_by_product'],
            'new_attempts': sum(len(r['attempts']) for r in state['jobs']),
            'active_jobs': [{'agent': j['agent'], 'task': j['id'], 'attempt': r['attempts'][-1]['number']}
                            for j, r in zip(m['jobs'], state['jobs']) if r['status'] == 'running'],
            'status': 'completed' if counts.get('completed') == len(m['jobs']) else 'incomplete',
            'exit_code': 0 if counts.get('completed') == len(m['jobs']) else 1}


def save_state(directory, state):
    state['updated_at'] = now()
    write(Path(directory) / 'parallel-state.json', state)
    write(Path(directory) / 'parallel-status.json', status(directory))


def run(directory, credentials_registry):
    directory = Path(directory).resolve()
    with file_lock(directory / '.scheduler.lock', blocking=False, busy_message='Recovery already running'):
        m = verify(directory, prior_files=True)
        state = read(directory / 'parallel-state.json')
        validate_state(directory, m, state)
        require(not any(r['status'] == 'running' for r in state['jobs']),
                'Unresolved worker; inspect owned processes before recovery')
        agents = {a['id']: a for a in m['registry']['agents']}
        active = {}
        stop = False
        def stopping(signum, frame):
            nonlocal stop
            stop = True
        previous = {sig: signal.signal(sig, stopping) for sig in (signal.SIGINT, signal.SIGTERM)}
        state['running'] = True
        state['scheduler_pid'] = os.getpid()
        save_state(directory, state)
        try:
            while True:
                if (directory / 'stop-request.json').exists():
                    stop = True
                for index, (proc, handles, group, number) in list(active.items()):
                    if stop and not getattr(proc, '_recovery_signalled', False):
                        proc.send_signal(signal.SIGINT)
                        proc._recovery_signalled = True
                    if proc.poll() is None:
                        continue
                    handles.close()
                    row, job = state['jobs'][index], m['jobs'][index]
                    target = attempt_path(directory, job, number)
                    result = read(target / 'result.json') if (target / 'result.json').exists() else {
                        'status': 'worker_error', 'category': 'setup_blocked', 'exit_code': proc.returncode}
                    from .formal_public import tree_hashes
                    row['attempts'][-1].update(result=result, finished_at=now(), files=tree_hashes(target))
                    category = result['category']
                    if category == 'completed':
                        row.update(status='completed', selected={'attempt': number, 'path': str(target.relative_to(directory))})
                    elif category == 'transient' and remaining_attempts(job, row) > 0 and not stop:
                        row.update(status='retry_wait', retry_at=time.time() + m['policy']['retry_delay_seconds'])
                    else:
                        row['status'] = 'interrupted' if category == 'interrupted' else 'failed'
                    # A task failure needs diagnosis, but must not strand unrelated
                    # queued questions. Shared setup/account failures still pause.
                    if category in ('setup_blocked', 'account_blocked'):
                        state['blocked_groups'][group] = {'reason': category, 'job': index, 'at': now()}
                    print(f"{job['agent']} / {job['id']} / a{number}: {result['status']} ({category})", file=sys.stderr, flush=True)
                    del active[index]
                    save_state(directory, state)
                if not stop:
                    counts = Counter(v[2] for v in active.values())
                    per_agent = Counter(m['jobs'][i]['agent'] for i in active)
                    for row, job in zip(state['jobs'], m['jobs']):
                        group = agents[job['agent']]['cli']
                        if (row['status'] not in ('queued', 'retry_wait') or row['retry_at'] > time.time() or
                                group in state['blocked_groups'] or counts[group] >= m['concurrency_by_product'][group]):
                            continue
                        if group == 'codex' and per_agent[job['agent']] >= 1:
                            continue
                        if remaining_attempts(job, row) <= 0:
                            row['status'] = 'failed'; continue
                        number = len(row['attempts']) + 1
                        target = attempt_path(directory, job, number)
                        require(not target.exists(), 'Attempt directory already exists; preserve before continuing')
                        target.mkdir(parents=True)
                        handles = ExitStack()
                        stdout = handles.enter_context((target / 'worker.stdout.log').open('xb'))
                        stderr = handles.enter_context((target / 'worker.stderr.log').open('xb'))
                        for path in target.glob('*.log'): path.chmod(0o600)
                        args = [sys.executable, '-B', '-c', WORKER, str(directory), str(job['index']),
                                str(number), str(Path(credentials_registry).resolve())]
                        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1'); env.pop('PYTHONPATH', None)
                        try:
                            proc = subprocess.Popen(args, cwd=directory / 'source', env=env,
                                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                        except BaseException:
                            handles.close(); raise
                        row['attempts'].append({'number': number, 'pid': proc.pid, 'started_at': now()})
                        row['status'] = 'running'
                        active[job['index']] = (proc, handles, group, number)
                        counts[group] += 1
                        per_agent[job['agent']] += 1
                        print(f"Starting {job['agent']} / {job['id']} / a{number}", file=sys.stderr, flush=True)
                        save_state(directory, state)
                eligible = any(r['status'] in ('queued', 'retry_wait') and
                    agents[j['agent']]['cli'] not in state['blocked_groups']
                    for r, j in zip(state['jobs'], m['jobs']))
                if not active and (stop or not eligible):
                    break
                time.sleep(0.25)
        finally:
            # Signal only children created by this scheduler. Their capture finally
            # blocks terminate their own sandbox groups and preserve partial files.
            for proc, _, _, _ in active.values():
                if proc.poll() is None: proc.send_signal(signal.SIGINT)
            for proc, handles, _, _ in active.values():
                proc.wait(); handles.close()
            state['running'] = False
            save_state(directory, state)
            for sig, handler in previous.items(): signal.signal(sig, handler)
        return status(directory)


def validate_state(directory, manifest, state):
    from .formal_public import tree_hashes
    require(len(state['jobs']) == len(manifest['jobs']), 'Recovery state lost jobs')
    for job, row in zip(manifest['jobs'], state['jobs']):
        require(row['index'] == job['index'], 'Recovery job order changed')
        require(row['status'] in ('queued', 'retry_wait', 'running', 'completed', 'failed',
                                 'interrupted', 'legacy_review'), 'Unknown recovery state')
        exclusions = manifest.get('previous_recovery', {}).get('budget_exclusions', {}).get(str(job['index']), {})
        require(row.get('budget_exclusions', {}) == exclusions, 'Retry budget exclusion changed')
        require(len(row['attempts']) <= 2 + len(exclusions), 'Too many recovery attempts')
        for number, attempt in enumerate(row['attempts'], 1):
            require(attempt['number'] == number, 'Attempt order changed')
            require('files' in attempt, 'Unresolved worker; inspect owned processes before recovery')
            target = attempt_path(directory, job, number)
            require(tree_hashes(target) == attempt['files'], 'Recovery attempt changed')
            require(not (target / 'result.json').exists() or read(target / 'result.json') == attempt['result'],
                    'Recovery result changed')
        if row['status'] == 'completed':
            if str(job['index']) in manifest.get('reused_runs', {}):
                reused = manifest['reused_runs'][str(job['index'])]
                require(not row['attempts'] and row['selected'] == {'reused': str(job['index'])} and
                        file_hash(Path(reused['path']) / 'workspace/answer.md') == reused['answer_sha256'],
                        'Reused answer or selection changed')
            elif row['selected'].get('attempt'):
                selected = row['selected']['attempt']
                require(1 <= selected <= len(row['attempts']) and
                        row['attempts'][selected - 1]['result']['category'] == 'completed', 'Invalid selected attempt')
            else:
                action = row.get('audit', {}).get('action')
                path = (prior_path(manifest, job) / 'workspace/answer.md' if action == 'retain' else
                        Path(directory) / 'recovered' / str(job['index']) / 'answer.md')
                require(action in ('retain', 'recover') and file_hash(path) == row['audit']['answer_sha256'],
                        'Unaudited legacy result')


def recover_written_answer(cli, trajectory):
    """Extract a fully acknowledged write, never execute commands or use reasoning.

    Unknown tools after the write invalidate the candidate. This intentionally
    declines recovery for truncated logs, edits and shell-based file generation.
    """
    records = [(i, json.loads(line)) for i, line in enumerate(
        regular_bytes(trajectory).decode('utf-8').splitlines(), 1) if line.strip()]
    require(cli in ('kimi', 'qwen'), 'No lossless write recovery for this CLI')
    candidate = None
    pending = {}
    read_only = {'ReadFile', 'Read', 'Grep', 'Glob', 'SearchWeb', 'FetchURL',
                 'read_file', 'read_many_files', 'grep_search', 'glob', 'web_search', 'web_fetch'}
    for line, record in records:
        calls, results = [], []
        if cli == 'kimi':
            for tc in record.get('tool_calls', []):
                f = tc['function']
                calls.append((tc['id'], f['name'], json.loads(f['arguments'])))
            if record.get('role') == 'tool':
                results.append((record.get('tool_call_id'), record.get('content'), False))
        else:
            content = record.get('message', {}).get('content', [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get('type') == 'tool_use':
                    calls.append((block['id'], block['name'], block['input']))
                elif block.get('type') == 'tool_result':
                    results.append((block.get('tool_use_id'), block.get('content'), block.get('is_error', False)))
        for ident, name, args in calls:
            if name in read_only:
                continue
            # Qwen commonly verifies its write with wc. Accept only wc + options +
            # one answer path, never an arbitrary shell expression from the log.
            if name == 'run_shell_command':
                try:
                    words = shlex.split(args.get('command', ''))
                except ValueError:
                    words = []
                if (len(words) >= 2 and words[0] == 'wc' and
                        all(re.fullmatch(r'-[lcmw]+', w) for w in words[1:-1]) and
                        re.fullmatch(r'(?:/[A-Za-z0-9_./-]+/)?answer\.md', words[-1])):
                    continue
            candidate = None
            pending.clear()
            path = args.get('path') if cli == 'kimi' else args.get('file_path')
            if name == ('Write' if cli == 'kimi' else 'write_file') and isinstance(path, str) and \
                    Path(path).name == 'answer.md' and isinstance(args.get('content'), str) and args['content'].strip():
                require(not record.get('truncated'), 'Truncated tool write')
                pending[ident] = (args['content'], line, path)
        for ident, content, error in results:
            if ident not in pending:
                continue
            body, call_line, path = pending.pop(ident)
            if error or not isinstance(content, str):
                continue
            if cli == 'kimi':
                match = re.fullmatch(r'Wrote (\d+) bytes to ' + re.escape(path) + r'\s*', content)
                accepted = match and int(match[1]) == len(body.encode('utf-8'))
            else:
                accepted = content.strip() == 'Successfully created and wrote to new file: ' + path + '.' or \
                           content.strip() == 'Successfully overwrote file: ' + path + '.'
            if accepted:
                candidate = (body, {'call_line': call_line, 'result_line': line, 'tool_call_id': ident,
                                   'trajectory_sha256': file_hash(trajectory)})
    require(candidate is not None, 'No complete acknowledged final file write in trajectory')
    return candidate


def audit(directory, decisions_path):
    """Apply explicit organizer capture decisions without editing any original attempt."""
    directory = Path(directory).resolve()
    from .formal_public import tree_hashes
    with file_lock(directory / '.scheduler.lock', blocking=False, busy_message='Stop scheduler before capture audit'):
        m = verify(directory)
        state = read(directory / 'parallel-state.json')
        decisions = read(decisions_path)
        require(isinstance(decisions, list) and len({d['index'] for d in decisions}) == len(decisions), 'Invalid capture audit')
        for d in decisions:
            index = d['index']; require(type(index) is int and 0 <= index < len(m['jobs']), 'Invalid audit index')
            row, job = state['jobs'][index], m['jobs'][index]
            require(row['status'] == 'legacy_review' and not row.get('audit'), 'Capture already resolved')
            source = prior_path(m, job)
            require(tree_hashes(source) == job['prior_files'], 'Prior capture changed')
            require(d.get('reason') and d['action'] in ('retain', 'recover', 'recapture'), 'Audit requires an action and reason')
            if d['action'] == 'retain':
                path = source / 'workspace/answer.md'
                require(d.get('answer_sha256') == file_hash(path), 'Audited answer changed')
                row.update(status='completed', selected={'prior': str(source), 'answer': str(path),
                    'provenance': 'legacy_final_response_capture_audited_not_relabelled'})
            elif d['action'] == 'recover':
                agent = next(a for a in m['registry']['agents'] if a['id'] == job['agent'])
                body, receipt = recover_written_answer(agent['cli'], source / 'workspace/trajectory.jsonl')
                path = directory / 'recovered' / str(index) / 'answer.md'
                require(not path.exists() or regular_bytes(path) == body.encode('utf-8'), 'Recovered answer differs')
                path.parent.mkdir(parents=True)
                if not path.exists():
                    path.write_bytes(body.encode('utf-8'))
                d = dict(d, answer_sha256=file_hash(path), extraction=receipt)
                write(path.parent / 'receipt.json', d)
                row.update(status='completed', selected={'prior': str(source), 'answer': str(path),
                    'provenance': 'lossless_acknowledged_tool_write_extraction'})
            else:
                row.update(status='queued', selected=None)
            row['audit'] = dict(d, recorded_at=now())
        save_state(directory, state)
    return status(directory)
