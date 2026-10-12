#!/usr/bin/env python3
"""Render the current README figures using the original Dev20 visual language.

Standard-library SVG output; no model calls or added runtime dependencies.
Pass --source to refresh score/category data from an adjudicated local summary.
Without --source, regenerate figures from the public score data.
"""
import argparse
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from html import escape
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'experiments/records70/score-summary-70.json'
FIGURES = ROOT / 'docs/figures/dev70-current'
CATEGORIES = ('recommendation_single', 'planning_single', 'staged')
EN = ('Recommendations', 'Planning & decisions', 'Two-stage tasks')
ZH = ('择校推荐', '规划与决策', '两阶段任务')
INK, MUTED, PAPER, RULE = '#30283F', '#746C7E', '#FAF7F1', '#E4D9CD'
PURPLE, TRACK, GOLD = '#7854A6', '#EDE5EF', '#E9B949'
CORAL, OCHRE = '#D96351', '#B57D17'


def read(path):
    return json.loads(path.read_text())


def put(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def fmt(bounds):
    return f'{bounds[0]:.2f}' if bounds[0] == bounds[1] else f'{bounds[0]:.2f}–{bounds[1]:.2f}'


class Figure:
    def __init__(self, title, description, height):
        self.height = height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="{height}" viewBox="0 0 1600 {height}" role="img" aria-labelledby="title desc">',
                      f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>']
        self.rect(0, 0, 1600, height, PAPER, 36)

    def rect(self, x, y, w, h, color, radius=0, stroke='none'):
        self.parts.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{radius:g}" fill="{color}" stroke="{stroke}"/>')

    def text(self, x, y, text, size=22, color=INK, bold=False, anchor='start'):
        self.parts.append(f'<text x="{x:g}" y="{y + size * .86:g}" font-family="Arial, Hiragino Sans GB, Noto Sans CJK SC, Microsoft YaHei, sans-serif" font-size="{size}" font-weight="{600 if bold else 400}" text-anchor="{anchor}" fill="{color}">{escape(str(text))}</text>')

    def lines(self, x, y, lines, size=22, color=INK, bold=False, step=None):
        for i, text in enumerate(lines):
            self.text(x, y+i*(step or size*1.45), text, size, color, bold)

    def line(self, x1, y1, x2, y2, color=RULE, width=1):
        self.parts.append(f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" stroke="{color}" stroke-width="{width}"/>')

    def circle(self, x, y, r, color):
        self.parts.append(f'<circle cx="{x:g}" cy="{y:g}" r="{r:g}" fill="{color}"/>')

    def arrow(self, x1, y1, x2, y2, color=PURPLE):
        self.line(x1, y1, x2, y2, color, 2)
        if x1 == x2:
            self.line(x2-6, y2-10, x2, y2, color, 2)
            self.line(x2+6, y2-10, x2, y2, color, 2)
        else:
            self.line(x2-10, y2-6, x2, y2, color, 2)
            self.line(x2-10, y2+6, x2, y2, color, 2)

    def header(self, eyebrow, title, subtitle):
        self.text(56, 34, eyebrow, 18, PURPLE, True)
        self.text(56, 79, title, 42, INK, True)
        self.text(56, 139, subtitle, 22, MUTED)

    def save(self, name):
        (FIGURES/name).write_text('\n'.join(self.parts+['</svg>'])+'\n')


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


def overall(data, zh):
    title = '70题均分排名' if zh else '70-task score ranking'
    extra = (len(data['ranking']) - 7) * 76
    f = Figure(title, 'Mean score intervals on a 0–100 scale. Source: experiments/records70/score-summary-70.json.', 876 + extra)
    f.header('70 TASKS / OVERALL RESULTS', title,
             f"{len(data['ranking'])}个模型 · 70道题 · 满分100分" if zh else f"{len(data['ranking'])} models · 70 tasks · 100-point scale")
    # Reserve enough label space for the widest model name and interval.
    x0, width, y0, step = 450, 820, 236, 76
    for tick in (0, 25, 50, 75, 100):
        x = x0 + width*tick/100
        f.line(x, y0-20, x, 755 + extra)
        f.text(x, 773 + extra, tick, 18, MUTED, anchor='middle')
    for i, row in enumerate(data['ranking']):
        y = y0+i*step
        lower, upper = row['mean_score_interval']
        f.text(56, y+4, row['rank'], 24, MUTED)
        f.text(106, y+4, row['model'], 25, INK, True)
        f.rect(x0, y, width, 32, TRACK, 5)
        f.rect(x0, y, width*lower/100, 32, PURPLE, 5)
        if upper > lower:
            f.rect(x0+width*lower/100, y, width*(upper-lower)/100, 32, GOLD)
        f.text(1540, y+3, fmt(row['mean_score_interval']), 27, INK, True, 'end')
    f.rect(56, 825 + extra, 16, 16, PURPLE, 3)
    f.text(82, 823 + extra, '分数' if zh else 'Score', 19, MUTED)
    f.rect(230, 825 + extra, 16, 16, GOLD, 3)
    f.text(256, 823 + extra, '区间' if zh else 'Range', 19, MUTED)
    f.save(f'overall-scores.{"zh-CN" if zh else "en"}.svg')


def category_scores(data, zh):
    title = '不同题型，表现如何？' if zh else 'How do scores vary by task type?'
    extra = (len(data['ranking']) - 7) * 67
    f = Figure(title, 'Per-category score intervals; the same model order and 0–100 scales across three panels.', 884 + extra)
    f.header('70 TASKS / TASK-TYPE RESULTS', title,
             '每类题目等权计分 · 统一100分制' if zh else 'Equal task weights within each category · Common 100-point scale')
    f.rect(56, 191, 16, 16, PURPLE, 3)
    f.text(82, 188, '分数' if zh else 'Score', 20)
    f.rect(230, 191, 16, 16, GOLD, 3)
    f.text(256, 188, '区间' if zh else 'Range', 20)
    labels = ZH if zh else EN
    first_y, step, width = 326, 67, 212
    for column, (category, label) in enumerate(zip(CATEGORIES, labels)):
        x = 360+column*398
        count = data['category_counts'][category]
        f.text(x, 243, label, 24, INK, True)
        f.text(x+342, 246, f'{count}题' if zh else f'{count} tasks', 18, MUTED, anchor='end')
        for tick in (0, 50, 100):
            tx = x+width*tick/100
            f.text(tx, 289, tick, 16, MUTED, anchor='middle')
            f.line(tx, first_y-8, tx, first_y+(len(data['ranking'])-1)*step+34)
        for i, row in enumerate(data['ranking']):
            y = first_y+i*step
            bounds = row['categories'][category]['mean_score_interval']
            lower, upper = bounds
            f.rect(x, y, width, 27, TRACK, 4)
            f.rect(x, y, width*lower/100, 27, PURPLE, 4)
            if upper > lower:
                f.rect(x+width*lower/100, y, width*(upper-lower)/100, 27, GOLD)
            f.text(x+226, y+5, fmt(bounds), 19)
    for i, row in enumerate(data['ranking']):
        f.text(56, first_y+i*step+3, row['model'], 24, INK, True)
    f.text(56, 823 + extra, '按70题总均分排序 · 两阶段合为一道题评分' if zh else
           'Ordered by the 70-task mean · Each two-stage task receives one score', 20, MUTED)
    f.save(f'category-scores.{"zh-CN" if zh else "en"}.svg')


def overview(selected, example, zh):
    assert example['document_question_id'] == 'Q071'
    assert '前约15%' in example['stages'][0]['instruction']
    assert '工科大类' in example['stages'][1]['instruction']
    title = '信息变了，建议如何更新？' if zh else 'When facts change, does the advice keep up?'
    f = Figure(title, 'Q071: initial facts and new evidence enter one session in order; two answers are reviewed together.', 956)
    f.header('BAOYAN AGENT BENCHMARK / HOW IT WORKS', title,
             '以Q071为例：先完成第一轮作答，再提供补充信息。' if zh else
             'Example Q071: new evidence enters only after the first answer.')
    f.rect(56, 236, 422, 211, '#FBE0D7', 22)
    f.rect(56, 481, 422, 211, '#F8E6AB', 22)
    f.rect(546, 236, 462, 456, '#EEE5F5', 24)
    f.rect(1076, 236, 468, 456, 'white', 24, RULE)
    f.text(82, 260, '01 / 初始背景 S0' if zh else '01 / INITIAL FACTS · S0', 23, INK, True)
    f.lines(82, 310, ['工科大类，尚未正式分流', '想申请机械或智能制造'] if zh else
            ['No declared major yet.', 'Mechanical engineering or', 'smart manufacturing?'], 28 if zh else 26, INK, True, 34 if zh else 32)
    f.text(82, 405 if zh else 417, '排名前约15% · 六级450–499分' if zh else 'Top ~15% · CET-6 450–499', 22, MUTED)
    f.text(82, 507, '02 / 补充信息 S1' if zh else '02 / NEW EVIDENCE · S1', 23, INK, True)
    f.lines(82, 553, ['补充机械创新、大创', '与智能系统项目经历'] if zh else
            ['Mechanical innovation,', 'student research,', 'intelligent systems.'], 27, INK, True, 33)
    f.text(82, 653, '学籍专业仍是工科大类' if zh else 'Still no formally declared major.', 21, MUTED)
    f.arrow(490, 394, 534, 394, CORAL)
    f.arrow(490, 614, 534, 614, OCHRE)
    f.text(572, 260, '同一个原生 Agent 会话' if zh else 'ONE NATIVE AGENT SESSION', 26, INK, True)
    f.text(572, 303, '模型 + 工具 + 认证配置' if zh else 'Model + tools + authentication', 23, MUTED)
    f.rect(572, 353, 410, 101, 'white', 15)
    f.text(598, 375, 'A0  首次答卷' if zh else 'A0  First answer', 29, INK, True)
    f.text(598, 419, '只使用第一阶段已知信息' if zh else 'Use only the initial facts', 22, MUTED)
    f.arrow(777, 466, 777, 528)
    f.text(798, 489, '再提供 S1' if zh else 'Then release S1', 20, PURPLE)
    f.rect(572, 541, 410, 123, 'white', 15)
    f.text(598, 565, 'A1  更新建议' if zh else 'A1  Revised advice', 29, INK, True)
    f.text(598, 610, '重评适配，保留仍然成立的约束' if zh else 'Reassess fit; retain constraints', 21, MUTED)
    f.arrow(1020, 465, 1064, 465)
    f.text(1104, 260, '独立评审' if zh else 'INDEPENDENT REVIEW', 29, INK, True)
    f.text(1104, 303, '两轮答卷 + 本题评分标准' if zh else 'Both answers + task rubric', 23, MUTED)
    review = [('理解限制条件', '排名优势不等于资格已确认'), ('使用新增证据', '核对项目贡献与成果状态'), ('给出下一步行动', '解释取舍与具体核验动作')] if zh else [
        ('Read the constraints', 'Rank is not eligibility.'), ('Use new evidence', 'Check project contributions.'), ('Plan the next step', 'Explain tradeoffs and checks.')]
    for i, (label, detail) in enumerate(review):
        y = 369+i*108
        f.circle(1121, y+13, 15, PURPLE)
        f.line(1114, y+14, 1120, y+20, 'white', 3)
        f.line(1120, y+20, 1130, y+7, 'white', 3)
        f.text(1154, y-3, label, 26, INK, True)
        f.text(1154, y+40, detail, 21, MUTED)
        if i < 2:
            f.line(1104, y+82, 1516, y+82)
    labels = ('题目', '学生案例', '作答阶段') if zh else ('tasks', 'student cases', 'answer stages')
    details = ('21择校 / 36规划 / 13两阶段', '同一案例可以包含不同问题', '两阶段题合并为一个分数') if zh else (
        '21 recommendation / 36 planning / 13 staged', 'A case can contain multiple tasks', 'Two stages, one task score')
    for x, value, label, detail in zip((56, 584, 1104), (70, selected['case_group_count'], 83), labels, details):
        f.text(x, 737, value, 64, INK, True)
        f.text(x+100, 778, label, 25, INK, True)
        f.text(x, 818, detail, 19, MUTED)
    f.rect(56, 885, 1488, 42, INK, 13)
    f.text(800, 896, 'S0 → A0 → S1 → A1   ·   评分材料仅供评审使用' if zh else
           'S0 → A0 → S1 → A1   ·   Rubrics stay with the reviewer', 21, 'white', True, 'middle')
    f.save(f'benchmark-overview.{"zh-CN" if zh else "en"}.svg')


def distribution(selected, zh):
    title = '70题，覆盖哪些任务？' if zh else 'What does the 70-task set cover?'
    f = Figure(title, 'One dot represents one task. Category counts: 21 recommendations, 36 planning and decisions, 13 two-stage tasks.', 674)
    f.header('70 TASKS / DATASET', title,
             '23组学生案例 · 83个作答阶段' if zh else '23 student cases · 83 answer stages')
    labels = ZH if zh else EN
    for i, (category, label, color) in enumerate(zip(CATEGORIES, labels, (CORAL, PURPLE, OCHRE))):
        x, count = 56+i*506, selected['counts'][category]
        f.text(x, 218, label, 28, INK, True)
        f.text(x, 266, count, 64, color, True)
        f.text(x+98, 305, f'{count/70:.1%}', 23, MUTED)
        for j in range(count):
            f.circle(x+14+(j%9)*42, 386+(j//9)*40, 11, color)
        f.text(x, 561, f'{count}道题 / {count*(2 if category=="staged" else 1)}个阶段' if zh else
               f'{count} tasks / {count*(2 if category=="staged" else 1)} answer stages', 22, MUTED)
        if i < 2:
            f.line(x+456, 214, x+456, 589)
    f.line(56, 612, 1544, 612)
    f.text(56, 633, '每个圆点代表一道题 · 两阶段任务合并为一道题计分' if zh else
           'One dot = one task · Each two-stage task receives one score', 20, MUTED)
    f.save(f'dataset-distribution.{"zh-CN" if zh else "en"}.svg')


def difficulty_distribution(selected, zh):
    counts = selected['difficulty_counts']
    if counts != {'easy': 14, 'medium': 34, 'hard': 22}:
        raise ValueError('Current owner-approved difficulty distribution differs')
    title = '70题难度分布' if zh else 'Difficulty across 70 tasks'
    f = Figure(title, 'Owner-approved difficulty labels: 14 easy, 34 medium, 22 hard. No independent double review is claimed.', 608)
    f.header('70 TASKS / DIFFICULTY', title,
             '负责人确认 · 不代表独立双标复核' if zh else 'Owner approved · no independent double review claimed')
    for i, (key, label, color) in enumerate(zip(('easy', 'medium', 'hard'),
            ('简单', '中等', '困难') if zh else ('Easy', 'Medium', 'Hard'),
            (CORAL, PURPLE, OCHRE))):
        x, count = 56+i*506, counts[key]
        f.text(x, 218, label, 30, INK, True)
        f.text(x, 268, count, 66, color, True)
        f.text(x+98, 307, {'easy': '20%', 'medium': '48.57%', 'hard': '31.43%'}[key], 23, MUTED)
        for j in range(count):
            f.circle(x+14+(j%9)*42, 389+(j//9)*40, 11, color)
        if i < 2:
            f.line(x+456, 214, x+456, 529)
    f.line(56, 548, 1544, 548)
    f.text(56, 565, '每个圆点代表一道题；标签不改变题面或分数' if zh else
           'One dot = one task · Labels do not change question text or scores', 20, MUTED)
    f.save(f'difficulty-distribution.{"zh-CN" if zh else "en"}.svg')


def readme_results(data, zh):
    path = ROOT/('README.zh-CN.md' if zh else 'README.md')
    original = path.read_text()
    heading = '## 评测结果' if zh else '## Evaluation results'
    next_heading = '## 快速开始' if zh else '## Quick start'
    lang = 'zh-CN' if zh else 'en'
    intro = ('本榜单基于70道公开题的评测记录汇总，展示各模型的平均得分与完成率。评测设置和评分说明见[文档](docs/result-methodology.md)。' if zh else
             'This leaderboard summarizes evaluation records for 70 public tasks, showing each model’s mean score and completion rate. See the [documentation](docs/result-methodology.md) for evaluation settings and scoring.')
    lines = [heading, '', intro, '', '| 排名 | Agent配置 | 题数 | 完成率 | 均分 / 100 |' if zh else
             '| Rank | Agent configuration | Tasks | Completion | Mean score / 100 |', '| ---: | --- | ---: | ---: | ---: |']
    model_agents = {r['name']: r['agent'] for r in read(ROOT/'experiments/records70/score-details-70.json')['rows']}
    for r in data['ranking']:
        completed = f"{r['completed_count']}/70 ({r['completion_rate']:.1%})" if 'completed_count' in r else '—'
        label = f"[{r['model']}](experiments/records70/models/{model_agents[r['model']]}/README.md)"
        lines.append(f"| {r['rank']} | {label} | 70 | {completed} | {fmt(r['mean_score_interval'])} |")
    assert all('timeout_count' in r for r in data['ranking'])
    current_timeouts = sum(r['timeout_count'] for r in data['ranking'])
    lines += ['', (f'{len(data["ranking"])}个模型各完成70题；当前共{current_timeouts}题超时。' if zh else
                  f'All {len(data["ranking"])} agent configurations completed 70 tasks; {current_timeouts} current tasks timed out.')]
    lines += ['', ('当前70题及评分标准均已由维护者确认完成教师校准。难度标签经负责人确认：简单14题（20%）、中等34题（48.57%）、困难22题（31.43%）；不代表独立双标复核。逐题评分记录仍保留生成时的 `scored_draft` 与 `teacher_calibration` 标签；`official=false` 是独立的结果认证标记。' if zh else
                   'All 70 current tasks and rubrics have owner-confirmed teacher calibration. The owner-approved difficulty labels are 14 easy (20%), 34 medium (48.57%) and 22 hard (31.43%); this is not an independent double review. Per-answer `scored_draft` and `teacher_calibration` labels remain in the captured scoring records. `official=false` is a separate result-certification flag.')]
    lines += ['', f'![{"各模型的70题均分排名" if zh else "70-task mean scores for all models"}](docs/figures/dev70-current/overall-scores.{lang}.svg)', '',
              f'![{"70题难度分布" if zh else "70-task difficulty distribution"}](docs/figures/dev70-current/difficulty-distribution.{lang}.svg)', '',
              '[评分数据](experiments/records70/score-summary-70.json) · [逐题得分](experiments/records70/score-details-70.json) · [按模型查阅答卷与评分](experiments/records70/README.md)' if zh else
              '[Score data](experiments/records70/score-summary-70.json) · [Per-task results](experiments/records70/score-details-70.json) · [Answers and scores by model](experiments/records70/README.md)', '', '']
    start, end = original.index(heading), original.index(next_heading)
    path.write_text(original[:start]+'\n'.join(lines)+original[end:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--update-readmes', action='store_true')
    args = parser.parse_args()
    selection_path = ROOT/'config/selections/current.json'
    selected = read(selection_path)
    example_path = ROOT/'data/staged-dev/N16-02/task.json'
    example = read(example_path)
    if args.source:
        export_scores(args.source, selected)
    data = read(PUBLIC)
    check_scores(data, selected)
    details_path = PUBLIC.with_name('score-details-70.json')
    if details_path.exists():
        check_details(data, selected)
    FIGURES.mkdir(parents=True, exist_ok=True)
    for zh in (False, True):
        overall(data, zh)
        category_scores(data, zh)
        overview(selected, example, zh)
        distribution(selected, zh)
        difficulty_distribution(selected, zh)
        if args.update_readmes:
            readme_results(data, zh)
    manifest_path = FIGURES/'results-manifest.json'
    names = [f'{stem}.{lang}.svg' for stem in ('overall-scores', 'category-scores', 'benchmark-overview', 'dataset-distribution', 'difficulty-distribution') for lang in ('en', 'zh-CN')]
    files = {name: file_hash(FIGURES/name) for name in names}
    put(manifest_path, {'contains_model_scores': True, 'task_count': 70,
                        'stage_count': 83, 'case_group_count': selected['case_group_count'],
                        'model_count': len(data['ranking']), 'files': files,
                        'source_sha256': file_hash(PUBLIC),
                        'source_files_sha256': {str(p.relative_to(ROOT)): file_hash(p) for p in (PUBLIC, details_path, selection_path, example_path, Path(__file__)) if p.exists()},
                        'generator': 'scripts/render_current70_figures.py'})
    print(json.dumps({'figures': len(names), 'models': len(data['ranking']), 'categories': data['category_counts'], 'readmes_updated': args.update_readmes}))


if __name__ == '__main__':
    main()
