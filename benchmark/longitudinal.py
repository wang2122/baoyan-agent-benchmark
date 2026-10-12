"""Local, operator-mediated longitudinal pilot; no model calls or automatic judging.

One conversation per episode. Only the next released prompt crosses the candidate
boundary. Authoritative snapshots and original answer bytes stay with organizers.
"""
import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .core import candidate_prompt, digest, file_hash, read, require, safe_id, write
from .runtime_common import regular_bytes
from .scoring import judgment_template, score
from .task_sources import load_source


SCHEMA = 'longitudinal-pilot.1'
JUDGE_VERSION = 'longitudinal-review-v1'
INSTRUCTION = (
    '这是同一位学生的连续咨询，请在同一聊天中接续处理。只使用截至本轮已披露的事实；'
    '不得查询学生身份、真实最终去向、未来阶段、参考答案或评分标准。'
    '招生信息须符合本轮时点，无法核实的条件明确列为待核；未给日期不得自行补全。'
    '每轮先完成本轮请求，再更新一份可独立阅读的行动记录：已知事实及状态、目标与约束、'
    '当前方案或候选清单、下一步动作及核验节点、待确认事项。后续轮次说明哪些旧判断保留、'
    '撤销或调整以及依据。题面中的后续事件是外部给定信息，不代表你的建议已经被执行。'
    '请提交完整原始回答，不自评分。'
)
CHECKS = {
    'continuity': '后续使用前序目标、约束和交付物；保持仍有效的判断，合理改变须解释。',
    'updating': '收到新信息后撤销过时行动并重排优先级；不为表现变化而无理由改动。',
    'artifacts': '持续更新可独立阅读的行动记录、候选及待核清单，修复发现的遗漏。',
    'temporal_boundary': '不倒灌未来事实，不虚构成果、回复期限或执行结果，不把给定事件归功于自身。',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def catalog(root):
    path = root / 'data/longitudinal/episodes.json'
    require(path.is_file(), 'Missing public dev episode catalog: data/longitudinal/episodes.json')
    value = read(path)
    require(value['schema_version'] == SCHEMA and value['official'] is False, 'Invalid pilot catalog')
    require(len({e['id'] for e in value['episodes']}) == len(value['episodes']), 'Duplicate episode')
    return value


def snapshot(root, episode_id, catalog_value=None):
    if episode_id.startswith('staged:'):
        return staged_snapshot(root, episode_id[7:])
    value = catalog(root) if catalog_value is None else catalog_value
    matches = [e for e in value['episodes'] if e['id'] == episode_id]
    require(len(matches) == 1, 'Unknown episode')
    episode = matches[0]
    stages, sources = [], {}
    for i, ref in enumerate(episode['stages']):
        require(ref['id'] == f'S{i}', 'Stages must be ordered and contiguous')
        folder, tid = ref['collection'], safe_id(ref['task_id'])
        require(folder in ('dev', 'staged-dev'), 'Only approved pilot source collections allowed')
        task, rubric, hashes = load_source(root, folder, tid)
        require(hashes == {'task': ref['task_sha256'], 'rubric': ref['rubric_sha256']},
                'Pilot source changed: ' + folder + '/' + tid +
                '; run benchmark validate or longitudinal update-plan, then review a new catalog version')
        require(rubric['scoring_policy_sha256'] == file_hash(root / 'docs/scoring.md'), 'Scoring policy changed')
        if folder == 'dev':
            require(task['case_group_id'] == episode['case_group_id'], 'Source belongs to another student')
            require(ref['source_stage'] is None, 'Static source cannot select a stage')
            prompt = candidate_prompt(task)
        else:
            selected = [s for s in task['stages'] if s['stage_id'] == ref['source_stage']]
            require(len(selected) == 1, 'Unknown source stage')
            require(task.get('case_group_id') == episode['case_group_id'], 'Source belongs to another student')
            # Staged prompts are self-contained at each release boundary.
            require(not task.get('persona'), 'Review nonempty staged persona before exposing it')
            stage = selected[0]
            prompt = stage_question(task, stage)
        key = folder + '/' + tid
        sources[key] = {'task': task, 'rubric': rubric}
        stages.append({'id': ref['id'], 'source': key, 'source_stage': ref['source_stage'],
                       'prompt': INSTRUCTION + '\n\n' + prompt})
    require(len(stages) > 1, 'An episode needs multiple stages')
    # A staged rubric evaluates all its turns together, never a future-blind S0 alone.
    for key, source in sources.items():
        if key.startswith('staged-dev/'):
            require([s['source_stage'] for s in stages if s['source'] == key]
                    == [s['stage_id'] for s in source['task']['stages']], 'Incomplete staged source')
    return {'schema_version': SCHEMA, 'version': value['version'], 'episode': episode,
            'stages': stages, 'sources': sources,
            'scoring_policy': (root / 'docs/scoring.md').read_text(encoding='utf-8')}


def staged_snapshot(root, ident):
    task, rubric, _ = load_source(root, 'staged-dev', ident)
    key = 'staged-dev/' + ident
    return {'schema_version': SCHEMA, 'version': task['task_version'], 'kind': 'staged',
            'episode': {'id': ident, 'case_group_id': task['case_group_id']},
            'sources': {key: {'task': task, 'rubric': rubric}},
            'stages': [{'id': stage['stage_id'], 'source': key, 'source_stage': stage['stage_id'],
                        'prompt': staged_prompt(task, stage)}
                       for stage in task['stages']],
            'scoring_policy': (root / 'docs/scoring.md').read_text(encoding='utf-8')}


def stage_question(task, stage):
    """Render the stage question according to its input format."""
    if task.get('task_version', '').split('+', 1)[0] == '1.0.2':
        return stage['instruction']
    return f"本轮时点：{stage['visible_at']}\n\n{stage['instruction']}"


def staged_prompt(task, stage):
    """The exact independently released stage text, also used in the reading copy."""
    separator = '\n\n' if task.get('task_version', '').split('+', 1)[0] == '1.0.2' else '\n\n约束：\n'
    return (stage_question(task, stage) +
            (separator + '\n'.join(task['constraints']) if task.get('constraints') else ''))


def chain_checks(state):
    return {} if state['snapshot'].get('kind') == 'staged' else CHECKS


def load_run(directory):
    state = read(directory / 'run.json')
    require(state['schema_version'] == SCHEMA, 'Unsupported run')
    require(digest(state['snapshot']) == state['snapshot_sha256'], 'Episode snapshot changed')
    stages, records = state['snapshot']['stages'], state['records']
    require(len(records) <= len(stages), 'Too many answers')
    for i, record in enumerate(records):
        require(record['stage_id'] == stages[i]['id'], 'Answer order changed')
        data = base64.b64decode(record['answer_base64'], validate=True)
        require(digest(record['receipt']) == record['receipt_sha256'], 'Receipt changed')
        require(data.decode('utf-8').strip() and digest(record['answer_base64']) == record['answer_sha256'],
                'Original answer changed')
        require(record['receipt']['conversation_id'] == state['conversation_id'], 'Conversation changed')
    require(state['released'] in (len(records), len(records) + 1)
            and state['released'] <= len(stages), 'Invalid release state')
    return state


def prepare(root, episode_id, directory, candidate_dir, product, model, conversation_id, *, aggregation=None):
    root, directory, candidate_dir = (Path(p).resolve() for p in (root, directory, candidate_dir))
    require(not directory.exists() and not candidate_dir.exists(), 'Use new organizer and candidate directories')
    require(not candidate_dir.is_relative_to(root) and not root.is_relative_to(candidate_dir),
            'Candidate directory must be separate from the source repository')
    require(not candidate_dir.is_relative_to(directory) and not directory.is_relative_to(candidate_dir),
            'Organizer and candidate directories must be separate')
    require(all(isinstance(v, str) and v.strip() for v in (product, model, conversation_id)), 'Record product/model/conversation')
    snap = snapshot(root, episode_id)
    require(aggregation in (None, 'unique_source_macro_mean.1'), 'Unknown episode aggregation')
    state = {'schema_version': SCHEMA, 'snapshot': snap, 'snapshot_sha256': digest(snap),
             'candidate_dir': str(candidate_dir), 'product': product, 'model': model,
             'conversation_id': conversation_id, 'created_at': now(), 'released': 0, 'records': [],
             'official': False, 'isolation_verified': False,
             'session_evidence': 'operator_reported_unverified'}
    if aggregation:
        state['aggregation'] = aggregation
    directory.mkdir(parents=True)
    write(directory / 'run.json', state)
    return release(directory)


def release(directory):
    state = load_run(directory)
    stages, count = state['snapshot']['stages'], len(state['records'])
    if count == len(stages):
        return {'status': 'completed', 'stages': count, 'official': False}
    stage = stages[count]
    # Idempotent re-export of the current stage; cannot skip an unanswered stage.
    if state['released'] == count:
        state['released'] += 1
        state['released_at'] = now()
        write(directory / 'run.json', state)
    work = Path(state['candidate_dir'])
    work.mkdir(parents=True, exist_ok=True)
    path = work / (stage['id'] + '-prompt.md')
    payload = stage['prompt'].encode('utf-8')
    if path.exists() or path.is_symlink():
        require(regular_bytes(path) == payload, 'Released prompt was edited')
    else:
        path.write_bytes(payload)
    return {'status': 'awaiting_answer', 'stage_id': stage['id'], 'prompt': str(path),
            'conversation_id': state['conversation_id'], 'official': False}


def record_answer(directory, stage_id, answer_path, receipt_path, *, native=False):
    state = load_run(directory)
    require(native == (state.get('execution_backend') in ('native_codex', 'native_cli')),
            'Native captures cannot be replaced through manual answer import')
    i = len(state['records'])
    require(i < len(state['snapshot']['stages']) and state['released'] == i + 1, 'Release a stage before recording')
    require(stage_id == state['snapshot']['stages'][i]['id'], 'Wrong stage or answer already recorded')
    receipt = read(receipt_path)
    require(receipt.get('stage_id') == stage_id, 'Receipt stage mismatch')
    for key in ('conversation_id', 'product', 'model'):
        require(receipt.get(key) == state[key], 'Receipt differs: ' + key)
    require(receipt.get('same_conversation') is True and receipt.get('answer_unedited') is True,
            'Same conversation and original answer attestation required')
    require(type(receipt.get('extra_user_messages')) is int and receipt['extra_user_messages'] == 0,
            'Only the released stage prompt may be added')
    times = [datetime.fromisoformat(receipt[k].replace('Z', '+00:00')) for k in ('started_at', 'finished_at')]
    require(all(t.utcoffset() is not None for t in times) and times[1] >= times[0], 'Invalid receipt timestamps')
    previous = state['records'][-1]['receipt']['finished_at'] if i else state['created_at']
    require(times[0] >= datetime.fromisoformat(previous) and
            times[0] >= datetime.fromisoformat(state['released_at']), 'Stage started before release or previous completion')
    data = regular_bytes(answer_path)
    require(data.decode('utf-8').strip(), 'Empty answer')
    encoded = base64.b64encode(data).decode('ascii')
    state['records'].append({'stage_id': stage_id, 'answer_base64': encoded,
                             'answer_sha256': digest(encoded), 'receipt': receipt,
                             'receipt_sha256': digest(receipt), 'recorded_at': now()})
    write(directory / 'run.json', state)
    return {'status': 'answer_recorded', 'stage_id': stage_id,
            'completed_stages': i + 1, 'total_stages': len(state['snapshot']['stages']),
            'next': 'Run next to release the following stage', 'official': False}


def answer_text(record):
    return base64.b64decode(record['answer_base64'], validate=True).decode('utf-8')


def review_state(directory):
    state = load_run(directory)
    if state.get('execution_backend') in ('native_codex', 'native_cli'):
        from .episode_runner import verify_attempts
        verify_attempts(directory, read(directory / 'native-episode.json'), state)
    return state


def stage_artifacts(directory, stage_id):
    from .episode_state import load_checkpoint
    checkpoint = load_checkpoint(directory / 'stages' / stage_id / 'checkpoint.json')
    # Session logs may contain account/provider metadata: reviewers get artifacts only.
    return {name: value for name, value in checkpoint['files'].items() if name.startswith('artifacts/')}


def review_units(state):
    stages = state['snapshot']['stages']
    records = state['records']
    require(len(records) == len(stages), 'Complete all stages before reviewing the episode')
    result = {}
    for key, source in state['snapshot']['sources'].items():
        indices = [i for i, s in enumerate(stages) if s['source'] == key]
        last = max(indices)
        # Context ends at this unit, preventing later evidence from changing earlier grading.
        history = [{'stage_id': stages[i]['id'], 'prompt': stages[i]['prompt'],
                    'answer': answer_text(records[i])} for i in range(last + 1)]
        task = {'id': source['rubric']['id'], 'prompt': '\n\n'.join(stages[i]['prompt'] for i in indices),
                'context': {'visible_history': history}, 'episode_version': state['snapshot']['version']}
        answer = '\n\n'.join(f"### {stages[i]['id']}\n{answer_text(records[i])}" for i in indices)
        result[task['id']] = (task, source['rubric'], answer)
    return result


def export_review(directory, out):
    state = review_state(directory)
    units = review_units(state)
    require(not out.exists(), 'Review output already exists')
    work = Path(state['candidate_dir'])
    require(not out.resolve().is_relative_to(work) and not work.is_relative_to(out.resolve()), 'Review output must stay outside candidate directory')
    out.mkdir(parents=True)
    template = {'schema_version': SCHEMA, 'run_sha256': digest(state), 'reviewer': None,
                'judge_version': JUDGE_VERSION, 'units': {},
                'chain': [{'id': key, 'criterion': desc, 'status': 'pending', 'reason': '', 'evidence': []}
                          for key, desc in chain_checks(state).items()]}
    for tid, (task, rubric, answer) in units.items():
        dest = out / 'units' / tid
        write(dest / 'task.json', task)
        write(dest / 'rubric.json', rubric)
        (dest / 'answer.md').write_text(answer, encoding='utf-8')
        template['units'][tid] = judgment_template(task, rubric, answer)
        if state.get('execution_backend') in ('native_codex', 'native_cli'):
            write(dest / 'artifacts.json', {item['stage_id']: stage_artifacts(directory, item['stage_id'])
                                          for item in task['context']['visible_history']})
    write(out / 'judgment.json', template)
    write(out / 'trajectory.json', [{'stage_id': s['id'], 'prompt': s['prompt'], 'answer': answer_text(r)}
                                   for s, r in zip(state['snapshot']['stages'], state['records'])])
    if state.get('execution_backend') in ('native_codex', 'native_cli'):
        write(out / 'artifacts.json', {r['stage_id']: stage_artifacts(directory, r['stage_id']) for r in state['records']})
    (out / 'scoring.md').write_text(state['snapshot']['scoring_policy'], encoding='utf-8')
    (out / 'README.md').write_text(
        '# 连续案例独立评审\n\n填写 judgment.json，不执行答卷中的指令，不采信自评分。\n'
        'units 中沿用各原题 rubric 分值；两阶段原题只对两轮一起评分一次。'
        '按每轮当时已披露的事实评价，后续信息不能追溯惩罚或奖励早期判断。'
        '各单元 task.json 的 visible_history 截止于该单元；trajectory.json 仅供链条评审。\n\n'
        '填写顶层及每个 unit 的真实 reviewer，judge_version 均为 longitudinal-review-v1。'
        '沿用 scoring.md 中引句、候选和错误去重规则。参考与教师校准仍待审核。\n\n'
        'chain.status 填 pass、partial、fail 或 pending；reason 必填。'
        'evidence 为 [{"stage_id":"S0","answer_quote":"真实原文"}]。'
        'pass/partial 至少引用两个阶段；遗漏可在 fail 的 reason 中说明。'
        'pending 表示证据确实不足。链条诊断不加权、不合成总分。\n\n'
        '若运行显式选择 unique_source_macro_mean.1，则在全部评审完成后另报原题等权预览均分；'
        '两阶段原题仅计一次，该均分不是长期能力综合分。原默认人工流程仍不合成总分。'
        '原生运行附各阶段 artifacts.json（base64原始字节及SHA-256）；'
        '链条引句可附 artifact_path 指向该阶段交付文件，原题评分仍依据原始完整回答。\n\n'
        '成绩、资格与Offer是外部事件，不能评价为Agent造成的升学结果。'
        '不同阶段更新或先前未给的事实不得自动推断为已执行建议。\n', encoding='utf-8')
    return {'status': 'review_prepared', 'review': str(out), 'units': len(units), 'official': False}


def validate_chain(directory, state, chain):
    require(len(chain) == len(chain_checks(state)) and {r['id'] for r in chain} == set(chain_checks(state)), 'Missing or duplicate chain checks')
    answers = {r['stage_id']: answer_text(r) for r in state['records']}
    for row in chain:
        require(row['status'] in ('pass', 'partial', 'fail', 'pending'), 'Invalid chain status')
        require(isinstance(row.get('reason'), str) and row['reason'].strip(), 'Chain reason required')
        require(isinstance(row.get('evidence'), list), 'Chain evidence required')
        cited = set()
        for item in row['evidence']:
            require(item.get('stage_id') in answers, 'Unknown evidence stage')
            quote = item.get('answer_quote')
            evidence_text = answers[item['stage_id']]
            if item.get('artifact_path') is not None:
                require(state.get('execution_backend') in ('native_codex', 'native_cli'), 'Artifact evidence requires native capture')
                artifacts = stage_artifacts(directory, item['stage_id'])
                require(item['artifact_path'] in artifacts, 'Unknown artifact evidence')
                evidence_text = base64.b64decode(artifacts[item['artifact_path']]['base64'], validate=True).decode('utf-8')
            require(isinstance(quote, str) and quote.strip() and quote in evidence_text, 'Chain quote missing from original answer or artifact')
            cited.add(item['stage_id'])
        require(row['status'] not in ('pass', 'partial') or len(cited) >= 2, 'Positive chain diagnosis needs evidence from two stages')


def apply_review(directory, judgment_path, out):
    state = review_state(directory)
    units = review_units(state)
    judgment = read(judgment_path)
    require(judgment.get('schema_version') == SCHEMA and judgment.get('run_sha256') == digest(state), 'Review belongs to another run or run changed')
    require(isinstance(judgment.get('reviewer'), str) and judgment['reviewer'].strip(), 'Reviewer required')
    require(judgment.get('judge_version') == JUDGE_VERSION, 'Wrong judge version')
    require(set(judgment['units']) == set(units), 'Missing or extra review units')
    scores = {}
    for tid, (task, rubric, answer) in units.items():
        j = judgment['units'][tid]
        require(j.get('reviewer') == judgment['reviewer'] and j.get('judge_version') == JUDGE_VERSION, 'Inconsistent reviewer settings')
        require(j.get('candidates_reviewed') is True, 'Review all candidates, including an empty set')
        scores[tid] = score(task, rubric, answer, j)
    chain = judgment['chain']
    validate_chain(directory, state, chain)
    require(not out.exists() and not out.is_symlink(), 'Result output already exists')
    work = Path(state['candidate_dir'])
    require(not out.resolve().is_relative_to(work), 'Results must stay outside candidate directory')
    pending = any(s['score'] is None for s in scores.values()) or any(r['status'] == 'pending' for r in chain)
    result = {'schema_version': SCHEMA, 'run_sha256': digest(state), 'judgment': judgment,
              'status': 'pending_evidence' if pending else 'reviewed_pilot', 'unit_scores': scores,
              'chain_diagnostics': chain, 'episode_score': None,
              'official': False, 'calibration': 'pending', 'model_run_verified': False}
    if state.get('aggregation') == 'unique_source_macro_mean.1':
        # Existing source rubrics remain unchanged; a two-stage source counts once.
        result['aggregation'] = state['aggregation']
        result['episode_score'] = None if pending else round(sum(s['score'] for s in scores.values()) / len(scores), 6)
        result['score_scope'] = ('original_two_stage_task' if state['snapshot'].get('kind') == 'staged' else
                                 'preview_source_quality_mean_not_calibrated_long_horizon_composite')
        result['chain_diagnostics_summary'] = {
            status: sum(r['status'] == status for r in chain) for status in ('pass', 'partial', 'fail', 'pending')}
        result['episode_id'] = state['snapshot']['episode']['id']
        result['case_group_id'] = state['snapshot']['episode']['case_group_id']
    write(out, result)
    return {'status': result['status'], 'result': str(out), 'episode_score': result['episode_score'], 'official': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    update = sub.add_parser('update-plan', help='Prepare source changes for review; do not rewrite active pins')
    update.add_argument('--version', required=True)
    update.add_argument('--out', type=Path, required=True)
    update = sub.add_parser('apply-update', help='Apply explicitly reviewed source pins as a new local version')
    update.add_argument('--plan-dir', type=Path, required=True)
    update.add_argument('--review', type=Path, required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--episode', required=True)
    prep.add_argument('--out', type=Path, required=True)
    prep.add_argument('--candidate-dir', type=Path, required=True)
    prep.add_argument('--aggregation', choices=('unique_source_macro_mean.1',))
    for key in ('product', 'model', 'conversation-id'):
        prep.add_argument('--' + key, required=True)
    for action in ('next', 'record', 'review', 'score'):
        cmd = sub.add_parser(action)
        cmd.add_argument('--run-dir', type=Path, required=True)
        if action == 'record':
            cmd.add_argument('--stage', required=True)
            cmd.add_argument('--answer', type=Path, required=True)
            cmd.add_argument('--receipt', type=Path, required=True)
        if action in ('review', 'score'):
            cmd.add_argument('--out', type=Path, required=True)
        if action == 'score':
            cmd.add_argument('--judgment', type=Path, required=True)
    args = p.parse_args(argv)
    if args.action == 'update-plan':
        from .catalog_updates import prepare_update
        return prepare_update(args.root, args.out, args.version)
    if args.action == 'apply-update':
        from .catalog_updates import apply_update
        return apply_update(args.root, args.plan_dir, args.review)
    if args.action == 'list':
        return {'version': catalog(args.root)['version'], 'episodes': [
            {'id': e['id'], 'title': e['title'], 'stages': len(e['stages']), 'case_group_id': e['case_group_id']}
            for e in catalog(args.root)['episodes']], 'official': False}
    if args.action == 'prepare':
        return prepare(args.root, args.episode, args.out, args.candidate_dir, args.product, args.model, args.conversation_id,
                       aggregation=args.aggregation)
    if args.action == 'next':
        return release(args.run_dir)
    if args.action == 'record':
        return record_answer(args.run_dir, args.stage, args.answer, args.receipt)
    if args.action == 'review':
        return export_review(args.run_dir, args.out)
    return apply_review(args.run_dir, args.judgment, args.out)


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, TypeError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        sys.exit(2)
