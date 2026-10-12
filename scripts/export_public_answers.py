#!/usr/bin/env python3
"""Prepare historical answers and existing scores locally, without execution traces."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_publication import (checksums, clean, export_public_answers,
                                         inside, put, read, safe_id, sha)
from scripts.publication_evidence import scan
from scripts.answer_layout import SOURCES, LABELS, relocate_archive, refresh_checksums


def project_score_summaries(summaries, rows):
    """Project archived scores onto retained tasks without changing any score."""
    result = []
    category_labels = {'择校推荐': 'Recommendations', '规划与决策': 'Planning & decisions',
                       '两阶段任务': 'Two-stage tasks'}
    def stats(own):
        n = len(own)
        scored = [r for r in own if r.get('score') is not None]
        complete = bool(n) and len(scored) == n and not any(r.get('pending_nodes') for r in own)
        bounds = [r.get('score_interval') for r in own]
        return {'expected': n, 'reviewed': sum(r.get('review_status') == 'completed' for r in own),
                'scored': len(scored), 'pending': sum(bool(r.get('pending_nodes')) for r in own),
                'complete': complete, 'review_complete': all(r.get('review_status') == 'completed' for r in own),
                'mean': round(sum(r['score'] for r in scored) / n, 4) if complete else None,
                'score_bounds': [round(sum(b[i] for b in bounds) / n, 4) for i in (0, 1)]
                                if n and all(bounds) else None}
    for original in summaries:
        own = [r for r in rows if r['agent'] == original['agent']]
        row = dict(original)
        row.update(stats(own), snapshot_answers=len(own), statuses={'completed': len(own)},
                   categories={label: stats([r for r in own if r['category'] == category])
                               for category, label in category_labels.items()})
        result.append(row)
    return result


def copy_answers(source, paths, destination, task_id=None):
    if any(Path(name).name != 'answer.md' for name in paths):
        raise ValueError('Selected artifact is not an answer')
    hashes = {name: sha(inside(source, name).read_bytes()) for name in paths}
    if not hashes:
        raise ValueError('Completed record has no archived answer')
    return export_public_answers(source, hashes, destination, task_id=task_id)


def export_dev20(source, out, allowed):
    materials = source / 'materials'
    packets = materials / 'review-materials'
    index = read(packets / 'index.json')
    report = read(materials / 'review-final/results.json')
    report['rows'] = [r for r in report['rows'] if r['task_id'] in allowed]
    report['summary'] = project_score_summaries(report['summary'], report['rows'])
    report['unresolved_facts'] = [r for r in report.get('unresolved_facts', [])
                                  if r.get('task_id') is None or r['task_id'] in allowed]
    if report.get('adjudication', {}).get('task_id') not in allowed:
        report['adjudication'] = {}
    scores = {(row['agent'], row['task_id']): row for row in report['rows']}
    records = []
    seen = set()
    for row in index['records']:
        agent, task = safe_id(row['agent']), safe_id(row['task_id'])
        if task not in allowed:
            continue
        key = (agent, task)
        if key in seen:
            raise ValueError('Duplicate reviewed answer')
        seen.add(key)
        score = scores[key]
        if score['packet'] != row['packet']:
            raise ValueError('Score and answer packet differ')
        packet = inside(packets, row['packet_path'])
        relative = f'{agent}/{task}'
        artifacts = copy_answers(packet, ['answer.md'], out / relative, task_id=task)
        records.append({'agent': agent, 'task_id': task,
                        'answers': [relative + '/' + x['path'] for x in artifacts]})
    if seen != set(scores):
        raise ValueError('Answer and score coverage differ')
    row_fields = ('agent', 'name', 'task_id', 'category', 'stage_count', 'review_status',
                  'score', 'score_interval', 'pending_nodes', 'deductions', 'score_status')
    # Preserve final report scores, including owner adjudications, rather than
    # reconstructing them from earlier reviewer outputs.
    put(out / 'scores.json', clean({
        'selection_id': report['selection_id'], 'generated_at': report['generated_at'],
        'official': False, 'reviewer': report['reviewer'],
        'summary': report['summary'],
        'rows': [{k: row[k] for k in row_fields if k in row} for row in report['rows']],
        'unresolved_facts': report.get('unresolved_facts', []),
        'execution_note': report.get('execution_progress', {}).get('note'),
        'adjudication': {k: report.get('adjudication', {})[k]
                         for k in ('kind', 'agent', 'task_id', 'node', 'grade', 'points',
                                   'max_points', 'reason', 'before', 'after')
                         if k in report.get('adjudication', {})},
        'limitations': 'Historical scores and reported deductions; full item-level judgments and execution traces are private. Not a complete offline score-recomputation package.',
        'source_sha256': sha((materials / 'review-final/results.json').read_bytes()),
    }))
    return records


def export_dev70(source, out, allowed):
    records = []
    seen = set()
    for manifest in sorted((source / 'materials/records').glob('*/*/record.json')):
        data = read(manifest)
        agent = safe_id(data['agent'])
        rows = data.get('records', data.get('execution', {}).get('records', []))
        for row in rows:
            task = safe_id(row.get('task_id', row.get('unit_id')))
            if task not in allowed:
                continue
            record_id = safe_id(manifest.parent.name)
            relative = f'{agent}/{record_id}/{task}'
            if relative in seen:
                raise ValueError('Duplicate archived answer record')
            seen.add(relative)
            entry = {'agent': agent, 'task_id': task, 'record_id': record_id,
                     'status': row['status'], 'answers': [], 'score': None,
                     'scoring_status': 'not_exported',
                     'execution_note': 'Archived selected answer; original failed attempts and retries are retained privately. This is not a uniform first-attempt result.'}
            if row['status'] == 'completed':
                base = manifest.parent
                if 'selected_answers' in row:
                    paths = row['selected_answers']
                else:
                    capture = row.get('capture_path', 'captures/' + row.get('path', ''))
                    folder = inside(base, capture)
                    paths = [p.relative_to(base).as_posix()
                             for p in sorted(folder.glob('stages/S*/answer.md'))]
                    if not paths and (folder / 'workspace/answer.md').is_file():
                        paths = [(folder / 'workspace/answer.md').relative_to(base).as_posix()]
                artifacts = copy_answers(base, paths, out / relative, task_id=task)
                entry['answers'] = [relative + '/' + x['path'] for x in artifacts]
                entry['configuration'] = clean(data.get('configuration'))
                entry['provenance'] = row.get('provenance', 'archived_selected_answer')
            records.append(entry)
    return records


def export(source, out, archive, allowed_tasks=None):
    archive = {name: old for old, name in SOURCES.items()}.get(archive, archive)
    if archive not in ('dev20', 'dev70'):
        raise ValueError('Unsupported historical archive')
    allowed = set(read(ROOT / 'config/selections/current.json')['task_ids']) if allowed_tasks is None else set(allowed_tasks)
    source, out = Path(source).resolve(), Path(out).resolve()
    if out.exists():
        raise ValueError('Destination already exists; use a new output directory')
    out.mkdir(parents=True)
    records = export_dev20(source, out, allowed) if archive == 'dev20' else export_dev70(source, out, allowed)
    coverage = Counter(row['agent'] for row in records if row['answers'])
    put(out / 'manifest.json', {
        'archive': archive, 'historical': True, 'official': False,
        'answer_records': sum(coverage.values()), 'per_agent': dict(coverage),
        'execution_trajectories_public': False, 'records': records,
        'scope_task_ids': sorted(allowed),
        'scope': 'Current public selection only. Only answers physically present in the local archive. Not current-version results or evidence of complete campaign coverage.',
    })
    (out / 'README.md').write_text(
        f'# {LABELS[SOURCES[archive]]}\n\n'
        '[答卷索引 / Answer index](manifest.json)' +
        (' · [已有评分与扣分依据 / Existing scores and deductions](scores.json)' if archive == 'dev20' else '') +
        '\n\n历史答卷按原题集和评分版本记录，两阶段题包含各阶段答案。'
        '隐私脱敏及来源、导出哈希记录在各答卷的`artifacts.json`中。'
        '完整执行与评审轨迹内部存档。\n\n'
        'Historical answers retain their dataset and scoring versions, including each stage. '
        'Each answer’s `artifacts.json` records privacy redactions and source/export hashes. '
        'Full execution and reviewer traces remain internal.\n', encoding='utf-8')
    relocate_archive(out, out, archive)
    refresh_checksums(out)
    (out / 'README.md').write_text(
        '# 按模型与运行批次保存的答卷 / Answers by model and run\n\n' +
        ('[历史评分材料](models/)\n\n' if archive == 'dev20' else f'[来源清单](sources/{SOURCES[archive]}/manifest.json)\n\n') +
        '实际材料位于 `models/<模型>/runs/<批次>/<题目>/`，清单路径相对于本目录。\n', encoding='utf-8')
    privacy = scan(out)
    if privacy['status'] != 'no_pattern_findings':
        raise ValueError('Privacy patterns remain; keep this draft local for inspection')
    checksums(out)
    return {'archive': SOURCES[archive], 'manifest': None if archive == 'dev20' else f'sources/{SOURCES[archive]}/manifest.json', 'answer_records': sum(coverage.values()),
            'per_agent': dict(coverage), 'out': str(out), 'uploaded': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, choices=('retained-early-scores', 'selected-historical-answers', 'dev20', 'dev70'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    archive = {name: old for old, name in SOURCES.items()}.get(args.archive, args.archive)
    source = args.source or ROOT / 'experiments/execution-records' / ('dev75' if archive == 'dev70' else archive)
    print(json.dumps(export(source, args.out, args.archive), ensure_ascii=False))


if __name__ == '__main__':
    main()
