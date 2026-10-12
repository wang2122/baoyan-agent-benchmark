"""Compare independently recorded reviews and preserve human adjudication notes."""
from collections import Counter
from itertools import combinations
import math
from pathlib import Path

from .core import digest, guard_private_output, read, require
from .reports import markdown_cell, save_report
from .reproduction import batch_context, batch_runs
from .statistics import context_identity, mean, reviewed_profile, rounded
from .submissions import verify_results


def krippendorff_alpha(units, scale):
    """Coincidence-weighted alpha; pending ratings are missing, never agreement."""
    require(scale in ('nominal', 'interval'), 'Unknown reliability scale')
    eligible = [[value for value in unit if value is not None] for unit in units]
    eligible = [unit for unit in eligible if len(unit) >= 2]
    ratings = [value for unit in eligible for value in unit]
    count = len(ratings)
    if count < 2:
        return None
    def distance(left, right):
        return float(left != right) if scale == 'nominal' else (left - right) ** 2
    observed = math.fsum(math.fsum(distance(left, right) for i, left in enumerate(unit)
                                 for j, right in enumerate(unit) if i != j) / (len(unit) - 1)
                         for unit in eligible) / count
    marginal = Counter(ratings)
    expected = math.fsum(a_count * b_count * distance(a, b)
                         for a, a_count in marginal.items() for b, b_count in marginal.items()) / (count * (count - 1))
    return rounded(1 - observed / expected) if expected else None


def attempt_key(meta):
    return f"{meta['adapter']}/{meta['task_id']}/r{meta['repeat']}"


def candidate_signature(judgment):
    # Ignore judge-local occurrence IDs and prose; preserve independently scored identities/states.
    return sorted({(row['canonical_candidate_key'], row['school_key'], row['treatment'], row['validity'])
                   for row in judgment['recommendation_review']})


