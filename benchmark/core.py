"""Versioned task loading, candidate projection and integrity checks."""
import hashlib
import os
import re
from pathlib import Path

VERSION = '1.0'
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$')
SPLITS = ('dev', 'test')
METHOD = 'admissions_rubric_v1'
CANDIDATE_INPUT_VERSION = 'candidate-input.2'

# Keep the public helper names and canonical JSON bytes stable for old callers.
from .runtime_common import digest, encoded, read, require, sha as file_hash, write


def text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def safe_id(value):
    require(isinstance(value, str) and ID.fullmatch(value), 'Invalid task/group ID')
    return value


def storage(root, split, private_root=None):
    require(split in SPLITS, 'Unknown split')
    root = Path(root).resolve()
    if split == 'test':
        require(private_root is not None, 'test requires --private-root outside this repository')
        p = Path(private_root).resolve()
        require(not p.is_relative_to(root), 'Private test storage must be outside the repository')
        return p
    return root / 'data' / split


def candidate(task):
    # Explicit field projection: never serialize the task's internal audit record.
    fields = ('id', 'task_version', 'prompt', 'context', 'deliverables', 'execution')
    if task['task_version'] in ('dev-v0.5', 'dev-v0.6') or (
            task['task_version'].split('+', 1)[0] in ('1.0.0', '1.0.1', '1.0.2') and not task['context']['persona']):
        fields = tuple(k for k in fields if k != 'id')
    result = {k: task[k] for k in fields}
    if input_version(task) == CANDIDATE_INPUT_VERSION:
        # The organizer retains structured context; candidate files carry it once,
        # in a self-contained question, without duplicating it as a second record.
        result['prompt'] = candidate_question(task)
        result['context'] = {'persona': {}, 'observation': {}}
    return result


def input_version(task):
    version = task.get('execution', {}).get('prompt_version')
    require(version in (None, CANDIDATE_INPUT_VERSION), 'Unsupported candidate input version')
    return version


def candidate_question(task):
    """Question plus only context facts not already present in its wording."""
    prompt = task['prompt'].strip()
    extra = []
    for section in ('persona', 'observation'):
        for key, value in task['context'][section].items():
            require(isinstance(value, str), 'Candidate context values must be text')
            if value.strip() and value.strip() not in prompt:
                extra.append(f'{key}：{value}')
    return '\n'.join(extra) + '\n\n' + prompt if extra else prompt


def candidate_prompt(task):
    """Identical candidate-only message for CLI stdin and official App copy/paste."""
    if input_version(task) == CANDIDATE_INPUT_VERSION:
        question = candidate_question(task)
        instructions = ('请完成下面的保研任务，以中文输出完整最终答卷。信息来源及联网要求以题目为准；'
                        '不要读取评分标准、参考答案、其他答卷或工作目录以外的文件。')
        delivery = []
        if 'answer.md' not in question:
            delivery.append('请将完整原始答卷写入 answer.md。')
        if not any(word in question for word in ('自评分', '自行评分')):
            delivery.append('不要自行评分。')
        return '\n\n'.join([instructions, question] + ([''.join(delivery)] if delivery else []))
    # Preserve exact rendering for previously frozen tasks and run snapshots.
    if task['task_version'] in ('dev-v0.5', 'dev-v0.6') or (
            task['task_version'].split('+', 1)[0] in ('1.0.0', '1.0.1', '1.0.2') and not task['context']['persona']):
        when = task['context']['observation'].get('评测时间')
        return (f'评测时间：{when}\n\n' if when else '') + task['prompt']
    return ('请完成下面的保研任务，以中文输出完整最终答卷。信息来源及联网要求以题目为准；'
            '不要读取评分标准、参考答案、其他答卷或工作目录以外的文件。不要自行评分。\n\n'
            + encoded(candidate(task)).decode('utf-8') + '\n\n' + task['prompt'])


def query_prompt(task):
    """New workspace queries match the sent message; old snapshots stay valid."""
    return candidate_prompt(task) if input_version(task) == CANDIDATE_INPUT_VERSION else task['prompt']


def guard_private_output(split, out, root=None):
    """Keep private task snapshots and reports outside the publishable checkout."""
    if split == 'test':
        roots = {Path(__file__).resolve().parents[1], Path(root).resolve() if root else Path(__file__).resolve().parents[1]}
        require(all(not Path(out).resolve().is_relative_to(p) for p in roots),
                'Private test outputs must stay outside the public repository')


