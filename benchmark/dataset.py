"""Generate a HuggingFace release from the canonical public dataset."""
import json
from pathlib import Path
from .core import candidate, collection, digest, encoded, file_hash, query_prompt, read, require, write
from .release import CATALOG_FILE, check_public_review, check_active_public_scope
from .task_sources import load_source


def export_hf(root, out, scope='preview', review_path=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    require(not out.exists(), 'Export destination already exists')
    require(scope in ('preview', 'public'), 'Unknown HuggingFace export scope')
    if scope == 'public':
        check_active_public_scope(root)
    require(scope == 'public' or review_path is None, 'Release review is only used for public scope')
    rows = collection(root).get('dev', [])
    require(rows, 'Public dev is empty')
    staged_manifest = root / 'data/staged-dev/manifest.json'
    staged = [load_source(root, 'staged-dev', ident) for ident in read(staged_manifest)['tasks']] if staged_manifest.is_file() else []
    episodes = None
    if (root / CATALOG_FILE).is_file():
        from .longitudinal import catalog, snapshot
        episodes = catalog(root)
        for episode in episodes['episodes']:
            snapshot(root, episode['id'])
    review = None
    if scope == 'public':
        require(review_path is not None, 'Public scope requires --review with explicit privacy and redistribution approval')
        review = read(review_path)
        files = {'data/dev/manifest.json': encoded(read(root / 'data/dev/manifest.json'))}
        files.update({'data/dev/' + task['id'] + '/' + name + '.json': encoded(value)
                      for task, rubric in rows for name, value in (('task', task), ('rubric', rubric))})
        folders = ['data/dev']
        if staged_manifest.is_file():
            folders.append('data/staged-dev')
            files['data/staged-dev/manifest.json'] = encoded(read(staged_manifest))
            files.update({'data/staged-dev/' + task['task_id'] + '/' + name + '.json': encoded(value)
                          for task, rubric, _ in staged for name, value in (('task', task), ('rubric', rubric))})
        if episodes is not None:
            files[CATALOG_FILE] = encoded(episodes)
        check_public_review(files, review, folders=folders)
    samples = []
    for task, rubric in rows:
        public = candidate(task)
        samples.append({'id': task['id'], 'input': query_prompt(task), 'context': public['context'],
                        'deliverables': public['deliverables'], 'execution': public['execution'],
                        'rubric': rubric, 'reference': rubric['reference_candidates'],
                        'metadata': {'split': 'dev', 'task_version': task['task_version'],
                                     'task_kind': task['task_kind'], 'domain': task['domain'], 'review': task['review'],
                                     'case_group_id': task['case_group_id'], 'difficulty': task['difficulty'],
                                     'track': 'single_stage', 'sample_unit': 'task',
                                     'fingerprints': {'task': digest(task), 'rubric': digest(rubric)}}})
    out.mkdir(parents=True)
    document = root / 'data/questions-and-scoring.md'
    if document.is_file():
        (out / 'questions-and-scoring.md').write_bytes(document.read_bytes())
    selection = root / 'config/selections/current.json'
    if selection.is_file():
        (out / 'selection.json').write_bytes(selection.read_bytes())
    (out / 'dev.jsonl').write_text(''.join(json.dumps(s, ensure_ascii=False) + '\n' for s in samples), encoding='utf-8')
    staged_samples = [{'id': task['task_id'], 'stages': task['stages'], 'constraints': task['constraints'],
                       'rubric': rubric, 'metadata': {'split': 'dev', 'track': 'staged', 'sample_unit': 'task',
                       'task_version': task['task_version'], 'case_group_id': task['case_group_id'],
                       'review': task['review'], 'fingerprints': hashes}}
                      for task, rubric, hashes in staged]
    (out / 'staged-dev.jsonl').write_text(''.join(json.dumps(s, ensure_ascii=False) + '\n' for s in staged_samples), encoding='utf-8')
    if episodes is not None:
        write(out / 'episodes.json', episodes)
    card = (root / 'docs/huggingface/README.md').read_text(encoding='utf-8')
    card += ('\n## 本次导出范围\n\n'
             f'本目录实际导出 **{len(rows)} 道单阶段 dev 题**、'
             f'**{len({t["case_group_id"] for t, _ in rows})} 个学生案例组**。'
             f'另含 **{len(staged)} 道两阶段 dev 题**、**{len(episodes["episodes"]) if episodes else 0} 条衍生连续案例**。'
             '不含私有 test；连续案例不增加原题数。\n\n'
             f'分发范围：`{scope}`；' + ('已提供逐题公开审核。' if scope == 'public' else '预览文件不附公开授权声明。') +
             '\n\n`rubric`、`reference` 仅供开发与评审，不能作为被测 Agent 输入；'
             '请使用仓库 candidate 投影或作答包。难度标签仍须按原数据审核状态解释。\n')
    (out / 'README.md').write_text(card, encoding='utf-8')
    (out / 'scoring.md').write_text((root / 'docs/scoring.md').read_text(encoding='utf-8'), encoding='utf-8')
    if review is not None:
        write(out / 'public-review.json', review)
    write(out / 'manifest.json', {'version': read(root / 'config/benchmark.json')['version'], 'split': 'dev', 'sample_count': len(rows) + len(staged),
                                'scope': scope, 'public_release_approved': scope == 'public',
                                'sample_unit': 'task', 'track': 'separate_tracks',
                                'single_stage_count': len(rows), 'staged_count': len(staged),
                                'derived_episode_count': len(episodes['episodes']) if episodes else 0,
                                'case_group_count': len({t['case_group_id'] for t, _ in rows}),
                                'review_sha256': digest(review) if review is not None else None,
                                'scoring_policy_sha256': file_hash(out / 'scoring.md'),
                                'dev_sha256': file_hash(out / 'dev.jsonl'),
                                'staged_dev_sha256': file_hash(out / 'staged-dev.jsonl'),
                                'episodes_sha256': file_hash(out / 'episodes.json') if episodes else None})
    return {'out': str(out), 'sample_count': len(rows) + len(staged), 'scope': scope,
            'public_release_approved': scope == 'public', 'uploaded': False}