def compare_reviews(root, batches, out, private_root=None):
    require(len(batches) >= 2 and len({str(Path(b).resolve()) for b in batches}) == len(batches),
            'Provide at least two distinct reviewed batches')
    contexts, plans, sets, profiles = [], [], [], []
    for index, batch in enumerate(batches):
        context, rows = batch_context(root, batch, private_root)
        guard_private_output('test' if context['suite'] == 'test' else 'dev', out, root)
        verify_results(root, batch, private_root)
        manifest, runs = batch_runs(batch)
        contexts.append(context); plans.append(digest(manifest['plan']))
        records, per_set_profiles, settings = {}, set(), None
        for run in runs:
            meta = read(run / 'run.json')
            key = attempt_key(meta)
            record = {'meta': meta, 'judgment': None, 'score': None}
            if meta['status'] == 'completed':
                require((run / 'judgment.json').is_file() and (run / 'score.json').is_file(),
                        'Every completed answer needs a recorded review, including pending judgments')
                profile, settings = reviewed_profile(run, meta)
                per_set_profiles.add(profile)
                record.update(judgment=read(run / 'judgment.json'), score=read(run / 'score.json'))
            records[key] = record
        require(len(per_set_profiles) == 1, 'Each review set needs one recorded reviewer profile and completed answers')
        profile = per_set_profiles.pop()
        profiles.append(profile)
        sets.append({'id': f'reviewer-{index + 1}', 'profile_sha256': profile, 'settings': settings, 'records': records})
    require(len({context_identity(context) for context in contexts}) == 1 and len(set(plans)) == 1,
            'Review sets must use the same dataset, protocol, workflow and attempt plan')
    require(len(set(profiles)) == len(profiles), 'Duplicate reviewer profile; this is not a distinct review set')
    context = contexts[0]
    first = sets[0]['records']
    for review_set in sets[1:]:
        require(set(review_set['records']) == set(first), 'Review attempts differ')
        for key, record in first.items():
            other = review_set['records'][key]['meta']
            meta = record['meta']
            fields = ('status', 'answer_file_sha256', 'model', 'adapter_version', 'experiment_sha256',
                      'prepared_experiment_sha256', 'task_sha256', 'rubric_sha256', 'protocol_sha256')
            require(all(meta.get(field) == other.get(field) for field in fields),
                    'Review sets must contain identical original attempts/answers/settings')
    disagreements, quality_units, coverage_units, comparisons = [], [], [], []
    known_counts = Counter()
    source_reviews = []
    for key, record in sorted(first.items()):
        if record['meta']['status'] != 'completed':
            continue
        judgments = [review_set['records'][key]['judgment'] for review_set in sets]
        results = [review_set['records'][key]['score'] for review_set in sets]
        source_reviews.append({'attempt': key, 'task_id': record['meta']['task_id'],
            'answer_file_sha256': record['meta']['answer_file_sha256'],
            'reviews': {review_set['id']: {'judgment_sha256': digest(judgment), 'score_sha256': digest(result),
                         'profile_sha256': review_set['profile_sha256']}
                        for review_set, judgment, result in zip(sets, judgments, results)}})
        def dispute(category, ident, values, pending=False):
            if pending or any(value != values[0] for value in values[1:]):
                disagreements.append({'id': digest([key, category, ident])[:24], 'attempt': key,
                    'category': category, 'item_id': ident,
                    'observed': {review_set['id']: value for review_set, value in zip(sets, values)},
                    'contains_pending': pending})
        for category, field, value_field, units in (('quality', 'nodes', 'grade', quality_units),
                                                    ('coverage', 'coverage', 'status', coverage_units)):
            indexed = [{item['id']: item for item in judgment[field]} for judgment in judgments]
            for ident in sorted(indexed[0]):
                values = [items[ident][value_field] for items in indexed]
                pending = any(value == 'pending' for value in values)
                numeric = [None if value == 'pending' else (int(value == 'hit') if category == 'coverage' else value)
                           for value in values]
                units.append(numeric)
                known_counts[category + '_units'] += 1
                known_counts[category + '_units_with_two_known'] += sum(value is not None for value in numeric) >= 2
                known_counts[category + '_pending_ratings'] += sum(value is None for value in numeric)
                dispute(category, ident, values, pending)
        tiers = [next((node['points'] for node in result['nodes'] if node['kind'] == 'school_tier'), 'not_applicable')
                 for result in results]
        dispute('school_tier', 'tier_points', tiers, any(value is None for value in tiers))
        candidates = [candidate_signature(judgment) for judgment in judgments]
        dispute('candidate_extraction', 'canonical_candidates', candidates)
        totals = [result['score'] for result in results]
        dispute('total_score', 'score', totals, any(value is None for value in totals))
    for left_index, right_index in combinations(range(len(sets)), 2):
        left, right = sets[left_index], sets[right_index]
        differences, grade_counts = [], Counter()
        for key, record in first.items():
            if record['meta']['status'] != 'completed':
                continue
            left_record, right_record = left['records'][key], right['records'][key]
            a, b = left_record['score']['score'], right_record['score']['score']
            if a is not None and b is not None:
                differences.append(a - b)
            for category, field, value_field in (('quality', 'nodes', 'grade'), ('coverage', 'coverage', 'status')):
                lvalues = {row['id']: row[value_field] for row in left_record['judgment'][field]}
                rvalues = {row['id']: row[value_field] for row in right_record['judgment'][field]}
                for ident in lvalues:
                    if 'pending' not in (lvalues[ident], rvalues[ident]):
                        grade_counts[category + '_known_pairs'] += 1
                        grade_counts[category + '_agreed_pairs'] += lvalues[ident] == rvalues[ident]
                    else:
                        grade_counts[category + '_pending_pairs'] += 1
        comparisons.append({'left': left['id'], 'right': right['id'], 'direction': 'left_minus_right',
            'known_total_pairs': len(differences), 'expected_completed_answers': len(source_reviews),
            'total_mean_absolute_difference': rounded(mean([abs(value) for value in differences])),
            'total_mean_signed_difference': rounded(mean(differences)),
            'total_max_absolute_difference': max(map(abs, differences)) if differences else None,
            'item_agreement': dict(grade_counts)})
    report = {'schema_version': 'benchmark-calibration.1', 'official': False, 'suite': context['suite'],
        'context_sha256': context_identity(context), 'plan_sha256': plans[0],
        'reviewers': [{key: value for key, value in review_set.items() if key != 'records'} for review_set in sets],
        'completed_answers': len(source_reviews), 'source_reviews': source_reviews,
        'quality_grade_alpha_interval': krippendorff_alpha(quality_units, 'interval'),
        'coverage_alpha_nominal': krippendorff_alpha(coverage_units, 'nominal'),
        'rating_coverage': dict(known_counts), 'pairwise': comparisons, 'disagreements': disagreements,
        'independence': 'reviewer_declared_not_authenticated',
        'measurement_scope': 'pooled_credit_grades_and_coverage_labels_not_all_semantic_facts',
        'note': 'Pending ratings never count as agreement. Alpha is null without adequate ratings or marginal variation. '
                'No automatic teacher approval or universal acceptance threshold; human review remains required.'}
    template = {'schema_version': 'benchmark-adjudication.1', 'comparison_sha256': digest(report),
        'decisions': [{**item, 'status': 'pending', 'resolution': None, 'reason': None, 'reviewer': None,
                       'reviewer_type': None, 'evidence_references': []} for item in disagreements]}
    lines = ['# 多评审校准（预览）', '', f"已完成答卷：{len(source_reviews)}；待处理差异或待核项：{len(disagreements)}。",
        '', f"质量档位区间尺度α：{report['quality_grade_alpha_interval']}；覆盖状态名义尺度α：{report['coverage_alpha_nominal']}。",
        '', '|评审对|有效总分对/完成答卷|总分平均绝对差|总分平均有向差|', '|---|---|---|---|']
    for pair in comparisons:
        cells = (pair['left'] + ' / ' + pair['right'], f"{pair['known_total_pairs']}/{pair['expected_completed_answers']}",
                 pair['total_mean_absolute_difference'], pair['total_mean_signed_difference'])
        lines.append('|' + '|'.join(map(markdown_cell, cells)) + '|')
    lines += ['', '填写adjudication-template.json并另存，使用calibration-adjudicate保存裁决记录。不会改写任何原始judgment或score。', '']
    save_report(out, {'calibration.json': report, 'adjudication-template.json': template}, 'calibration.md', '\n'.join(lines))
    return {'status': 'calibration_compared', 'report': str(Path(out).resolve()),
            'completed_answers': len(source_reviews), 'disagreements': len(disagreements), 'official': False}


