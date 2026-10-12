"""Command execution and reproducible aggregation. Working directories are not sandboxes."""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from .metrics import metrics, validate_usage
from .core import candidate, digest, file_hash, guard_private_output, query_prompt, read, require, text_hash, write
from .runtime_common import regular_bytes


def prepare(task, rubric, out, protocol):
    out = Path(out).resolve()
    guard_private_output(task['split'], out)
    require(not out.exists(), 'Run directory already exists')
    (out / 'workspace').mkdir(parents=True)
    write(out / 'workspace/task.json', candidate(task))
    (out / 'workspace/query.md').write_text(query_prompt(task), encoding='utf-8')
    # Organizer state stays outside workspace; command adapters still need external sandboxing.
    write(out / 'task.json', task)
    write(out / 'rubric.json', rubric)
    write(out / 'protocol.json', protocol)
    policy = Path(__file__).resolve().parents[1] / 'docs/scoring.md'
    require(file_hash(policy) == rubric['scoring_policy_sha256'], 'Scoring policy mismatch')
    (out / 'scoring.md').write_text(policy.read_text(encoding='utf-8'), encoding='utf-8')
    meta = {'task_id': task['id'], 'task_sha256': digest(task), 'rubric_sha256': digest(rubric),
            'protocol_sha256': digest(protocol), 'split': task['split'], 'status': 'prepared',
            'adapter': None, 'adapter_version': None, 'model': None, 'repeat': None,
            'official': False, 'isolation_verified': False}
    write(out / 'run.json', meta)
    return out


def trusted_launcher(command, adapter, version, model):
    """Recognize only the pinned in-repository launcher, never an adapter's self-report."""
    from .adapters import ROOT, adapter_build
    try:
        if (len(command) != 7 or Path(command[0]).resolve() != Path(sys.executable).resolve()
                or Path(command[1]).resolve() != ROOT / 'scripts/baselines.py'
                or command[2] != '--registry' or command[4:] != ['launch', '--baseline', adapter]):
            return False
        registry = read(Path(command[3]))
        matches = [b for b in registry['baselines'] if b['id'] == adapter]
        return (registry.get('adapter_build_sha256') == adapter_build() and len(matches) == 1
                and matches[0].get('model') == model and matches[0].get('version') == version
                and matches[0].get('execution_mode') != 'manual_app'
                and matches[0].get('command') == command)
    except (ValueError, OSError, KeyError, TypeError):
        return False


