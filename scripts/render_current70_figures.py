#!/usr/bin/env python3
"""Validate the current PNG figures and refresh their checksum manifest.

User-supplied figures are preserved byte-for-byte; retired SVGs are not rendered.
Pass --source to refresh score/category data from an adjudicated local summary.
Pass --update-readmes to refresh only leaderboard rows, retaining image references
and the surrounding bilingual explanations.
"""
import argparse
from collections import Counter
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'experiments/records70/score-summary-70.json'
FIGURES = ROOT / 'docs/figures/dev70-current'
CATEGORIES = ('recommendation_single', 'planning_single', 'staged')
CURRENT_FIGURES = (
    'fig1_en_2l.png', 'fig1_zh_2l.png',
    'fig2_en.png', 'fig2_zh.png',
    'fig3_en_2l.png', 'fig3_zh_2l.png',
    'fig_diff_en.png', 'fig_diff_zh.png',
    'fig_task_en.png', 'fig_task_zh.png',
)
CONFIGURATION_LABELS = {
    'GPT Sol': 'Codex + GPT Sol 6.1',
    'GPT Astra': 'Codex + GPT Astra 6',
    'DeepSeek V4.1 Flash': 'DSH + DeepSeek V4.1 Flash',
    'GLM 5.3 Flash': 'Volcengine Managed Agent + GLM 5.3 Flash',
    'Qwen3.8 Max': 'Qwen Code + Qwen 3.8 Max',
    'Kimi K3': 'Kimi Code + Kimi K3',
    'Gemini 3.8 Flash': 'Antigravity + Gemini 3.8 Flash',
    'Gemini 3.1 Pro': 'Antigravity + Gemini 3.1 Pro',
}


def read(path):
    return json.loads(path.read_text())