def record_adjudication(root, report_dir, decisions_path, out):
    report_dir = Path(report_dir).resolve()
    report = read(report_dir / 'calibration.json')
    require(isinstance(report, dict) and report.get('schema_version') == 'benchmark-calibration.1', 'Unsupported calibration report')
    guard_private_output('test' if report['suite'] == 'test' else 'dev', out, root)
    template, submitted = read(report_dir / 'adjudication-template.json'), read(decisions_path)
    require(isinstance(template, dict) and isinstance(submitted, dict)
            and template.get('schema_version') == submitted.get('schema_version') == 'benchmark-adjudication.1'
            and template.get('comparison_sha256') == submitted.get('comparison_sha256') == digest(report),
            'Adjudication belongs to another comparison')
    immutable = ('id', 'attempt', 'category', 'item_id', 'observed', 'contains_pending')
    allowed = set(immutable) | {'status', 'resolution', 'reason', 'reviewer', 'reviewer_type', 'evidence_references'}
    require(isinstance(report['disagreements'], list) and all(isinstance(item, dict) for item in report['disagreements']),
            'Invalid source disputes')
    expected = {item['id']: item for item in report['disagreements']}
    require(len(expected) == len(report['disagreements']), 'Duplicate source dispute')
    require(isinstance(template['decisions'], list) and len(template['decisions']) == len(expected)
            and all(isinstance(item, dict) for item in template['decisions']), 'Invalid adjudication template')
    template_items = {item['id']: item for item in template['decisions']}
    decisions = submitted['decisions']
    require(isinstance(decisions, list) and all(isinstance(item, dict) for item in decisions) and len(decisions) == len(expected)
            and {item.get('id') for item in decisions} == set(expected) == set(template_items),
            'Adjudication must preserve every dispute exactly once')
    resolved = 0
    for item in decisions:
        original = expected[item['id']]
        require(set(item) == allowed and set(template_items[item['id']]) == allowed
                and all(item.get(key) == original[key] for key in immutable),
                'Source dispute was modified')
        require(item.get('status') in ('resolved', 'pending'), 'Unknown adjudication status')
        if item['status'] == 'resolved':
            require(item.get('reviewer_type') == 'human' and all(isinstance(item.get(key), str) and item[key].strip()
                    for key in ('resolution', 'reason', 'reviewer')), 'Resolved disputes need a human reviewer, decision and reason')
            references = item.get('evidence_references')
            require(isinstance(references, list) and references
                    and all(isinstance(value, str) and value.strip() for value in references),
                    'Resolved disputes need traceable evidence references')
            resolved += 1
    value = {**submitted, 'official': False, 'resolved': resolved, 'pending': len(expected) - resolved,
             'provenance': 'human_declared_not_authenticated', 'effect': 'record_only_original_scores_unchanged'}
    save_report(out, {'adjudication.json': value}, 'adjudication.md',
                f'# 评审裁决记录\n\n已裁决：{resolved}；待处理：{len(expected)-resolved}。\n\n原始判断与成绩未改写；本记录不自动使评分通过教师校准。\n')
    return {'status': 'adjudication_recorded', 'exit_code': 0 if resolved == len(expected) else 1,
            'report': str(Path(out).resolve()), 'official': False}
