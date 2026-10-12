#!/usr/bin/env python3
"""Build a private recovery snapshot; public figures use render_current70_figures.py."""
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_publication import checksums, clean, export_public_answers, put, read, sha
from benchmark.core import digest

ARCHIVE = ROOT / 'experiments/execution-records/dev75/materials'
OUT = ARCHIVE / 'current70-recovery'
FIGURES = OUT / 'figures'
SCHEMA = 'current70-presentation.1'
RECOVERED_SNAPSHOT = ARCHIVE / 'recovered-originals-20261011/59bf3c078704eafc0900e1fd754ef3deb94ac8d9.json'
SESSION_RECOVERY = ARCHIVE / 'recovered-originals-20261011/session-score-recovery.json'


def source(path):
    return {'file': path.name, 'sha256': sha(path.read_bytes())}


def historical(selection):
    progress = read(ARCHIVE / 'round2-progress.json')
    adjudications = read(ARCHIVE / 'verified-pending.json')
    latest = read(ARCHIVE / 'status-snapshot-20261010-213913.json')
    recovered = read(ARCHIVE / 'recovered-review-evidence-20261011.json')
    original_snapshot = read(RECOVERED_SNAPSHOT)
    original_rows = {(r['agent'], r['task_id']): r for r in original_snapshot['rows']}
    session_recovery = read(SESSION_RECOVERY)
    excerpts_path = SESSION_RECOVERY.with_name('session-source-excerpts.json')
    if sha(excerpts_path.read_bytes()) != session_recovery['source_excerpts_sha256']:
        raise ValueError('Recovered session source changed')
    excerpts = {r['item_id']: r for r in read(excerpts_path)}
    session_rows = {(r['agent'], r['task_id']): r for r in session_recovery['rows']}
    for restored in session_rows.values():
        excerpt = excerpts[restored['source_item_id']]
        if sha(excerpt['output'].encode()) != restored['source_output_sha256']:
            raise ValueError('Recovered session output hash differs')
    recovered_by_task = {(r['agent'], r['task_id']): r for r in recovered['adjudications']}
    by_task = {x['task_id']: x for x in selection['tasks']}
    overlays = {(x['agent'], x['task_id']): x for x in adjudications['rows']}
    rows = []
    for original in progress['rows']:
        if original['task_id'] not in by_task:
            continue
        row = dict(original)
        restored_row = original_rows.get((row['agent'], row['task_id']))
        if restored_row and restored_row.get('review_status') == 'completed':
            if (row.get('answer_sha256') and restored_row.get('answer_sha256') != row['answer_sha256']):
                raise ValueError('Recovered snapshot and archived answer hash differ')
            row.update({k: restored_row[k] for k in ('answer_sha256', 'score', 'score_interval',
                        'pending_nodes', 'review_mode') if k in restored_row})
            row['review_status'] = 'completed'
            row['recovered_original_row'] = restored_row
            row['aggregation_hold'] = bool(restored_row.get('comparability_hold')) or row['aggregation_hold']
            row['recovered_snapshot_at'] = original_snapshot['updated_at']
        session_row = session_rows.get((row['agent'], row['task_id']))
        if session_row:
            if row['review_status'] == 'completed':
                raise ValueError('Session recovery would overwrite an existing reviewed row')
            if row.get('answer_sha256') not in (None, session_row['answer_sha256']):
                raise ValueError('Recovered session and archived answer hash differ')
            row.update({k: session_row[k] for k in ('answer_sha256', 'score', 'score_interval',
                                                  'pending_nodes', 'review_status')})
            row.update(execution_status='completed', recovered_session_row=session_row,
                       recovered_snapshot_at=session_row['source_observed_at'])
        task = by_task[row['task_id']]
        row.update(question_id=task['question_id'], category=task['category'],
                   reported_score=row['score'], evidence_status='archived_summary_not_recomputed',
                   current_version_eligible=False)
        if session_row:
            row['evidence_status'] = session_row['evidence_status']
        overlay = overlays.get((row['agent'], row['task_id']))
        # A later failure to locate the answer is not a reversal of a recovered adjudication.
        if overlay and not (overlay['status'] == 'pending_missing_original_answer' and
                            row.get('score') is not None and not row['pending_nodes']):
            if overlay['answer_sha256'] != row['answer_sha256']:
                raise ValueError('Adjudication and answer hash differ')
            row.update(score=overlay['effective_historical_score'],
                       score_interval=overlay['effective_historical_score_interval'],
                       pending_nodes=overlay['remaining_pending_nodes'],
                       adjudication_status=overlay['status'], reason=overlay.get('reason'),
                       node_adjudications=overlay['effective_node_judgments'])
        if row['aggregation_hold']:
            row.update(score=None, score_interval=None, score_status='gold_issue_hold')
        elif row['pending_nodes']:
            row['score_status'] = 'pending'
        elif row['score'] is not None:
            row['score_status'] = 'archived_numeric_summary'
        else:
            row['score_status'] = 'numeric_detail_unavailable'
        restored = recovered_by_task.get((row['agent'], row['task_id']))
        if restored:
            if restored['answer_sha256'] != row['answer_sha256']:
                raise ValueError('Recovered decision and archived answer hash differ')
            row.update(score=restored['historical_total'], score_interval=restored['historical_interval'],
                       pending_nodes=restored['pending_nodes'], score_status='archived_numeric_summary',
                       adjudication_status='historical_decision_recovered',
                       evidence_status=restored['evidence_status'],
                       recovered_adjudication=restored)
        rows.append(row)
    summaries = []
    for config in latest['summary']:
        own = [r for r in rows if r['agent'] == config['agent']]
        summaries.append({
            'agent': config['agent'], 'name': config['name'],
            'historical_completed_answers': sum(r['agent'] == config['agent'] and r['task_id'] in by_task and r['answer_status'] == 'completed' for r in latest['rows']),
            'historical_review_status_count': sum(r['agent'] == config['agent'] and r['task_id'] in by_task and r['review_status'] == 'completed' for r in latest['rows']),
            'recovered_review_records': sum('recovered_original_row' in r or
                                            'recovered_session_row' in r for r in own),
            'recovered_raw_numeric_records': sum(r['reported_score'] is not None for r in own),
            'archived_numeric_summaries': sum(r['score_status'] == 'archived_numeric_summary' for r in own),
            'early_snapshot_pending_records': sum(r['score_status'] == 'pending' for r in own),
            'latest_pending_records': None,
            'known_held_records': sum(r['aggregation_hold'] for r in own),
            'overall_mean': None, 'category_means': None,
        })
    return {
        'selection_id': selection['id'], 'scope_task_ids': selection['task_ids'], 'current_version_eligible': False,
        'execution_snapshot_at': latest['updated_at'], 'numeric_snapshot_at': original_snapshot['updated_at'],
        'review_completion': 'historical_reviews_owner_confirmed', 'completed_answers': sum(r['historical_completed_answers'] for r in summaries),
        'summary': summaries, 'rows': rows,
        'unbound_numeric_summaries': session_recovery['unbound_numeric_summaries'],
        'sources': [source(ARCHIVE / n) for n in ('round2-progress.json', 'verified-pending.json',
                                                'status-snapshot-20261010-213913.json',
                                                'recovered-review-evidence-20261011.json')] +
                   [source(RECOVERED_SNAPSHOT), source(SESSION_RECOVERY), source(excerpts_path)],
        'limitations': [
            'Execution/review counts and numeric summaries have different snapshot times.',
            'Owner confirms reviews were completed; missing local score details do not mean unreviewed answers.',
            'Summary rows do not establish complete answer-level review provenance.',
            'Historical inputs are not relabelled as current 1.0.2 results.',
            'Means remain null; missing, timed-out, pending and held records are not filled with zero.',
        ],
    }


