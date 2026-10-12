"""Repeated-run diagnostics from verified scores; no new semantic judgments."""
from collections import Counter, defaultdict
from itertools import combinations
import math
from pathlib import Path
import random
import statistics as standard_statistics

from .core import digest, guard_private_output, read, require
from .reports import markdown_cell, save_report
from .reproduction import batch_context, batch_runs
from .submissions import check_review_settings, verify_results

CONTEXT_FIELDS = ('release_version', 'suite', 'task_ids', 'fingerprints', 'protocol_sha256',
                  'scoring_policy_sha256', 'engine_sha256')


def context_identity(context):
    values = {name: context[name] for name in CONTEXT_FIELDS}
    if context.get('schema_version') == 'benchmark-reproduction.2':
        values.pop('engine_sha256')
        values['components'] = context['components']
    return digest(values)


def mean(values):
    return math.fsum(values) / len(values) if values else None


def rounded(value):
    return round(value, 6) if value is not None else None


def percentile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def cluster_interval(task_values, tasks, samples, seed):
    """Resample whole student groups, retaining task weighting within each draw."""
    require(type(samples) is int and 2 <= samples <= 100000, 'Bootstrap samples must be 2..100000')
    groups = defaultdict(list)
    for ident in sorted(task_values):
        groups[tasks[ident]['case_group_id']].append(task_values[ident])
    if len(groups) < 2:
        return None
    clusters = [groups[name] for name in sorted(groups)]
    rng = random.Random(seed)
    draws = []
    for _ in range(samples):
        selected = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        draws.append(math.fsum(math.fsum(group) for group in selected) / sum(map(len, selected)))
    return {'level': 0.95, 'lower': rounded(percentile(draws, .025)),
            'upper': rounded(percentile(draws, .975)), 'method': 'percentile_case_group_bootstrap',
            'case_groups': len(clusters), 'samples': samples,
            'scope': 'between_case_groups_conditional_on_observed_repeat_means'}


def reviewed_profile(run, meta):
    settings_path = run / 'review-settings.json'
    require(settings_path.is_file() and not settings_path.is_symlink(),
            'Statistics/calibration require recorded reviewer settings')
    settings = read(settings_path)
    require(isinstance(settings, dict), 'Reviewer settings must be an object')
    require(meta.get('review_settings_sha256') == digest(settings), 'Saved reviewer settings changed')
    return check_review_settings(settings, read(run / 'judgment.json')), settings


