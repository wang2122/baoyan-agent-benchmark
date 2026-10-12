"""Public answer layout: model / source run / task. Never reads private runs."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

SOURCES = {'dev20': 'retained-early-scores', 'dev70': 'selected-historical-answers',
           'current70': 'supplemental-input-answers'}
LABELS = {'retained-early-scores': '历史评审材料 / Historical reviewed evidence',
          'selected-historical-answers': '历史选定答卷 / Selected historical answers',
          'supplemental-input-answers': '补充运行答卷 / Supplemental run answers'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def put(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def run_id(archive, record):
    if archive == 'dev20':
        return 'historical-reviewed'
    if archive == 'current70':
        return 'supplemental-inputs'
    original = record['record_id']
    if original.startswith('completed75-'):
        return 'selected-native-' + original.split('-', 1)[1]
    if original.startswith('historical-'):
        return 'historical-sessions-' + original.split('-', 1)[1]
    return 'native-batch-' + re.sub(r'[^a-zA-Z0-9]', '', original)[:12]


def task_path(agent, batch, task):
    # Identifiers come from trusted publication manifests; reject path traversal.
    for value in (agent, batch, task):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
            raise ValueError('Unsafe public identifier: ' + value)
    return Path('models') / agent / 'runs' / batch / task


def write_run_readme(folder, source_id):
    folder = Path(folder)
    title = LABELS.get(source_id, '历史评审材料 / Historical reviewed evidence')
    origin = '[来源记录](manifest.json)' if source_id in ('retained-early-scores', 'retained_historical_scoring') else f'[来源清单](../../../../sources/{source_id}/manifest.json)'
    (folder / 'README.md').write_text(
        f'# {title}\n\n本批次的答案、评分与执行状态见[清单](manifest.json)。\n\n'
        f'[模型结果](../../README.md) · {origin} · [计分方法](../../../../../../docs/result-methodology.md)\n', encoding='utf-8')


def relocate_archive(legacy, destination, archive):
    """Move public files and metadata, preserving every answer and score value.

    Manifests use paths relative to the answer root; per-run manifests use paths
    relative to their run folder. The returned mapping covers every old file.
    """
    legacy, destination = Path(legacy), Path(destination)
    source_id = SOURCES[archive]
    manifest = read(legacy / 'manifest.json')
    mapping, records, grouped, batch_sources = {}, [], defaultdict(list), {}
    original_files = [p for p in legacy.rglob('*') if p.is_file()]
    score_report = read(legacy / 'scores.json') if (legacy / 'scores.json').exists() else {}
    score_rows = score_report.get('rows', [])
    score_report_hash = hashlib.sha256((legacy / 'scores.json').read_bytes()).hexdigest() if score_report else None
    manifest_hash = hashlib.sha256((legacy / 'manifest.json').read_bytes()).hexdigest()
    score_by_task = {(x['agent'], x['task_id']): x for x in score_rows}
    for original in manifest['records']:
        record = dict(original)
        batch = run_id(archive, record)
        key = (record['agent'], batch)
        identity = record.get('record_id', archive)
        if key in batch_sources and batch_sources[key] != identity:
            raise ValueError('Distinct source runs would share a public batch name')
        batch_sources[key] = identity
        task = task_path(record['agent'], batch, record['task_id'])
        record['run_id'] = batch
        record['answers'] = []
        if archive == 'dev70':
            old_task = Path(record['agent']) / record['record_id'] / record['task_id']
        else:
            old_task = Path(record['agent']) / record['task_id']
        for old_answer in original.get('answers', []):
            parts = Path(old_answer).parts
            stage = next((x for x in parts if re.fullmatch(r'S\d+', x)), None)
            relative = Path('stages') / stage / 'answer.md' if stage else Path('answer.md')
            new = task / relative
            mapping[old_answer] = new.as_posix()
            record['answers'].append(new.as_posix())
        folder = legacy / old_task
        for path in folder.rglob('*') if folder.exists() else []:
            if not path.is_file():
                continue
            old = path.relative_to(legacy).as_posix()
            if old not in mapping:
                relative = path.relative_to(folder)
                if len(relative.parts) != 1:
                    raise ValueError('Unindexed public artifact: ' + old)
                mapping[old] = (task / relative).as_posix()
        records.append(record)
        grouped[(record['agent'], batch)].append(record)
    for path in original_files:
        old = path.relative_to(legacy).as_posix()
        if len(Path(old).parts) == 1:
            mapping[old] = (Path('sources') / source_id / old).as_posix()
    if set(mapping) != {p.relative_to(legacy).as_posix() for p in original_files}:
        raise ValueError('Unmapped public files')
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('Public file collision')
    for old, new in mapping.items():
        if not (legacy / old).is_file() or (destination / new).exists():
            raise ValueError('Missing source or existing destination: ' + old)
    # All collisions and unknown artifacts are rejected before moving anything.
    for old, new in mapping.items():
        target = destination / new
        target.parent.mkdir(parents=True, exist_ok=True)
        (legacy / old).rename(target)
    for old, new in mapping.items():
        if Path(old).name != 'artifacts.json':
            continue
        target = destination / new
        artifacts = read(target)
        for entry in artifacts:
            old_artifact = (Path(old).parent / entry['path']).as_posix()
            new_artifact = Path(mapping[old_artifact])
            entry['previous_public_path'] = entry['path']
            entry['path'] = new_artifact.relative_to(Path(new).parent).as_posix()
        put(target, artifacts)
    manifest.update(archive=source_id, legacy_source_archive=archive, records=records,
                    path_base='../..', layout='model-run-task.1')
    put(destination / 'sources' / source_id / 'manifest.json', manifest)
    result_path = destination / 'sources' / source_id / 'results.json'
    if result_path.exists():
        def repath(value):
            if isinstance(value, dict):
                return {k: repath(v) for k, v in value.items()}
            if isinstance(value, list):
                return [repath(v) for v in value]
            if isinstance(value, str) and value in mapping:
                return mapping[value]
            return value
        put(result_path, repath(read(result_path)))
    for (agent, batch), own in grouped.items():
        folder = destination / 'models' / agent / 'runs' / batch
        run_manifest = {'layout': 'model-run-task.1', 'path_base': '.', 'agent': agent,
            'run_id': batch, 'source_archive': source_id,
            'source_manifest': f'sources/{source_id}/manifest.json',
            'source_manifest_path_base': 'answer_root', 'answer_records': sum(bool(x['answers']) for x in own),
            'records': [dict(x, answers=[str(Path(a).relative_to(folder.relative_to(destination)))
                                       for a in x['answers']]) for x in own]}
        if archive == 'dev20':
            run_manifest.pop('source_manifest')
            run_manifest.pop('source_manifest_path_base')
            run_manifest.update(source_archive='retained_historical_scoring', source_legacy_archive=archive,
                source_score_report_sha256=score_report_hash, source_manifest_sha256=manifest_hash,
                reviewer=score_report.get('reviewer'), source_generated_at=score_report.get('generated_at'),
                adjudication=score_report.get('adjudication'), unresolved_facts=score_report.get('unresolved_facts', []))
        for key in ('public_dataset_version', 'privacy_display_revision', 'task_version', 'historical', 'official', 'scoring_semantics'):
            if key in manifest:
                run_manifest[key] = manifest[key]
        if archive == 'dev20':
            run_manifest['original_score_source_sha256'] = score_report.get('source_sha256')
        put(folder / 'manifest.json', run_manifest)
        for record in own:
            row = score_by_task.get((agent, record['task_id']))
            if row:
                # Exact archived row, including deductions and pending nodes; no regrading.
                put(folder / record['task_id'] / 'score.json', row)
        write_run_readme(folder, source_id)
    if archive == 'dev20':
        # Per-task scores and source hashes are public; the independent campaign report is not.
        for path in (destination / 'sources' / source_id).iterdir():
            if not path.is_file():
                raise ValueError('Unexpected source-report directory')
            path.unlink()
        (destination / 'sources' / source_id).rmdir()
        mapping = {old: (None if new.startswith('sources/' + source_id + '/') else new) for old, new in mapping.items()}
    # Only prune empty original directories; never remove unrecognized content.
    for folder in sorted({p.parent for p in original_files} | set(legacy.rglob('*')), key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir() and folder not in (destination, legacy):
            try:
                folder.rmdir()
            except OSError:
                pass
    if legacy != destination and legacy.exists():
        legacy.rmdir()
    return mapping


def refresh_checksums(root):
    root = Path(root)
    for folder in sorted((root / 'sources').glob('*')) + sorted((root / 'models').glob('*/runs/*')):
        if folder.is_dir():
            put(folder / 'checksums.json', {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(folder.rglob('*')) if p.is_file() and p.name != 'checksums.json'})