def current_records(selection):
    by_task = {r['task_id']: r for r in selection['tasks']}
    rows = []
    for batch_name, agent in (
        ('gemini-rerun2-20261011', 'antigravity-gemini-3-8-flash-high'),
        ('qwen-missing7-20261011', 'qwen3-8-max-0902'),
    ):
        for run_path in sorted((ARCHIVE / batch_name / 'single' / agent).glob('*/r1/run.json')):
            run = read(run_path)
            if run['task_id'] not in by_task:
                continue
            task = by_task[run['task_id']]
            frozen_task = read(run_path.parent / 'task.json')
            frozen_rubric = read(run_path.parent / 'rubric.json')
            if (digest(frozen_task) != task['fingerprints']['task'] or
                    digest(frozen_rubric) != task['fingerprints']['rubric']):
                raise ValueError('Rerun does not match current inputs: ' + run['task_id'])
            row = {k: run.get(k) for k in ('task_id', 'status', 'started_at', 'finished_at',
                                          'elapsed_seconds', 'cli', 'cli_version', 'model', 'auth_mode')}
            row.update(agent=agent, question_id=task['question_id'], category=task['category'],
                       task_sha256=task['fingerprints']['task'], rubric_sha256=task['fingerprints']['rubric'],
                       source_run=source(run_path), score=None, score_interval=None, pending_nodes=[],
                       scoring_status='not_scored', automatic_retry=False, answers=[])
            if row['status'] == 'timeout':
                row.update(scoring_status='timeout_no_retry', retry_policy='do_not_rerun')
            if row['status'] == 'completed':
                answer_path = run_path.parent / 'workspace/answer.md'
                raw_hash = sha(answer_path.read_bytes())
                if raw_hash != run.get('answer_file_sha256'):
                    raise ValueError('Completed answer hash changed')
                relative = Path(agent) / task['task_id']
                exported = export_public_answers(answer_path.parent, {'answer.md': raw_hash}, OUT / relative)
                row.update(answer_sha256=raw_hash,
                           answers=[(relative / x['path']).as_posix() for x in exported],
                           source_answer_export_sha256=exported[0]['export_sha256'])
                scored = ARCHIVE / batch_name / 'scored/runs' / agent / task['task_id'] / 'r1'
                if (scored / 'score.json').exists():
                    result, judgment = read(scored / 'score.json'), read(scored / 'judgment.json')
                    for key, expected in (('answer_sha256', raw_hash),
                                          ('task_sha256', task['fingerprints']['task']),
                                          ('rubric_sha256', task['fingerprints']['rubric'])):
                        if result[key] != expected or judgment[key] != expected:
                            raise ValueError('Score provenance differs: ' + key)
                    for name, value in (('score.json', result), ('judgment.json', judgment)):
                        put(OUT / relative / name, clean(value))
                    evidence_path = scored / 'sources.json'
                    if not evidence_path.exists():
                        evidence_path = ARCHIVE / batch_name / 'sources.json'
                    if evidence_path.exists():
                        put(OUT / relative / 'sources.json', clean(read(evidence_path)))
                        row['sources_path'] = (relative / 'sources.json').as_posix()
                    row.update(score=result['score'], score_interval=result['score_interval'],
                               confirmed_points=result['confirmed_points'], pending_nodes=result['pending_nodes'],
                               scoring_status='pending' if result['pending_nodes'] else 'scored_draft',
                               score_path=(relative / 'score.json').as_posix(),
                               judgment_path=(relative / 'judgment.json').as_posix(),
                               source_score=source(scored / 'score.json'))
            rows.append(row)
    return rows


