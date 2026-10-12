"""Canonical single/staged source loading and longitudinal dependency inspection."""
import copy
from pathlib import Path

from .core import digest, file_hash, read, require, safe_id, validate_task


def load_source(root, collection, ident):
    root, ident = Path(root).resolve(), safe_id(ident)
    require(collection in ('dev', 'staged-dev'), 'Unknown public/local source collection')
    base = root / 'data' / collection
    manifest = read(base / 'manifest.json')
    require(ident in manifest['tasks'], 'Source missing from manifest: ' + collection + '/' + ident)
    folder = base / ident
    require(not base.is_symlink() and not folder.is_symlink() and folder.resolve().is_relative_to(root),
            'Linked or escaped source directory')
    require(all((folder / name).is_file() and not (folder / name).is_symlink()
                for name in ('task.json', 'rubric.json')), 'Missing or linked source task/rubric')
    task, rubric = (read(folder / name) for name in ('task.json', 'rubric.json'))
    hashes = {'task': digest(task), 'rubric': digest(rubric)}
    require(manifest['fingerprints'].get(ident) == hashes, 'Source manifest fingerprint changed: ' + ident)
    require(rubric.get('scoring_policy_sha256') == file_hash(root / 'docs/scoring.md'),
            'Source scoring policy changed: ' + ident)
    if collection == 'dev':
        validate_task(task, rubric, 'dev')
        require(task['id'] == ident, 'Source/task ID mismatch')
    else:
        require(manifest.get('intended_split') == 'dev', 'Staged source must belong to dev')
        validate_staged(task, rubric, ident)
    return task, rubric, hashes


def validate_staged(task, rubric, ident):
    require(task.get('task_id') == ident and rubric.get('id') == ident, 'Staged source/task ID mismatch')
    stages = task.get('stages')
    require(isinstance(stages, list) and len(stages) > 1, 'Staged source requires multiple stages')
    require([s.get('stage_id') for s in stages] == [f'S{i}' for i in range(len(stages))],
            'Staged source must have contiguous ordered stages')
    require(all(isinstance(s.get(k), str) and s[k].strip() for s in stages for k in ('instruction', 'visible_at')),
            'Staged source requires visible time and instruction')
    require(rubric.get('kind') == 'admissions_rubric_v1', 'Unsupported staged scoring method')


def catalog_changes(root, value):
    """Report every affected episode instead of failing at the first stale source."""
    changed = {}
    for episode in value['episodes']:
        for stage in episode['stages']:
            key = stage['collection'] + '/' + stage['task_id']
            _, _, actual = load_source(root, stage['collection'], stage['task_id'])
            expected = {'task': stage['task_sha256'], 'rubric': stage['rubric_sha256']}
            if actual != expected:
                row = changed.setdefault(key, {'source': key, 'expected': expected, 'current': actual, 'uses': []})
                require(row['expected'] == expected, 'Conflicting pinned versions for source: ' + key)
                row['uses'].append({'episode': episode['id'], 'stage': stage['id']})
    return [changed[key] for key in sorted(changed)]


def proposed_catalog(root, value, version=None):
    proposal = copy.deepcopy(value)
    for episode in proposal['episodes']:
        for stage in episode['stages']:
            _, _, hashes = load_source(root, stage['collection'], stage['task_id'])
            stage.update(task_sha256=hashes['task'], rubric_sha256=hashes['rubric'])
    if version is not None:
        proposal['version'] = version
    return proposal


def inspect_extra_collections(root):
    root = Path(root)
    staged_path = root / 'data/staged-dev/manifest.json'
    staged = {'status': 'not_in_this_distribution', 'tasks': 0}
    if staged_path.is_file():
        manifest = read(staged_path)
        require(len(set(manifest['tasks'])) == len(manifest['tasks']), 'Duplicate staged source IDs')
        for ident in manifest['tasks']:
            load_source(root, 'staged-dev', ident)
        staged = {'status': 'public_dev_separate_track', 'tasks': len(manifest['tasks']), 'version': manifest['version']}
    pilot = {'status': 'not_in_this_distribution', 'episodes': 0, 'source_changes': []}
    if (root / 'data/longitudinal/episodes.json').is_file():
        from .longitudinal import catalog, snapshot
        value = catalog(root)
        changes = catalog_changes(root, value)
        proposed = proposed_catalog(root, value)
        for episode in value['episodes']:
            # Shape checks use a current proposal; no active fingerprints are rewritten.
            snapshot(root, episode['id'], proposed)
        pilot = {'status': 'source_review_required' if changes else 'pinned_sources_valid',
                 'version': value['version'], 'episodes': len(value['episodes']), 'source_changes': changes}
    return {'staged_dev': staged, 'longitudinal': pilot,
            'exit_code': 1 if pilot['source_changes'] else 0}