def put(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def fmt(bounds):
    return f'{bounds[0]:.2f}' if bounds[0] == bounds[1] else f'{bounds[0]:.2f}–{bounds[1]:.2f}'


def export_scores(source, selected):
    raw = read(source)
    tasks = {r['task_id']: r for r in selected['tasks']}
    counts = Counter(t['category'] for t in tasks.values())
    assert counts == Counter(dict(zip(CATEGORIES, (21, 36, 13))))
    assert raw['summary'] and len(raw['rows']) == 70 * len(raw['summary'])
    ranking = []
    for rank, model in enumerate(raw['summary'], 1):
        own = [r for r in raw['rows'] if r['agent'] == model['agent']]
        assert len(own) == 70 and {r['task_id'] for r in own} == set(tasks)
        assert all(r['question_id'] == tasks[r['task_id']]['question_id'] and r['category'] == tasks[r['task_id']]['category'] for r in own)
        categories = {}
        totals = [sum((Decimal(str(r['interval'][i])) for r in own), Decimal(0)) for i in (0, 1)]
        assert [float(v) for v in totals] == model['total_interval']
        assert [float(v/70) for v in totals] == model['mean_interval']
        for category in CATEGORIES:
            group = [r for r in own if r['category'] == category]
            sums = [sum((Decimal(str(r['interval'][i])) for r in group), Decimal(0)) for i in (0, 1)]
            categories[category] = {'question_count': len(group),
                                    'total_score_interval': [float(v) for v in sums],
                                    'mean_score_interval': [float(v/len(group)) for v in sums]}
        ranking.append({'rank': rank, 'model': model['name'], 'question_count': 70,
                        'total_score_interval': [float(v) for v in totals],
                        'mean_score_interval': [float(v/70) for v in totals], 'categories': categories,
                        **{k: model[k] for k in ('completed_count', 'timeout_count', 'completion_rate', 'pending_answer_count') if k in model}})
    exported = {'question_count': 70, 'points_per_question': 100, 'maximum_total_score': 7000,
                'generated_at': raw['generated_at'], 'official': False,
                'category_counts': dict(counts), 'ranking': ranking}
    if raw.get('score_note'):
        exported['score_note'] = raw['score_note']
    put(PUBLIC, exported)
    fields = ('agent', 'name', 'task_id', 'question_id', 'category', 'interval', 'pending_nodes',
              'answer_status', 'source_kind', 'answer_sha256')
    put(PUBLIC.with_name('score-details-70.json'), {
        'official': False, 'question_count': 70, 'generated_at': raw['generated_at'],
        'score_note': raw.get('score_note', {}),
        'rows': [{k: row[k] for k in fields if k in row} for row in raw['rows']]})


def check_scores(data, selected):
    assert data['question_count'] == selected['task_count'] == 70
    assert data['category_counts'] == selected['counts']
    assert data['ranking']
    assert len({r['model'] for r in data['ranking']}) == len(data['ranking'])
    for r in data['ranking']:
        assert r['question_count'] == 70
        if 'completed_count' in r:
            assert r['completed_count'] + r['timeout_count'] == 70
            assert r['completion_rate'] == r['completed_count'] / 70
        for category in CATEGORIES:
            c = r['categories'][category]
            assert c['question_count'] == data['category_counts'][category]
            assert 0 <= c['mean_score_interval'][0] <= c['mean_score_interval'][1] <= 100
        for i in (0, 1):
            total = sum(Decimal(str(r['categories'][c]['total_score_interval'][i])) for c in CATEGORIES)
            assert abs(total-Decimal(str(r['total_score_interval'][i]))) < Decimal('0.000000001')
            assert abs(total/70-Decimal(str(r['mean_score_interval'][i]))) < Decimal('0.000000001')


def check_details(data, selected):
    details = read(PUBLIC.with_name('score-details-70.json'))
    tasks = {r['task_id']: r for r in selected['tasks']}
    assert len(details['rows']) == 70 * len(data['ranking'])
    for model in data['ranking']:
        own = [r for r in details['rows'] if r['name'] == model['model']]
        assert len(own) == 70 and {r['task_id'] for r in own} == set(tasks)
        for r in own:
            assert r['question_id'] == tasks[r['task_id']]['question_id']
            assert r['category'] == tasks[r['task_id']]['category']
            assert 0 <= r['interval'][0] <= r['interval'][1] <= 100
            if r.get('answer_status') == 'timeout':
                assert r['interval'] == [0, 0]
        if 'completed_count' in model:
            assert sum(r['answer_status'] == 'completed' for r in own) == model['completed_count']
            assert sum(r['answer_status'] == 'timeout' for r in own) == model['timeout_count']
        for i in (0, 1):
            total = sum((Decimal(str(r['interval'][i])) for r in own), Decimal(0))
            assert abs(total - Decimal(str(model['total_score_interval'][i]))) < Decimal('0.000000001')


def readme_results(data, zh):
    path = ROOT/('README.zh-CN.md' if zh else 'README.md')
    original = path.read_text()
    heading = '## 评测结果' if zh else '## Evaluation results'
    next_heading = '## 快速开始' if zh else '## Quick start'
    section_start, section_end = original.index(heading), original.index(next_heading)
    table_start = original.index('| ', section_start)
    rows_start = original.index('\n', original.index('\n', table_start) + 1) + 1
    rows_end = original.index('\n\n', rows_start)
    assert rows_end < section_end
    model_agents = {r['name']: r['agent'] for r in read(ROOT/'experiments/records70/score-details-70.json')['rows']}
    lines = []
    for r in data['ranking']:
        completed = f"{r['completed_count']}/70 ({r['completion_rate']:.1%})" if 'completed_count' in r else '—'
        label = CONFIGURATION_LABELS.get(r['model'], r['model'])
        link = f"[{label}](experiments/records70/models/{model_agents[r['model']]}/README.md)"
        lines.append(f"| {r['rank']} | {link} | 70 | {completed} | {fmt(r['mean_score_interval'])} |")
    path.write_text(original[:rows_start]+'\n'.join(lines)+original[rows_end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--update-readmes', action='store_true')
    args = parser.parse_args()
    selection_path = ROOT/'config/selections/current.json'
    selected = read(selection_path)
    example_path = ROOT/'data/staged-dev/O03-04/task.json'
    rubric_path = example_path.with_name('rubric.json')
    if args.source:
        export_scores(args.source, selected)
    data = read(PUBLIC)
    check_scores(data, selected)
    details_path = PUBLIC.with_name('score-details-70.json')
    if details_path.exists():
        check_details(data, selected)
    FIGURES.mkdir(parents=True, exist_ok=True)
    for name in CURRENT_FIGURES:
        if not (FIGURES/name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError(f'Invalid PNG figure: {name}')
    if args.update_readmes:
        for zh in (False, True):
            readme_results(data, zh)
    manifest_path = FIGURES/'results-manifest.json'
    names = CURRENT_FIGURES
    files = {name: file_hash(FIGURES/name) for name in names}
    put(manifest_path, {'contains_model_scores': True, 'task_count': 70,
                        'stage_count': 83, 'case_group_count': selected['case_group_count'],
                        'model_count': len(data['ranking']), 'files': files,
                        'source_sha256': file_hash(PUBLIC),
                        'source_files_sha256': {str(p.relative_to(ROOT)): file_hash(p) for p in (PUBLIC, details_path, selection_path, example_path, rubric_path, Path(__file__)) if p.exists()},
                        'generator': 'scripts/render_current70_figures.py'})
    print(json.dumps({'figures': len(names), 'models': len(data['ranking']), 'categories': data['category_counts'], 'readmes_updated': args.update_readmes}))


if __name__ == '__main__':
    main()