def execution_table(rows, zh):
    heading = ('| Agent配置 | 历史答卷 | 已完成评审 | 留存数值摘要 | 70题均分 |' if zh else
               '| Agent configuration | Historical answers | Completed reviews | Retained numeric summaries | 70-task mean |')
    lines = [heading, '| --- | ---: | ---: | ---: | --- |']
    for r in rows:
        lines.append(f"| {r['name']} | {r['historical_completed_answers']}/70 | "
                     f"{r['historical_review_status_count']}/70 | "
                     f"{r['archived_numeric_summaries']}/70 | {'待恢复汇总' if zh else 'Pending record recovery'} |")
    return '\n'.join(lines)


def result_text(row):
    if row['score'] is not None:
        return f"{row['score']:g} / 100"
    if row['score_interval']:
        return '–'.join(f'{v:g}' for v in row['score_interval'])
    return '—'


def result_table(rows, zh):
    lines = [('| 配置 | 题号 | 执行 | 分数 / 区间 | 评分状态 |' if zh else
              '| Configuration | Task | Execution | Score / bounds | Review status |'),
             '| --- | --- | --- | ---: | --- |']
    status = {'completed': '完成', 'timeout': '超时，不重跑', 'running': '执行中', 'prepared': '未开始'}
    for r in rows:
        name = 'Gemini 3.8 Flash' if r['agent'].startswith('antigravity') else 'Qwen 3.8 Max'
        execution = status.get(r['status'], r['status']) if zh else r['status']
        pending = len(r['pending_nodes'])
        review = ((f'{pending}项待核' if zh else f'{pending} pending nodes') if pending else
                  ('初评完成' if zh else 'Scored draft') if r['score'] is not None else
                  ('超时，评分留空' if zh else 'Timeout; score unavailable') if r['status'] == 'timeout' else
                  ('尚无评分' if zh else 'Not scored'))
        task_label = r['question_id']
        if r['answers']:
            task_label = f"[{task_label}]({r['answers'][0]})"
        points = result_text(r)
        if r.get('score_path'):
            points = f"[{points}]({r['score_path']})"
            review = f"[{review}]({r['judgment_path']})"
        lines.append(f'| {name} | {task_label} | {execution} | {points} | {review} |')
    return '\n'.join(lines)