def execute(out, command, adapter, adapter_version, model, repeat, timeout):
    out = Path(out).resolve()
    meta = read(out / 'run.json')
    require(meta['status'] == 'prepared', 'Run already attempted; use a fresh directory for retries')
    require(isinstance(command, list) and command and all(isinstance(a, str) for a in command),
            '--command must be a JSON argv list, not a shell string')
    require(timeout > 0 and repeat > 0, 'Timeout and repeat must be positive')
    require(all(x.strip() for x in (adapter, adapter_version, model)), 'Adapter/model versions required')
    require(meta['task_sha256'] == digest(read(out / 'task.json')), 'Task snapshot changed')
    require(meta['rubric_sha256'] == digest(read(out / 'rubric.json')), 'Rubric snapshot changed')
    require(meta['protocol_sha256'] == digest(read(out / 'protocol.json')), 'Protocol snapshot changed')
    work = out / 'workspace'
    require(digest(read(work / 'task.json')) == digest(candidate(read(out / 'task.json')))
            and (work / 'query.md').read_text(encoding='utf-8') == query_prompt(read(out / 'task.json')),
            'Candidate inputs changed since preparation')
    env = os.environ.copy()
    env.update(BENCH_TASK=str(work / 'task.json'), BENCH_QUERY=str(work / 'query.md'),
               BENCH_ANSWER=str(work / 'answer.md'), BENCH_TRAJECTORY=str(work / 'trajectory.jsonl'),
               BENCH_USAGE=str(work / 'usage.json'))
    trusted = trusted_launcher(command, adapter, adapter_version, model)
    report_path = out / 'adapter-isolation.json'
    env.pop('BENCH_ISOLATION_REPORT', None)
    if trusted:
        require(not report_path.exists(), 'Organizer isolation report already exists')
        env['BENCH_ISOLATION_REPORT'] = str(report_path)
    meta.update(adapter=adapter, adapter_version=adapter_version, model=model, repeat=repeat,
                timeout_seconds=timeout, status='running', started_at=datetime.now(timezone.utc).isoformat())
    write(out / 'run.json', meta)
    start = time.monotonic()
    status, exit_code = 'failed', None
    try:
        # Do not persist argv or environment: either may contain credentials.
        with (out / 'adapter.stdout.log').open('wb') as stdout, (out / 'adapter.stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, cwd=work, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                exit_code = process.wait(timeout=timeout)
                status = 'completed' if exit_code == 0 else 'failed'
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                # Only terminate the process group this call created.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'timeout'
    except OSError:
        status = 'launch_failed'
    # Candidate artifacts can be malformed or unsafe even when the child exits 0.
    # Always finish the run record; do not leave it running after a decoding error.
    problems = []
    answer = work / 'answer.md'
    answer_hash = None
    if answer.exists() or answer.is_symlink():
        try:
            data = regular_bytes(answer)
            import hashlib
            answer_hash = hashlib.sha256(data).hexdigest()
            if status == 'completed' and not data.decode('utf-8').strip():
                status = 'missing_answer'
        except (ValueError, OSError, UnicodeError):
            problems.append('answer.md')
    elif status == 'completed':
        status = 'missing_answer'
    trace = work / 'trajectory.jsonl'
    trace_status = 'missing'
    if trace.exists() or trace.is_symlink():
        try:
            events = [json.loads(line) for line in regular_bytes(trace).decode('utf-8').splitlines() if line.strip()]
            trace_status = 'recorded_unverified' if events and all(isinstance(e, dict) for e in events) else 'invalid'
        except (ValueError, OSError, UnicodeError):
            trace_status = 'invalid'
    trace_required = read(out / 'task.json')['execution'].get('trajectory_required', False)
    if trace_required and trace_status != 'recorded_unverified' and status == 'completed':
        problems.append('trajectory.jsonl')
    usage_path = work / 'usage.json'
    usage, usage_status = None, 'missing'
    if usage_path.exists() or usage_path.is_symlink():
        try:
            usage = validate_usage(json.loads(regular_bytes(usage_path).decode('utf-8')))
            usage_status = 'adapter_reported_unverified'
        except (ValueError, OSError, TypeError, UnicodeError):
            usage_status = 'invalid'
    isolation_path = report_path if trusted else work / 'isolation.json'
    meta['isolation_verified'] = False
    meta['isolation_evidence_source'] = 'missing_report'
    if isolation_path.exists() or isolation_path.is_symlink():
        try:
            isolation = json.loads(regular_bytes(isolation_path).decode('utf-8'))
            # Trusted-adapter evidence, never a claim of a VM/security audit or formal eligibility.
            meta['isolation'] = isolation
            meta['isolation_report_sha256'] = file_hash(isolation_path)
            meta['isolation_verified'] = (trusted and isolation.get('backend') == 'macos-seatbelt-provider-proxy-v1'
                and isolation.get('status') == 'passed' and isolation.get('cleanup') == 'complete'
                and isolation.get('scope') == 'local_boundary_probes')
            meta['isolation_evidence_source'] = 'trusted_adapter_local_probes' if trusted else 'untrusted_adapter_report'
        except (ValueError, OSError, TypeError, AttributeError, UnicodeError):
            meta['isolation_verified'] = False
            meta['isolation_evidence_source'] = 'invalid_report'
    meta.update(usage=usage, usage_status=usage_status)
    for name, field in (('model-observation.json', 'model_observation'), ('failure.json', 'failure')):
        artifact = work / name
        if artifact.exists() or artifact.is_symlink():
            try:
                value = json.loads(regular_bytes(artifact).decode('utf-8'))
                require(isinstance(value, dict), 'Adapter metadata must be an object')
                if field == 'model_observation':
                    require(isinstance(value.get('status'), str), 'Missing model observation status')
                    require(isinstance(value.get('reported', []), list)
                            and all(isinstance(m, str) for m in value.get('reported', [])), 'Invalid reported models')
                meta[field] = value
            except (ValueError, OSError, UnicodeError):
                problems.append(name)
                if field == 'model_observation':
                    meta[field] = {'requested': model, 'reported': [], 'status': 'invalid'}
    if problems:
        if status == 'completed':
            status = 'failed'
        meta['failure'] = {'phase': 'output_validation', 'exception_type': 'ValueError',
                          'artifacts': problems, 'detail': 'Invalid adapter artifacts; inspect private run files.'}
    meta.update(status=status, exit_code=exit_code, elapsed_seconds=round(time.monotonic()-start, 3),
                finished_at=datetime.now(timezone.utc).isoformat(),
                trajectory_status=trace_status, trajectory_required=trace_required,
                answer_file_sha256=answer_hash)
    write(out / 'run.json', meta)
    return meta


def aggregate(rows, runs, protocol):
    require(bool(rows), 'No tasks selected; no leaderboard can be computed')
    expected = {task['id']: task for task, _ in rows}
    groups = {}
    seen = set()
    judge_versions = set()
    reviewer_profiles = set()
    reviewed_with_settings = []
    details = {}
    for directory in runs:
        directory = Path(directory)
        meta = read(directory / 'run.json')
        require(meta['task_id'] in expected, 'Run outside selected split')
        task = expected[meta['task_id']]
        require(meta['task_sha256'] == digest(task), 'Mixed task versions')
        require(meta['protocol_sha256'] == digest(protocol), 'Mixed protocols')
        require(meta.get('adapter') and meta.get('adapter_version') and meta.get('model'), 'Missing baseline identity')
        if meta.get('experiment') is not None:
            require(meta.get('experiment_sha256') == digest(meta['experiment']), 'Experiment metadata changed')
        key = (meta['adapter'], meta['adapter_version'], meta['model'], meta['timeout_seconds'], meta['repeat'],
               meta.get('experiment_sha256'))
        identity = (*key, task['id'])
        require(identity not in seen, 'Duplicate task attempt; do not select best-of retries')
        seen.add(identity)
        bucket = groups.setdefault(key, {})
        value, scored = None, None
        score_path = directory / 'score.json'
        if score_path.exists():
            scored = read(score_path)
            require(scored['task_sha256'] == digest(task) and scored['rubric_sha256'] == task['rubric_sha256'],
                    'Stale score')
            answer_path = directory / 'workspace/answer.md'
            require(answer_path.is_file() and not answer_path.is_symlink(), 'Missing answer')
            require(file_hash(answer_path) == meta['answer_file_sha256'], 'Original answer changed after capture')
            answer = answer_path.read_text()
            require(scored['answer_sha256'] == text_hash(answer), 'Score/answer mismatch')
            # Recompute from the saved judgment, not a hand-edited numeric score.
            from .scoring import score
            actual = score(task, read(directory / 'rubric.json'), answer, read(directory / 'judgment.json'))
            require(actual == scored, 'Score is not reproducible from saved judgment')
            judge_versions.add(scored['judge_version'])
            require(len(judge_versions) == 1, 'Mixed judge versions; scores are not comparable')
            settings_path = directory / 'review-settings.json'
            reviewed_with_settings.append(settings_path.exists())
            if settings_path.exists():
                from .submissions import check_review_settings
                settings = read(settings_path)
                require(meta.get('review_settings_sha256') == digest(settings), 'Saved reviewer settings changed')
                reviewer_profiles.add(check_review_settings(settings, read(directory / 'judgment.json')))
                require(len(reviewer_profiles) == 1, 'Mixed reviewer identities/settings; preserve separate score sets')
            if meta['status'] == 'completed':
                value = scored['score']
        bucket[task['id']] = value
        details.setdefault(key, {})[task['id']] = {'meta': meta, 'score': scored if value is not None else None}
    require(not any(reviewed_with_settings) or all(reviewed_with_settings),
            'Cannot mix recorded reviewer settings with unknown legacy reviewers')
    reports = []
    for key, bucket in sorted(groups.items(), key=lambda item: repr(item[0])):
        values = [x for x in bucket.values() if x is not None]
        complete = len(values) == len(expected)
        reports.append(dict(adapter=key[0], adapter_version=key[1], model=key[2], timeout_seconds=key[3],
                            repeat=key[4], experiment_sha256=key[5],
                            experiment=next(iter(details[key].values()))['meta'].get('experiment'),
                            expected_tasks=len(expected), attempted_tasks=len(bucket),
                            scored_tasks=len(values), pending_or_missing_tasks=len(expected)-len(values),
                            mean_score=round(sum(values)/len(values), 2) if complete else None,
                            official=False, **metrics(expected, details[key], protocol)))
    return {'status': 'unreviewed_results', 'official': False,
            'protocol_sha256': digest(protocol), 'rows': reports,
            'note': '不按不完整覆盖率计算均分，不挑选最佳重试；不同重复分别保留。成功率阈值未冻结时为null；完成率不等于任务成功率，成本为适配器自报，正式排名与置信区间待协议冻结。'}
