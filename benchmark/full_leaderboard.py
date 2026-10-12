"""Preview complete original-task scope and derived episodes without double counting."""
from datetime import datetime, timezone
from pathlib import Path

from .core import digest, read, require
from .runtime_common import file_lock
from . import full_batch, full_judge
from .statistics import cluster_interval, context_identity, mean, rounded


def build(root, batches, samples=2000, seed=0, expected_repeats=None):
    require(batches and len({str(Path(b).resolve()) for b in batches}) == len(batches),
            'Empty or duplicate full result batches')
    groups, observations, profiles, scope_ids = {}, {}, set(), set()
    first = None
    for directory in map(Path, batches):
        if full_judge.is_judged(directory):
            source, plan, header = full_judge.verify(root, directory)
            profiles.add(digest(header['config']))
        else:
            source, plan = directory, full_batch.verify(root, directory)
        first = first or plan
        scope_ids.add(digest({'context': context_identity(plan['context']), 'originals': plan['originals'],
                             'episodes': plan['expected_episodes'], 'components': plan['components']}))
        require(expected_repeats is None or expected_repeats == plan['repeats'], 'Expected repeats differ from frozen plan')
        statuses = full_batch.report(source, plan)['jobs']
        for agent in plan['agents']:
            recipe = {k: v for k, v in agent.items() if k != 'binary'}
            group = groups.setdefault(agent['id'], {'recipe': recipe, 'repeats': plan['repeats'],
                                                    'timeout': plan['timeout_seconds']})
            require(group == {'recipe': recipe, 'repeats': plan['repeats'], 'timeout': plan['timeout_seconds']},
                    'Same agent ID has different recipes')
        for job in statuses:
            key = (job['agent'], job['kind'], job['id'], job['repeat'])
            require(key not in observations, 'Duplicate attempt; do not count the same answer twice')
            result = None
            if full_judge.is_judged(directory):
                scorepath = (directory / 'single/scored/runs' / job['agent'] / job['id'] / f"r{job['repeat']}" / 'score.json'
                             if job['kind'] == 'single' else full_batch.job_path(directory, job) / 'score.json')
                if scorepath.exists():
                    result = read(scorepath)
            original_score = (result.get('score') if job['kind'] == 'single' else
                              result['unit_scores'][job['id']]['score'] if job['kind'] == 'staged' else None) if result else None
            require(result is None or job['status'] == 'completed', 'Scored an incomplete native task')
            observations[key] = dict(job, score=original_score, reviewed=result is not None, result=result)
    require(len(scope_ids) == 1, 'Mixed full dataset/protocol/workflow scope')
    require(len(profiles) <= 1, 'Mixed full judge configurations')
    rows = []
    tasks = {t['id']: t for t in first['originals']}
    for aid, group in sorted(groups.items()):
        repeats = group['repeats']; task_rows = []; values = {}; pending = missing = completed = scored = 0
        for task in first['originals']:
            attempts = []
            for r in range(1, repeats + 1):
                obs = observations.get((aid, task['kind'], task['id'], r), {})
                status = obs.get('status', 'not_attempted'); value = obs.get('score')
                completed += status == 'completed'; scored += value is not None
                pending += bool(obs.get('reviewed') and value is None)
                missing += bool(status == 'completed' and not obs.get('reviewed'))
                attempts.append({'repeat': r, 'status': status, 'score': value, 'stages': obs.get('stages')})
            scores = [r['score'] for r in attempts if r['score'] is not None]
            task_mean = rounded(mean(scores)) if len(scores) == repeats else None
            if task_mean is not None:
                values[task['id']] = task_mean
            task_rows.append({'task_id': task['id'], 'task_kind': task['kind'], 'case_group_id': task['case_group_id'],
                              'mean_score': task_mean, 'scored_repeats': len(scores), 'repeats': attempts})
        episodes = []
        for eid in first['expected_episodes']:
            attempts = []
            for r in range(1, repeats + 1):
                obs = observations.get((aid, 'episode', eid, r), {})
                result = obs.get('result') or {}
                attempts.append({'repeat': r, 'status': obs.get('status', 'not_attempted'), 'stages': obs.get('stages'),
                                 'source_quality_mean': result.get('episode_score'),
                                 'review_status': result.get('status', 'not_reviewed'),
                                 'chain_diagnostics': [{k: row[k] for k in ('id', 'criterion', 'status')}
                                                       for row in result.get('chain_diagnostics', [])],
                                 'unit_scores': {tid: {k: score.get(k) for k in ('score', 'status', 'confirmed_points')}
                                                 for tid, score in result.get('unit_scores', {}).items()}})
            episodes.append({'episode_id': eid, 'repeats': attempts})
        complete = len(values) == len(tasks)
        partitions = {}
        for kind in sorted({t['kind'] for t in tasks.values()}):
            part = [t for t in task_rows if t['task_kind'] == kind]
            numeric = [t['mean_score'] for t in part if t['mean_score'] is not None]
            partitions[kind] = {'mean_score': rounded(mean(numeric)) if len(numeric) == len(part) else None,
                                'expected_tasks': len(part), 'scored_tasks': len(numeric)}
        rows.append({'configuration_id': digest(group)[:16], 'adapter': aid, 'model': group['recipe']['model'],
                     'expected_repeats': repeats, 'expected_attempts': len(tasks) * repeats,
                     'completed_attempts': completed, 'scored_attempts': scored, 'pending_attempts': pending,
                     'missing_reviews': missing, 'complete': complete, 'mean_score': rounded(mean(list(values.values()))) if complete else None,
                     'confidence_interval': cluster_interval(values, tasks, samples, f'{seed}:{aid}') if complete else None,
                     'by_task_kind': partitions, 'tasks': task_rows, 'episodes': episodes,
                     'runtime_evidence': None, 'recipe': group['recipe'],
                     'episodes_complete': all(a['status'] == 'completed' and a['review_status'] == 'reviewed_pilot'
                                              for e in episodes for a in e['repeats']),
                     'episode_aggregation': 'separate_diagnostics_not_added_to_original_task_mean'})
    return {'schema_version': 'benchmark-full-leaderboard.1', 'official': False, 'suite': 'dev',
            'expected_tasks': len(tasks), 'expected_episodes': len(first['expected_episodes']), 'rows': rows,
            'generated_at': datetime.now(timezone.utc).isoformat(), 'context_sha256': next(iter(scope_ids)),
            'reviewer_profiles': sorted(profiles), 'bootstrap_samples': samples, 'seed': seed,
            'aggregation': 'equal_original_task_weight_after_equal_repeat_weight_episodes_separate',
            'verification_scope': 'saved_judgments_and_calculation_not_new_semantic_review',
            'note': '原题分与衍生连续任务分开；缺失或待核不计零分，不给完整均分。'}


def export(root, batches, out, update=False, expected_repeats=None, samples=2000, seed=0):
    from .leaderboard import publish
    out = Path(out).absolute()
    require(not out.is_symlink(), 'Linked leaderboard output')
    with file_lock(out.parent / ('.' + out.name + '.leaderboard.lock'), blocking=False,
                   busy_message='Leaderboard export already running'):
        value = build(root, batches, samples, seed, expected_repeats)
        publish(value, out, update)
    complete = bool(value['rows']) and all(r['complete'] and r['episodes_complete'] for r in value['rows'])
    return {'status': 'leaderboard_complete' if complete else 'leaderboard_preview', 'exit_code': 0 if complete else 1,
            'official': False, 'page': str(out / 'index.html'), 'data': str(out / 'leaderboard.json'),
            'configurations': len(value['rows'])}