def figure(title, subtitle, body, height, desc):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1600" '
            f'height="{height}" viewBox="0 0 1600 {height}" role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>'
            '<style>text{font-family:Arial,"Hiragino Sans GB","Microsoft YaHei",sans-serif;fill:#30283F}'
            '.muted{fill:#746C7E}.title{font-size:42px;font-weight:600}.label{font-size:23px}'
            '.small{font-size:19px}</style>'
            f'<rect width="1600" height="{height}" rx="30" fill="#FAF7F1"/>'
            f'<text x="56" y="72" class="title">{escape(title)}</text>'
            f'<text x="56" y="116" class="label muted">{escape(subtitle)}</text>{body}</svg>\n')


def render(data, zh):
    completed = data['historical']['completed_answers']
    language = 'zh-CN' if zh else 'en'
    counts = data['categories']
    names = ['择校推荐', '规划与决策', '两阶段更新'] if zh else ['Recommendations', 'Planning & decisions', 'Two-stage updates']
    body = ''
    for i, (key, label) in enumerate(zip(('recommendation_single', 'planning_single', 'staged'), names)):
        x = 56 + i * 512
        color = ['#D96351', '#7854A6', '#B57D17'][i]
        body += (f'<rect x="{x}" y="162" width="464" height="210" rx="18" fill="white"/>'
                 f'<text x="{x+28}" y="217" class="label">{escape(label)}</text>'
                 f'<text x="{x+28}" y="300" style="font-size:66px;fill:{color};font-weight:600">{counts[key]}</text>'
                 f'<text x="{x+28}" y="345" class="small muted">{counts[key] / 70:.1%}</text>')
    note = ('70道题 · 83个作答阶段 · 两阶段合并为一道题评分' if zh else
            '70 tasks · 83 answer stages · Each two-stage task receives one score')
    body += f'<text x="56" y="427" class="label muted">{escape(note)}</text>'
    (FIGURES / f'dataset-distribution.{language}.svg').write_text(figure(
        '70题，覆盖哪些任务？' if zh else 'What does the 70-task set cover?',
        '按当前题库分类' if zh else 'Categories from the current question set', body, 472, note))
    body = ''
    for x, color, label in ((56, '#7854A6', '历史完成答卷' if zh else 'Historical completed answers'),
                            (550, '#D96351', '留存数值摘要' if zh else 'Retained numeric summaries')):
        body += f'<rect x="{x}" y="152" width="18" height="18" rx="3" fill="{color}"/>'
        body += f'<text x="{x+30}" y="169" class="small">{escape(label)}</text>'
    for i, r in enumerate(data['historical']['summary']):
        y = 228 + i * 67
        body += f'<text x="56" y="{y+19}" class="label">{escape(r["name"])}</text>'
        for x, field, color in ((400, 'historical_completed_answers', '#7854A6'),
                                (990, 'archived_numeric_summaries', '#D96351')):
            n = r[field]
            body += (f'<rect x="{x}" y="{y}" width="420" height="22" rx="4" fill="#EDE5EF"/>'
                     f'<rect x="{x}" y="{y}" width="{n / 70 * 420:.2f}" height="22" rx="4" fill="{color}"/>'
                     f'<text x="{x+434}" y="{y+19}" class="small">{n}/70</text>')
    note = (f'{completed}份历史答卷已评审；此图显示已恢复的数值资料，补跑结果另列。' if zh else
            f'All {completed} historical answers were reviewed. This chart shows recovered numeric records; reruns are listed separately.')
    body += f'<text x="56" y="810" class="small muted">{escape(note)}</text>'
    (FIGURES / f'result-coverage.{language}.svg').write_text(figure(
        '70题结果：证据覆盖情况' if zh else '70-task results: evidence coverage',
        '每个配置70题 · 答卷状态与数值评分分别统计' if zh else
        '70 tasks per configuration · Answer completion and numeric records counted separately',
        body, 856, note))