def report_statistics(root, batches, out, private_root=None, expected_repeats=None, samples=2000, seed=0):
    require(batches and len({str(Path(b).resolve()) for b in batches}) == len(batches), 'Duplicate or empty batches')
    require(type(seed) is int, 'Seed must be an integer')
    require(type(samples) is int and 2 <= samples <= 100000, 'Bootstrap samples must be 2..100000')
    contexts, plans, loaded, reviewer_profiles = [], [], [], set()
    baseline_plans = {}
    for batch in batches:
        context, rows = batch_context(root, batch, private_root)
        guard_private_output('test' if context['suite'] == 'test' else 'dev', out, root)
        verify_results(root, batch, private_root)  # Recompute every saved score, including pending ones.
        manifest, runs = batch_runs(batch)
        contexts.append(context); plans.append(manifest['plan'])
        for baseline in manifest['plan']['baselines']:
            prior = baseline_plans.setdefault(baseline['id'], baseline)
            require(prior == baseline, 'Baseline configuration differs between batches')
        for run in runs:
            meta = read(run / 'run.json')
            require(isinstance(meta.get('model_observation', {}), dict), 'Model observation must be an object')
            result = read(run / 'score.json') if (run / 'score.json').is_file() else None
            if result is not None:
                reviewer_profiles.add(reviewed_profile(run, meta)[0])
            loaded.append((meta, result))
    require(len({context_identity(c) for c in contexts}) == 1, 'Mixed dataset/protocol/workflow versions')
    require(len(reviewer_profiles) <= 1, 'Mixed reviewer profiles; compare separately with calibration-compare')
    context = contexts[0]
    planned_repeats = max(p['repeats'] for p in plans)
    declared = context['protocol'].get('repeats')
    repeats = expected_repeats if expected_repeats is not None else declared or planned_repeats
    require(type(repeats) is int and repeats > 0 and repeats >= planned_repeats,
            'Expected repeats cannot omit planned attempts')
    require(expected_repeats is None or declared is None or expected_repeats == declared,
            'Expected repeats differ from the recorded protocol')
    tasks = {task['id']: task for task, _ in rows}
    groups, identities = {}, set()
    for meta, result in loaded:
        key = (meta['adapter'], meta['adapter_version'], meta['model'], meta['timeout_seconds'],
               meta.get('experiment_sha256'))
        ident = (meta['adapter'], meta['task_id'], meta['repeat'])
        require(ident not in identities, 'Duplicate task/repeat attempt; do not count retries as new repeats')
        identities.add(ident)
        require(meta['repeat'] <= repeats, 'Unexpected repeat number')
        group = groups.setdefault(key, {})
        group[(meta['task_id'], meta['repeat'])] = (meta, result)
    reports, vectors = [], {}
    for key, attempts in sorted(groups.items(), key=lambda item: repr(item[0])):
        config_id = digest(list(key))[:16]
        task_rows, values = [], {}
        for ident, task in tasks.items():
            observed = []
            for repeat in range(1, repeats + 1):
                meta, result = attempts.get((ident, repeat), ({}, None))
                value = result.get('score') if result and meta.get('status') == 'completed' else None
                observed.append({'repeat': repeat, 'status': meta.get('status', 'not_attempted'), 'score': value})
            scores = [row['score'] for row in observed if row['score'] is not None]
            complete = len(scores) == repeats
            if complete:
                values[ident] = mean(scores)
            task_rows.append({'task_id': ident, 'case_group_id': task['case_group_id'],
                'task_kind': task['task_kind'], 'difficulty': task['difficulty'], 'repeats': observed,
                'scored_repeats': len(scores), 'mean_score': rounded(mean(scores)) if complete else None,
                'repeat_stddev': rounded(standard_statistics.stdev(scores)) if complete and repeats > 1 else None})
        complete = len(values) == len(tasks)
        repeat_means, observed_repeat_means = [], []
        for repeat in range(1, repeats + 1):
            scores = [row['repeats'][repeat - 1]['score'] for row in task_rows]
            value = mean(scores) if all(value is not None for value in scores) else None
            observed_repeat_means.append(value)
            repeat_means.append({'repeat': repeat, 'mean_score': rounded(value)})
        partitions = {}
        for field in ('task_kind', 'difficulty'):
            partitions[field] = {}
            for label in sorted({task[field] or 'uncalibrated' for task in tasks.values()}):
                ids = [ident for ident, task in tasks.items() if (task[field] or 'uncalibrated') == label]
                scores = [values[ident] for ident in ids if ident in values]
                partitions[field][label] = {'expected_tasks': len(ids), 'scored_tasks': len(scores),
                    'mean_score': rounded(mean(scores)) if len(scores) == len(ids) else None}
        completed = sum(meta['status'] == 'completed' for meta, _ in attempts.values())
        scored = sum(row['scored_repeats'] for row in task_rows)
        reports.append({'configuration_id': config_id, 'adapter': key[0], 'adapter_version': key[1],
            'model': key[2], 'timeout_seconds': key[3], 'experiment_sha256': key[4],
            'expected_tasks': len(tasks), 'expected_repeats': repeats, 'expected_attempts': len(tasks) * repeats,
            'recorded_attempts': len(attempts), 'scored_attempts': scored, 'completed_attempts': completed,
            'statuses': dict(Counter(meta['status'] for meta, _ in attempts.values())),
            'completion_rate': rounded(completed / (len(tasks) * repeats)), 'complete': complete,
            'mean_score': rounded(mean(list(values.values()))) if complete else None,
            'repeat_mean_stddev': rounded(standard_statistics.stdev(observed_repeat_means))
                if complete and repeats > 1 else None,
            'confidence_interval': cluster_interval(values, tasks, samples, f'{seed}:{config_id}') if complete else None,
            'by_task_kind': partitions['task_kind'], 'by_difficulty': partitions['difficulty'],
            'repeat_means': repeat_means, 'tasks': task_rows,
            'runtime_evidence': {'model_observation_statuses': dict(Counter(meta.get('model_observation', {}).get('status', 'missing')
                                   for meta, _ in attempts.values())),
                'locally_verified_isolation_attempts': sum(bool(meta.get('isolation_verified')) for meta, _ in attempts.values()),
                'required_trajectory_attempts': sum(task['execution'].get('trajectory_required', False) for task in tasks.values()) * repeats,
                'required_trajectory_recorded': sum(tasks[ident]['execution'].get('trajectory_required', False)
                    and meta.get('trajectory_status') == 'recorded_unverified'
                    for (ident, repeat_number), (meta, saved_score) in attempts.items()),
                'scope': 'stored_metadata_not_new_runtime_attestation'}})
        vectors[config_id] = values
    pairs = []
    for left, right in combinations(reports, 2):
        left_id, right_id = left['configuration_id'], right['configuration_id']
        common = set(vectors[left_id]) & set(vectors[right_id])
        differences = {ident: vectors[left_id][ident] - vectors[right_id][ident] for ident in common}
        complete = len(common) == len(tasks)
        pairs.append({'left': left_id, 'right': right_id, 'direction': 'left_minus_right',
            'paired_tasks': len(common), 'expected_tasks': len(tasks), 'complete': complete,
            'mean_difference': rounded(mean(list(differences.values()))) if complete else None,
            'confidence_interval': cluster_interval(differences, tasks, samples, f'{seed}:{left_id}:{right_id}') if complete else None})
    report = {'schema_version': 'benchmark-statistics.1', 'official': False, 'context_sha256': context_identity(context),
        'protocol_sha256': context['protocol_sha256'], 'suite': context['suite'],
        'aggregation': 'equal_task_weight_after_equal_repeat_weight', 'failure_policy': 'incomplete_without_mean',
        'bootstrap_samples': samples, 'seed': seed, 'confidence_level': .95,
        'reviewer_profiles': sorted(reviewer_profiles), 'rows': reports, 'paired_comparisons': pairs,
        'multiple_comparisons': 'unadjusted_descriptive_intervals_no_automatic_ranking',
        'note': 'Full canonical suite required for overall means. Intervals resample student groups after repeat averaging; '
                'they do not model all sources of repeat, reviewer or dataset uncertainty. Fewer than two case groups has no interval.'}
    lines = ['# 重复运行统计（预览）', '', '|配置|模型|已评分/预期尝试|均分|重复均分标准差|95%区间|', '|---|---|---|---|---|---|']
    for row in reports:
        ci = row['confidence_interval']
        cells = (row['adapter'], row['model'], f"{row['scored_attempts']}/{row['expected_attempts']}",
                 row['mean_score'], row['repeat_mean_stddev'], f"[{ci['lower']}, {ci['upper']}]" if ci else '未计算')
        lines.append('|' + '|'.join(map(markdown_cell, cells)) + '|')
    lines += ['', '缺失或待核时总均分为空；配对分差见statistics.json。区间按学生案例组抽样，条件于已观测的重复均分；不自动生成正式排名。', '']
    save_report(out, {'statistics.json': report}, 'statistics.md', '\n'.join(lines))
    return {'status': 'statistics_complete' if all(row['complete'] for row in reports) else 'statistics_incomplete',
            'exit_code': 0 if all(row['complete'] for row in reports) else 1,
            'report': str(Path(out).resolve()), 'official': False, 'configurations': len(reports)}