def validate_task(task, rubric, split):
    required = ('schema_version', 'id', 'case_group_id', 'split', 'difficulty', 'domain',
                'task_kind', 'task_version', 'prompt', 'context', 'deliverables', 'execution',
                'review', 'rubric_sha256')
    require(all(k in task for k in required), 'Missing task fields')
    require(task['schema_version'] == VERSION, 'Unsupported task schema')
    require(isinstance(task['task_version'], str) and task['task_version'].strip(), 'Missing task version')
    safe_id(task['id']); safe_id(task['case_group_id'])
    require(task['split'] == split, 'Split mismatch')
    require(task['domain'] == 'baoyan', 'Only baoyan tasks are supported')
    require(task['task_kind'] in ('recommendation', 'planning'), 'Unknown task kind')
    require(task['difficulty'] in (None, 'easy', 'medium', 'hard'), 'Invalid difficulty')
    require(isinstance(task['prompt'], str) and task['prompt'].strip(), 'Empty prompt')
    require(isinstance(task['context'], dict) and all(isinstance(task['context'].get(k), dict)
            for k in ('persona', 'observation')), 'Missing persona/observation')
    require(task['deliverables'] == [{'path': 'answer.md', 'format': 'markdown',
                                    'required': True, 'description': '完整原始答卷；不自评分'}],
            'This runner currently supports the answer.md deliverable contract only')
    require(task['execution']['mode'] == 'static_tool_task', 'Dynamic staged environments are not implemented')
    require(task['execution']['stage_count'] == 1, 'Do not flatten multi-stage tasks into this runner')
    require(type(task['execution'].get('trajectory_required')) is bool, 'Record whether trajectory is required')
    input_version(task)
    require(rubric.get('id') == task['id'], 'Rubric/task mismatch')
    require(rubric.get('task_kind') == task['task_kind'], 'Rubric task kind mismatch')
    require(digest(rubric) == task['rubric_sha256'], 'Rubric changed: review and version the task again')
    require(isinstance(task['review'], dict), 'Missing review state')
    require(all(k in task['review'] for k in ('content_status', 'difficulty_status', 'teacher_calibration')), 'Incomplete review metadata')
    require(task['review'].get('content_status') in ('pending', 'approved'), 'Invalid content review state')
    if task['review']['content_status'] == 'approved':
        require(task['difficulty'] is not None, 'Reviewed task needs calibrated difficulty')


def input_hash(task):
    if input_version(task) == CANDIDATE_INPUT_VERSION:
        return digest({'prompt_version': CANDIDATE_INPUT_VERSION, 'prompt': candidate_prompt(task)})
    return digest({key: task[key] for key in ('prompt', 'context')})


def load_tasks(root, split, private_root=None):
    base = storage(root, split, private_root).resolve()
    manifest = read(base / 'manifest.json')
    require(manifest.get('schema_version') == VERSION and manifest.get('split') == split,
            'Invalid collection manifest')
    ids = manifest['tasks']
    require(isinstance(ids, list) and len(ids) == len(set(ids)), 'Duplicate or invalid task IDs')
    rows = []
    policy_hash = file_hash(Path(root) / 'docs/scoring.md')
    for sid in ids:
        safe_id(sid)
        directory = (base / sid).resolve()
        require(directory.is_relative_to(base), 'Task path escapes collection')
        require(not any((directory / name).is_symlink() for name in ('task.json', 'rubric.json')),
                'Task files cannot be symlinks')
        task, rubric = read(directory / 'task.json'), read(directory / 'rubric.json')
        require(task['id'] == sid, 'Manifest/task ID mismatch')
        validate_task(task, rubric, split)
        require(rubric.get('kind') == METHOD, 'Unsupported scoring method')
        require(rubric.get('scoring_policy_sha256') == policy_hash, 'Scoring policy changed; version the release')
        require(manifest['fingerprints'].get(sid) == {'task': digest(task), 'rubric': digest(rubric)},
                'Dataset changed; update the release fingerprints after review')
        rows.append((task, rubric))
    return rows


def collection(root, private_root=None):
    result, ids, groups = {}, set(), {}
    for split in SPLITS:
        if split == 'test' and private_root is None:
            continue
        base = storage(root, split, private_root)
        if not (base / 'manifest.json').exists():
            require(split != 'test', 'Private test manifest not found')
            continue
        rows = load_tasks(root, split, private_root)
        result[split] = rows
        for task, _ in rows:
            require(task['id'] not in ids, 'Task appears in multiple splits')
            ids.add(task['id'])
            group = task['case_group_id']
            require(group not in groups or groups[group] == split, 'Related cases cross splits: ' + group)
            groups[group] = split
    return result