def write_report(data, zh):
    completed = data['historical']['completed_answers']
    heading = '# 70题结果与评分边界' if zh else '# 70-task results and scoring scope'
    intro = ('当前题库为1.0.2版，共70题、83个作答阶段。下列历史批次与当前版本补跑分开记录。'
             '缺失、超时、待核和评分条件错配均不自动补成零分；没有完整、同版本、可比较的70题数值评分时，均分与排名留空。' if zh else
             'The current set is version 1.0.2: 70 tasks and 83 answer stages. Historical campaign records and current-version reruns remain separate. '
             'Missing, timed-out, pending and rubric-held results are not filled with zero. Overall means and rankings require 70 comparable, same-version scores.')
    text = [heading, intro, '## 历史70题批次' if zh else '## Historical 70-task campaign',
            execution_table(data['historical']['summary'], zh),
            ('执行/已评审状态快照：' if zh else 'Execution/review-status snapshot: ') + data['historical']['execution_snapshot_at'] +
            ('；数值摘要快照：' if zh else '; numeric-summary snapshot: ') + data['historical']['numeric_snapshot_at'] + '.',
            ('已从Git对象恢复18:56的原评分快照及原裁定文件，恢复清单见内部执行材料。'
             '“留存数值摘要”不计入恢复快照中的暂停汇总项，也不表示逐项评审包已全部恢复。'
             f'已有{completed}份答卷评审已完成；当前缺口是后续原评分明细的恢复与汇总，不是重新做评审。' if zh else
             'The original 18:56 scoring snapshot and adjudication files have been recovered from Git objects; the recovery inventory remains internal. '
             'Retained numeric summaries exclude snapshot aggregation holds and do not establish complete item-level provenance. '
             f'All {completed} historical answers were reviewed; the remaining gap is recovery and aggregation of later records, not fresh review.'),
            '## 当前1.0.2版补跑' if zh else '## Current version 1.0.2 reruns',
            result_table(data['current_records'], zh),
            ('本页为生成时快照。超时不再重跑；执行中及未开始项目不能记为已完成。'
             '新答卷不覆盖旧答卷，不混入历史均分。当前评审为非盲事后初评，official=false。' if zh else
             'This is a generation-time snapshot. Timed-out tasks are not retried. Running/prepared tasks are not complete. '
             'New answers do not replace old answers or enter historical means. Current reviews are unblinded post-hoc drafts; official=false.'),
            '## 汇总阻断项' if zh else '## Aggregation blockers']
    for item in data['blockers']:
        text.append('- ' + item['zh' if zh else 'en'])
    text.extend(['[机器可读结果 / Machine-readable results](results.json) · [SHA256](checksums.json)',
                 ('完整执行与评审轨迹内部存档。评分依据保留于各答卷的judgment.json；得分及节点见score.json。' if zh else
                  'Full execution/reviewer traces remain internal. Each scored answer has a judgment.json rationale and a score.json node breakdown.')])
    (OUT / ('README.md' if zh else 'README.en.md')).write_text('\n\n'.join(text) + '\n')


