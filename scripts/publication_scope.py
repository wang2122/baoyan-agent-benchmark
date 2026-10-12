"""Project sources onto an explicit task allowlist."""
import json
from pathlib import Path
import hashlib


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def project(files, source, destination, selection):
    """Verify all original bytes before deriving a public copy; never edit sources."""
    allowed = set(selection['task_ids'])
    omitted, originals, exported, changed = [], {}, {}, {}
    for name, expected in sorted(files.items()):
        rel = Path(name)
        if not rel.parts or rel.parts[0] not in {'benchmark', 'scripts', 'schemas', 'config', 'docs', 'data', 'examples', 'tests'}:
            raise ValueError('Unexpected source folder')
        if rel.parts[0] == 'tests' and (len(rel.parts) != 2 or not rel.name.startswith('test_') or rel.suffix != '.py'):
            raise ValueError('Unexpected test file')
        if name != 'data/questions-and-scoring.md' and rel.parts[0] == 'data' and (len(rel.parts) < 2 or rel.parts[1] not in {'dev', 'staged-dev', 'longitudinal'}):
            raise ValueError('Non-public data folder')
        if '.local.' in name or name.endswith('.credentials.json'):
            raise ValueError('Private configuration')
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Unsafe source path')
        path = source / rel
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError('Linked or escaping source')
        raw = path.read_bytes()
        if sha(raw) != expected:
            raise ValueError('Source changed: ' + name)
        originals[name] = expected
        if len(rel.parts) >= 3 and rel.parts[:2] in (('data', 'dev'), ('data', 'staged-dev')):
            if rel.parts[2] not in ('manifest.json',) and rel.parts[2] not in allowed:
                omitted.append(name)
                continue
            if rel.parts[2] == 'manifest.json':
                data = json.loads(raw)
                data['tasks'] = [tid for tid in data['tasks'] if tid in allowed]
                data['fingerprints'] = {tid: fp for tid, fp in data['fingerprints'].items() if tid in allowed}
                data['publication_selection_id'] = selection['id']
                data['historical_source_manifest_sha256'] = expected
                raw = encoded(data)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        exported[name] = sha(raw)
        if exported[name] != expected:
            changed[name] = {'original_sha256': expected, 'export_sha256': exported[name],
                             'reason': 'Public allowlist projection; retained task and rubric bytes unchanged'}
    actual = set()
    for track in ('dev', 'staged-dev'):
        manifest = json.loads((destination / 'data' / track / 'manifest.json').read_text())
        actual.update(manifest['tasks'])
    if actual != allowed:
        raise ValueError('Public source task scope mismatch')
    # A retained longitudinal episode must never refer to an excluded original.
    catalog = destination / 'data/longitudinal/episodes.json'
    if catalog.exists():
        for episode in json.loads(catalog.read_text())['episodes']:
            if any(stage['task_id'] not in allowed for stage in episode['stages']):
                raise ValueError('Episode references a non-public task')
    return {'original_files': originals, 'exported_files': exported, 'omitted': omitted,
            'changed': changed, 'projection': 'public-task-allowlist.1',
            'public_task_count': len(allowed), 'public_task_ids': selection['task_ids'],
            'scope': 'Source copy restricted to the selected current tasks'}


def rebind_selection(selection, engine):
    copied = json.loads(json.dumps(selection))
    copied['historical_source_files_sha256'] = copied['source_files_sha256']
    copied['source_files_sha256'] = {name: sha((engine / name).read_bytes())
                                   for name in copied['source_files_sha256']}
    copied['reproduction_adjustment'] = 'Collection-manifest bindings updated; selected task/rubric content unchanged'
    return copied
