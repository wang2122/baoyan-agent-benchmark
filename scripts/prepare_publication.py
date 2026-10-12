#!/usr/bin/env python3
"""Create a local-only, immutable publication draft. Never calls models or uploads."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark.core import digest, query_prompt


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows), encoding='utf-8')


def inside(base, name):
    rel = Path(name)
    if rel.is_absolute() or '..' in rel.parts or (base / rel).is_symlink():
        raise ValueError('Unsafe artifact path: ' + str(name))
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('Artifact escapes source')
    return path


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('Unsafe identifier')
    return value


SECRET_KEY = re.compile(r'^(?:authorization|cookie|set-cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|client[_-]?secret|email)$', re.I)
USER_HOME = re.compile(r'(?P<root>/Users/|/home/)(?!REDACTED(?:/|\b))[^/\s"\\]+')
WEB_URL = re.compile(r'''https?://[^\s<>"'\\]+''', re.I)


def local_home_matches(value):
    """Match filesystem homes without mistaking public URL paths for them."""
    masked = WEB_URL.sub(lambda match: ' ' * len(match.group()), value)
    return list(USER_HOME.finditer(masked))


def clean_text(value, task_id=None):
    from scripts.privacy_display import scrub
    value = scrub(value, task_id)
    for match in reversed(local_home_matches(value)):
        value = value[:match.start()] + match.group('root') + 'REDACTED' + value[match.end():]
    value = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]', value)
    value = re.sub(r'\b(?:sk-|hf_|ghp_|github_pat_)[A-Za-z0-9_-]{16,}', '[REDACTED]', value)
    value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)\s*[=:]\s*)[^\s,;"\'}]+', r'\1[REDACTED]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b', '[REDACTED_JWT]', value)
    value = re.sub(r'\bAKIA[A-Z0-9]{16}\b', '[REDACTED_KEY]', value)
    value = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[REDACTED_EMAIL]', value)
    value = re.sub(r'(?<![A-Za-z0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![A-Za-z0-9])', '[REDACTED_PHONE]', value)
    value = re.sub(r'(?im)((?:set-cookie|cookie)\s*:\s*)[^\r\n]+', r'\1[REDACTED_COOKIE]', value)
    value = re.sub(r'(?i)((?:[?&]|\\u0026)(?:token|access_token|api_key|key|signature|x-amz-signature|x-goog-signature)=)[^\s"&\\<>]+', r'\1[REDACTED]', value)
    return value


def clean(value, task_id=None):
    if isinstance(value, dict):
        task_id = value.get('task_id', value.get('source_task_id', task_id))
        return {k: '[REDACTED]' if SECRET_KEY.fullmatch(k) else clean(v, task_id) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v, task_id) for v in value]
    return clean_text(value, task_id) if isinstance(value, str) else value


META = ('schema_version', 'adapter', 'adapter_version', 'cli', 'cli_version', 'model', 'product',
        'reported_models', 'model_identity_status', 'model_observation', 'auth_mode', 'execution_config',
        'execution_backend', 'status', 'started_at', 'finished_at', 'created_at', 'elapsed_seconds',
        'timeout_seconds', 'exit_code', 'usage', 'usage_status', 'trajectory_status', 'answer_capture',
        'official', 'task_id', 'task_sha256', 'rubric_sha256', 'protocol_sha256', 'experiment_sha256',
        'isolation_verified', 'host_configuration_verified', 'execution_mode', 'isolation_evidence_source',
        'execution_contract_sha256', 'snapshot_sha256', 'timing_provenance')


def export_capture(source, hashes, destination, task_id=None):
    """Prepare internal evidence copies; these may contain full execution traces."""
    from scripts.publication_evidence import pattern_matches
    from scripts.privacy_display import infer_task, REVISION
    artifacts = []
    for name, expected in sorted(hashes.items()):
        path = inside(source, name)
        current_task = task_id or infer_task(path)
        leaf = path.name
        allowed = (leaf in {'answer.md', 'final-response.md', 'query.md', 'trajectory.jsonl', 'events.jsonl',
                            'trace.jsonl', 'usage.json', 'execution.json', 'protocol.json', 'run.json',
                            'status.json', 'cli.stderr.log', 'stderr.log', 'worker.stderr.log', 'worker.stdout.log', 'result.json'} or
                   (leaf.endswith('-prompt.md') and 'visible' in Path(name).parts))
        if not allowed:
            continue
        raw = path.read_bytes()
        if sha(raw) != expected:
            raise ValueError('Finished artifact changed: ' + str(path))
        body = raw.decode('utf-8')
        if leaf == 'run.json':
            meta = json.loads(body)
            body = json.dumps(clean({k: meta[k] for k in META if k in meta}, current_task), ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        elif path.suffix == '.json':
            body = json.dumps(clean(json.loads(body), current_task), ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        elif path.suffix == '.jsonl':
            lines = []
            for line in body.split('\n'):
                if not line.strip():
                    continue
                try:
                    lines.append(json.dumps(clean(json.loads(line), current_task), ensure_ascii=False, sort_keys=True))
                except json.JSONDecodeError:
                    lines.append(clean_text(line, current_task))
            body = '\n'.join(lines) + ('\n' if lines else '')
        else:
            body = clean_text(body, current_task)
        public = body.encode('utf-8')
        out = inside(destination, name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(public)
        artifacts.append({'path': name, 'source_sha256': expected, 'export_sha256': sha(public),
                          'transformed': public != raw, 'bytes': len(public), 'privacy_display_revision': REVISION,
                          'source_pattern_counts': {k: len(hits)
                              for k, hits in pattern_matches(raw.decode('utf-8')).items() if hits}})
    put(destination / 'artifacts.json', artifacts)
    return artifacts


def export_public_answers(source, hashes, destination, task_id=None):
    """Publish answer text only; keep logs, prompts and runtime metadata internal."""
    answers = {name: expected for name, expected in hashes.items()
               if Path(name).name in {'answer.md', 'final-response.md'}}
    return export_capture(source, answers, destination, task_id=task_id)


def load_selection(path):
    data = read(path)
    if data['task_count'] != len(data['tasks']) or data['task_ids'] != [x['task_id'] for x in data['tasks']]:
        raise ValueError('Selection count/order mismatch')
    if len(set(data['task_ids'])) != data['task_count']:
        raise ValueError('Duplicate task')
    for row in data['tasks']:
        safe_id(row['task_id'])
        for kind in ('task', 'rubric'):
            current = read(inside(ROOT, row[kind + '_path']))
            if digest(current) != row['fingerprints'][kind]:
                raise ValueError('Selected content changed: ' + row['task_id'])
    for name, expected in data['source_files_sha256'].items():
        if sha(inside(ROOT, name).read_bytes()) != expected:
            raise ValueError('Selection dependency changed: ' + name)
    return data


def checksums(folder, relative_path='checksums.json'):
    put(folder / relative_path, {p.relative_to(folder).as_posix(): sha(p.read_bytes())
        for p in sorted(folder.rglob('*'))
        if p.is_file() and '.git' not in p.relative_to(folder).parts
        and p.name != 'checksums.json'})


HF_RETIRED_FILES = frozenset({'selection-20.json', 'selection-40.json',
                             'selection-75.json', 'CALIBRATION-STATUS.md'})
HF_SNAPSHOT_FILES = ('README.md', 'README.zh-CN.md', 'LICENSE', 'CITATION.cff',
                     'manifest.json', 'selection-70.json', 'single-stage.jsonl',
                     'staged.jsonl', 'rubrics.jsonl', 'questions.md',
                     'questions-and-scoring.md', 'scoring.md')


def dataset_snapshot(out, source, readmes=None):
    """Maintain an approved HF snapshot without regenerating questions or scores."""
    source = Path(source).resolve()
    readmes = Path(readmes).resolve() if readmes else source
    if out.exists() or out.resolve().is_relative_to(source):
        raise ValueError('Snapshot destination must be new and outside the source')
    selected = read(source / 'selection-70.json')
    ids = selected['task_ids']
    expected = read(ROOT / 'config/selections/current.json')['task_ids']
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError('HF snapshot differs from the current approved task IDs')
    if {p.name for p in (source / 'tasks').iterdir()} != set(ids):
        raise ValueError('HF snapshot has extra or missing task directories')
    names = set(HF_SNAPSHOT_FILES)
    for ident in ids:
        safe_id(ident)
        if {p.name for p in (source / 'tasks' / ident).iterdir()} != {'task.json', 'rubric.json'}:
            raise ValueError('Unexpected task artifact: ' + ident)
        names.update('tasks/' + ident + '/' + k + '.json' for k in ('task', 'rubric'))
    declared = read(source / 'checksums.json')
    for name in names:
        if sha(inside(source, name).read_bytes()) != declared.get(name):
            raise ValueError('HF snapshot checksum mismatch: ' + name)
    if (source / '.gitattributes').is_file():
        names.add('.gitattributes')
    for name, expected_hash in selected['source_files_sha256'].items():
        if name not in names or sha(inside(source, name).read_bytes()) != expected_hash:
            raise ValueError('HF selection dependency mismatch: ' + name)
    records = {}
    stage_count = 0
    for filename, staged in (('single-stage.jsonl', False), ('staged.jsonl', True)):
        for line in (source / filename).read_text(encoding='utf-8').split('\n'):
            if not line:
                continue
            row = json.loads(line)
            ident = row['task_id']
            if ident not in ids or ident in records:
                raise ValueError('Unexpected or duplicate HF task record: ' + ident)
            task = read(source / 'tasks' / ident / 'task.json')
            if json.loads(row['task_json']) != task:
                raise ValueError('HF task record differs from canonical material: ' + ident)
            stages = json.loads(row['stages_json']) if staged else [row['input']]
            if len(stages) != row['stage_count'] or (staged and stages != task['stages']):
                raise ValueError('HF task stage mismatch: ' + ident)
            if not staged and task['prompt'] not in row['input']:
                raise ValueError('HF input differs from canonical prompt: ' + ident)
            stage_count += len(stages)
            records[ident] = row
    rubric_ids = []
    rubric_rows = {}
    for line in (source / 'rubrics.jsonl').read_text(encoding='utf-8').split('\n'):
        if not line:
            continue
        row = json.loads(line)
        ident = row['task_id']
        if ident not in ids or json.loads(row['rubric_json']) != read(source / 'tasks' / ident / 'rubric.json'):
            raise ValueError('HF rubric record differs from canonical material: ' + ident)
        rubric_ids.append(ident)
        rubric_rows[ident] = row
    manifest = read(source / 'manifest.json')
    if (set(records) != set(ids) or len(rubric_ids) != len(ids) or set(rubric_ids) != set(ids)
            or selected['task_count'] != len(ids) or manifest['task_count'] != len(ids)
            or stage_count != selected['stage_count'] or stage_count != manifest['stage_count']):
        raise ValueError('HF snapshot counts or coverage mismatch')
    if len(ids) == 70:
        current = read(ROOT / 'config/selections/current.json')
        if manifest.get('projection_schema') != 'github-source-to-hf-public70.1' or \
                manifest.get('github_source_selection_sha256') != sha(
                    (ROOT / 'config/selections/current.json').read_bytes()):
            raise ValueError('HF projection is not bound to the current GitHub selection')
        for entry in current['tasks']:
            ident = entry['task_id']
            source_task = read(ROOT / entry['task_path'])
            source_rubric = read(ROOT / entry['rubric_path'])
            task = read(source / 'tasks' / ident / 'task.json')
            rubric = read(source / 'tasks' / ident / 'rubric.json')
            if any(task.get(key) != source_task.get(key) for key in ('prompt', 'stages')):
                raise ValueError('HF question differs from current GitHub source: ' + ident)
            if task.get('difficulty') != entry['difficulty'] or any(
                    task['review'].get(key) != source_task['review'][key] for key in
                    ('content_status', 'difficulty_status', 'teacher_calibration')) or \
                    rubric.get('teacher_calibration') != source_rubric['teacher_calibration']:
                raise ValueError('HF review metadata differs from current GitHub source: ' + ident)
            if any(row.get('effective_review', {}).get('teacher_calibration') != 'completed_owner_confirmed'
                   for row in (records[ident], rubric_rows[ident])):
                raise ValueError('HF effective review metadata is stale: ' + ident)
    english = (readmes / 'README.md').read_text(encoding='utf-8')
    required_configs = ('single-stage.jsonl', 'staged.jsonl', 'rubrics.jsonl')
    header = english.split('---', 2)[1] if english.startswith('---\n') else ''
    if any('path: ' + name not in header for name in required_configs):
        raise ValueError('HF README does not declare the three current loader files')
    for filename in ('README.md', 'README.zh-CN.md'):
        card = (readmes / filename).read_text(encoding='utf-8')
        if any(name in card for name in HF_RETIRED_FILES):
            raise ValueError('HF README still references retired files: ' + filename)
        if '屿智同行' not in card or '保研岛' not in card:
            raise ValueError('HF README lacks project attribution or acknowledgements')
    # Validate everything before creating the destination; copy approved bytes unchanged.
    out.mkdir(parents=True)
    for name in sorted(names):
        origin = readmes / name if name in ('README.md', 'README.zh-CN.md') else source / name
        dest = inside(out, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(origin.read_bytes())
    checksums(out)
    return {'out': str(out), 'tasks': len(ids), 'stages': stage_count,
            'retired_files_excluded': sorted(HF_RETIRED_FILES), 'uploaded': False}


def dataset(out, selected):
    """Export only the current questions, stages and reviewer-only rubrics."""
    out.mkdir(parents=True)
    tasks, rubrics = [], []
    for row in selected['tasks']:
        task = read(ROOT / row['task_path'])
        rubric = read(ROOT / row['rubric_path'])
        identity = {k: row[k] for k in ('question_id', 'task_id', 'stage_count', 'track')}
        if row['track'] == 'dev':
            tasks.append(dict(identity, input=query_prompt(task)))
        else:
            tasks.append(dict(identity, stages=task['stages'], constraints=task['constraints']))
        rubrics.append(dict(identity, rubric=rubric))
        for kind, value in (('task', task), ('rubric', rubric)):
            put(out / 'tasks' / row['task_id'] / (kind + '.json'), value)
    jsonl(out / 'tasks.jsonl', tasks)
    jsonl(out / 'rubrics.jsonl', rubrics)
    put(out / 'selection.json', selected)
    for source, name in (
            ('data/questions-and-scoring.md', 'questions-and-scoring.md'),
            ('docs/scoring.md', 'scoring.md'), ('LICENSE-DATA', 'LICENSE'),
            ('docs/huggingface/CITATION.cff', 'CITATION.cff'),
            ('docs/huggingface/README.md', 'README.md'),
            ('docs/huggingface/README.zh-CN.md', 'README.zh-CN.md')):
        (out / name).write_bytes((ROOT / source).read_bytes())
    put(out / 'manifest.json', {'schema': 'current-dataset-export.1',
        'task_count': selected['task_count'], 'stage_count': selected['stage_count'],
        'document_sha256': selected['source_document_sha256'],
        'answer_records': 0, 'score_records': 0, 'uploaded': False})
    checksums(out)


def repository(out, selected, cohort_agents=None):
    """Copy the current source distribution, without any execution artifacts."""
    import tempfile
    from benchmark.archives import read_archive
    from benchmark.release import build_release
    from benchmark.full_batch import validate_selection
    validate_selection(ROOT, selected)
    with tempfile.TemporaryDirectory(prefix='current-source-') as tmp:
        package = Path(tmp) / 'source.zip'
        build_release(ROOT, package)
        _, files = read_archive(package)
    out.mkdir(parents=True)
    for name, body in files.items():
        path = inside(out, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    put(out / 'source-manifest.json', {
        'schema': 'current-source-export.1',
        'files': {name: sha(body) for name, body in files.items()}})


def questions_only(out, selected):
    """Compatibility entry point for a dataset-only local export."""
    dataset(out, selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--questions-only', action='store_true')
    parser.add_argument('--hf-source', type=Path,
                        help='Maintain an approved HF snapshot with the current three loader files')
    parser.add_argument('--hf-readmes', type=Path,
                        help='Approved bilingual README folder for --hf-source maintenance')
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise ValueError('Destination already exists; use a new output directory')
    if args.hf_source:
        if args.questions_only:
            parser.error('--hf-source cannot be combined with --questions-only')
        print(json.dumps(dataset_snapshot(out, args.hf_source, args.hf_readmes), ensure_ascii=False))
        return
    if args.hf_readmes:
        parser.error('--hf-readmes requires --hf-source')
    selected = load_selection(ROOT / 'config/selections/current.json')
    if args.questions_only:
        questions_only(out, selected)
    else:
        repository(out / 'github', selected)
        dataset(out / 'huggingface', selected)
    print(json.dumps({'out': str(out), 'tasks': selected['task_count'],
        'answers': 0, 'scores': 0, 'uploaded': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