def main():
    if (OUT / 'results.json').exists() and read(OUT / 'results.json').get('schema') != SCHEMA:
        raise ValueError('Refuse to replace unrelated results')
    if (OUT / 'checksums.json').exists():
        for name, expected in read(OUT / 'checksums.json').items():
            if not (OUT / name).exists() or sha((OUT / name).read_bytes()) != expected:
                raise ValueError('Generated result was edited; preserve it before regeneration: ' + name)
    selection = read(ROOT / 'config/selections/current.json')
    if sha((ROOT / selection['source_document']).read_bytes()) != selection['source_document_sha256']:
        raise ValueError('Canonical question document changed')
    data = {
        'schema': SCHEMA, 'official': False, 'publication_status': 'local_draft',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'task_count': selection['task_count'], 'stage_count': selection['stage_count'], 'task_version': selection['source_version'],
        'source_document_sha256': selection['source_document_sha256'],
        'categories': selection['counts'], 'historical': historical(selection),
        'current_records': current_records(selection), 'overall_comparison_ready': False,
        'blockers': [
            {'id': 'missing_numeric_evidence',
             'zh': '本70题范围内的历史答卷已完成评审；目前尚未找回完整原始数值明细与逐项评审包，需要恢复资料后汇总，不重复评审。',
             'en': 'Historical answers retained in the 70-task scope were reviewed. Their complete original numeric and item-level records still need recovery and aggregation, without repeating those reviews.'},
            {'id': 'version_separation',
             'zh': '历史答卷与当前1.0.2版输入不同；不得只替换两份Gemini答卷后宣称全配置同版本可比。',
             'en': 'Historical answers and current 1.0.2 inputs differ; replacing two Gemini answers does not create a same-version comparison.'},
            {'id': 'gold_issue_q013',
             'zh': 'Q013原始评审已完成。题面与评分条件存在背景错配，早期有暂停汇总记录；汇总前需恢复并核对后续裁定，保留原题和旧分。',
             'en': 'Q013 original reviews were completed. Its prompt and criteria contain a background mismatch, with holds recorded in an early snapshot. Later dispositions need recovery and reconciliation before aggregation; original inputs and scores are preserved.'},
            {'id': 'reference_q021',
             'zh': 'Q021参考单位名称有误写嫌疑；真实单位已确认，但原参考意图及具体答卷命中仍待统一裁定。',
             'en': 'The real institution relevant to Q021 has been identified, but reference intent and answer-level matching remain unresolved.'},
            {'id': 'missing_answers_pending',
             'zh': '旧版Gemini Q043的17.85分及Qwen Q002的26.97分原裁定已恢复。Kimi Q043、Gemini Q050在恢复快照中仍有待核记录，不能据此认定其后续评审未完成。新版Gemini Q043另有四个待核覆盖项。',
             'en': 'Original adjudications for historical Gemini Q043 (17.85) and Qwen Q002 (26.97) have been recovered. Recovered snapshot pending records for Kimi Q043 and Gemini Q050 do not establish an unfinished later review. Current Gemini Q043 has four separate pending matches.'},
        ],
    }
    put(OUT / 'results.json', clean(data))
    answered = [r for r in data['current_records'] if r['answers']]
    put(OUT / 'manifest.json', {
        'archive': 'current70', 'historical': False, 'official': False,
        'task_version': data['task_version'], 'answer_records': len(answered),
        'per_agent': {agent: sum(r['agent'] == agent for r in answered)
                      for agent in sorted({r['agent'] for r in answered})},
        'records': [{k: r[k] for k in ('agent', 'question_id', 'task_id', 'answers',
                                      'answer_sha256', 'source_answer_export_sha256',
                                      'scoring_status', 'score', 'score_interval')}
                    for r in answered],
        'scope': 'Only physically available, completed current-version reruns; not 70-task completion.',
        'execution_trajectories_public': False,
    })
    FIGURES.mkdir(parents=True, exist_ok=True)
    for zh in (True, False):
        render(data, zh)
        write_report(data, zh)
    checksums(OUT)
    put(FIGURES / 'results-manifest.json', {
        'source_sha256': sha((OUT / 'results.json').read_bytes()), 'contains_model_scores': False,
        'files': {p.name: sha(p.read_bytes()) for p in sorted(FIGURES.glob('*.svg'))
                  if p.suffix == '.svg'},
    })
    print(json.dumps({'historical_numeric_summaries': sum(r['archived_numeric_summaries'] for r in data['historical']['summary']),
                      'current_records': len(data['current_records']),
                      'overall_comparison_ready': data['overall_comparison_ready'],
                      'out': str(OUT.relative_to(ROOT))}))


if __name__ == '__main__':
    main()
