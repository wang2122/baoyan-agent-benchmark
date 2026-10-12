"""Build and inspect a local, versioned public release without publishing anything."""
import json
from pathlib import Path, PurePosixPath

from .archives import hash_bytes, read_archive, write_archive
from .core import collection, digest, encoded, read, require, safe_id, validate_task
from .reproduction import REPRO_FILES, VERSION, engine_files, suite_rows

ROOT_FILES = ('.gitignore', 'README.md', 'README.zh-CN.md', 'LICENSE', 'LICENSE-DATA', 'CITATION.cff')
CONFIG_FILES = ('benchmark.json', 'protocol.json', 'baselines.example.json', 'credentials.example.sh',
                'formal-protocol.example.json', 'agents-cli.example.json', 'native-cli.protocol.json',
                'judge.example.json')
SELECTION_FILES = ('current.json',)
LOCAL_TESTS = ('test_longitudinal.py', 'test_nseries.py', 'test_catalog_updates.py')
REVIEW_FILE = 'release/public-review.json'
PUBLIC_COLLECTIONS = ('data/dev', 'data/staged-dev', 'examples/public-smoke')
CATALOG_FILE = 'data/longitudinal/episodes.json'
PUBLIC_DOCS = ('result-methodology.md', 'dataset.md', 'native-cli.md', 'quick-start.md', 'quick-start.en.md',
               'reproduction.md', 'review-protocol.md', 'scoring.md', 'study.md',
               'publication.md', 'experiment-publication.md', 'privacy-display.md', 'README.md')
DATA_DOCUMENT = 'data/questions-and-scoring.md'
HF_FILES = ('README.md', 'README.zh-CN.md', 'CITATION.cff', 'CALIBRATION-STATUS.md')



def check_active_public_scope(root):
    """Do not publish a historical pool beyond the current explicit allowlist."""
    root = Path(root)
    selected = root / 'config/selections/current.json'
    if not selected.exists():
        return
    expected = set(read(selected)['task_ids'])
    actual = set()
    for folder in ('dev', 'staged-dev'):
        manifest = root / 'data' / folder / 'manifest.json'
        if manifest.exists():
            actual.update(read(manifest)['tasks'])
    require(actual == expected,
            'Task pool differs from the current selection; synchronize the task manifests before exporting.')

def check_public_review(files, review, folders=PUBLIC_COLLECTIONS):
    """Require explicit release authorization bound to every packaged task and rubric."""
    require(isinstance(review, dict) and review.get('schema_version') == 'public-release-review.1',
            'Public scope requires an explicit public release review')
    require(isinstance(review.get('reviewer'), str) and review['reviewer'].strip()
            and isinstance(review.get('reviewed_at'), str) and review['reviewed_at'].strip(),
            'Public review requires reviewer and review time')
    expected = {}
    for folder in folders:
        manifest = json.loads(files[folder + '/manifest.json'])
        for ident in manifest['tasks']:
            key = folder + '/' + ident
            task = json.loads(files[key + '/task.json'])
            expected[key] = manifest['fingerprints'][ident]
    if CATALOG_FILE in files:
        require(review.get('catalog_sha256') == digest(json.loads(files[CATALOG_FILE])),
                'Missing or stale public catalog approval')
    records = review.get('tasks')
    require(isinstance(records, dict) and set(records) == set(expected), 'Public review membership differs')
    for key, fingerprints in expected.items():
        row = records[key]
        require(isinstance(row, dict) and row.get('fingerprints') == fingerprints
                and row.get('privacy_approved') is True and row.get('redistribution_approved') is True
                and isinstance(row.get('reason'), str) and row['reason'].strip(),
                'Missing or stale public approval: ' + key)


def public_file(root, path):
    path = Path(path)
    require(path.is_file() and path.resolve().is_relative_to(root)
            and not any((root / Path(*path.relative_to(root).parts[:i])).is_symlink()
                        for i in range(1, len(path.relative_to(root).parts) + 1)),
            'Missing or linked public release file')
    return path


def public_name(name):
    parts = PurePosixPath(name).parts
    if name in (REVIEW_FILE, 'release/hf-review.json', CATALOG_FILE, DATA_DOCUMENT):
        return True
    if name in ROOT_FILES:
        return True
    if len(parts) == 2:
        folder, filename = parts
        if folder == 'config':
            return filename in CONFIG_FILES
        if folder == 'docs':
            return filename in PUBLIC_DOCS
        patterns = {'benchmark': ('.py', '.html'), 'scripts': ('.py',),
                    'schemas': ('.json',)}
        if folder == 'tests':
            return filename.startswith('test_') and filename.endswith('.py')
        return folder in patterns and not filename.startswith('.') and PurePosixPath(filename).suffix in patterns[folder]
    if len(parts) == 3 and parts[:2] == ('docs', 'huggingface'):
        return parts[2] in HF_FILES
    if len(parts) == 3 and parts[:2] == ('config', 'reproduction'):
        return parts[2] in REPRO_FILES
    if len(parts) == 3 and parts[:2] == ('config', 'selections'):
        return parts[2] in SELECTION_FILES
    if parts[:2] in (('data', 'dev'), ('data', 'staged-dev'), ('examples', 'public-smoke')):
        return (len(parts) == 3 and parts[2] == 'manifest.json'
                or len(parts) == 4 and parts[3] in ('task.json', 'rubric.json'))
    return False


def packaged_collection(files, folder, demo=False):
    """Validate public membership/counts from packaged data, never a claimed total."""
    manifest = json.loads(files[folder + '/manifest.json'])
    ids = manifest['tasks']
    require(isinstance(ids, list) and ids and len(ids) == len(set(ids)), 'Invalid packaged collection')
    if demo:
        require(manifest.get('purpose') == 'public_synthetic_workflow_demo_not_formal_evaluation', 'Invalid packaged demo purpose')
    elif folder == 'data/staged-dev':
        require(manifest.get('intended_split') == 'dev', 'Invalid staged dev manifest')
    else:
        require(manifest.get('schema_version') == '1.0' and manifest.get('split') == 'dev', 'Invalid packaged dev manifest')
    expected = {folder + '/manifest.json'}
    for ident in ids:
        safe_id(ident)
        task_name, rubric_name = (folder + '/' + ident + '/' + name for name in ('task.json', 'rubric.json'))
        require(task_name in files and rubric_name in files, 'Missing packaged task/rubric')
        task, rubric = json.loads(files[task_name]), json.loads(files[rubric_name])
        if folder == 'data/staged-dev':
            from .task_sources import validate_staged
            validate_staged(task, rubric, ident)
        else:
            validate_task(task, rubric, 'dev')
        require(task.get('id', task.get('task_id')) == ident and manifest['fingerprints'].get(ident) == {'task': digest(task), 'rubric': digest(rubric)},
                'Packaged task/rubric fingerprint differs')
        require(rubric['scoring_policy_sha256'] == hash_bytes(files['docs/scoring.md']), 'Packaged scoring policy differs')
        if demo:
            require(task['execution']['web_mode'] == 'provided_materials_only', 'Invalid packaged demo mode')
        expected |= {task_name, rubric_name}
    require({name for name in files if name.startswith(folder + '/')} == expected,
            'Undeclared packaged collection files')
    return len(ids)


def packaged_catalog(files):
    from .longitudinal import SCHEMA
    value = json.loads(files[CATALOG_FILE])
    require(value.get('schema_version') == SCHEMA and value.get('official') is False,
            'Invalid packaged episode catalog')
    episodes = value['episodes']
    require(len({e['id'] for e in episodes}) == len(episodes), 'Duplicate packaged episode')
    for episode in episodes:
        stages = episode['stages']
        require(len(stages) > 1 and [s['id'] for s in stages] == [f'S{i}' for i in range(len(stages))],
                'Invalid packaged episode stages')
        selected = {}
        for stage in stages:
            require(stage['collection'] in ('dev', 'staged-dev'), 'Invalid episode collection')
            key = 'data/' + stage['collection'] + '/' + safe_id(stage['task_id'])
            task, rubric = (json.loads(files[key + '/' + n + '.json']) for n in ('task', 'rubric'))
            require(stage['task_sha256'] == digest(task) and stage['rubric_sha256'] == digest(rubric),
                    'Packaged episode source changed')
            require(task['case_group_id'] == episode['case_group_id'], 'Episode case group differs')
            if stage['collection'] == 'dev':
                require(stage['source_stage'] is None, 'Static source cannot select a stage')
            else:
                selected.setdefault(key, []).append(stage['source_stage'])
        for key, ids in selected.items():
            require(ids == [s['stage_id'] for s in json.loads(files[key + '/task.json'])['stages']],
                    'Incomplete packaged staged source')
    return len(episodes)


def build_release(root, out, scope='preview', review_path=None):
    root = Path(root).resolve()
    require(scope in ('preview', 'public'), 'Unknown release scope')
    if scope == 'public':
        check_active_public_scope(root)
    require(scope == 'public' or review_path is None, 'Release review is only used for public scope')
    # Check collection paths before loaders read them, including linked parent directories.
    for folder in PUBLIC_COLLECTIONS:
        manifest = read(public_file(root, root / folder / 'manifest.json'))
        for ident in manifest['tasks']:
            safe_id(ident)
            for name in ('task.json', 'rubric.json'):
                public_file(root, root / folder / ident / name)
    public = collection(root)['dev']  # Private collections are never read or bundled.
    demos = suite_rows(root, 'public-smoke')
    from .longitudinal import catalog, snapshot
    episodes = catalog(root)
    for episode in episodes['episodes']:
        snapshot(root, episode['id'])
    staged = read(root / 'data/staged-dev/manifest.json')
    config = read(root / 'config/benchmark.json')
    require(config.get('code_license') == 'MIT' and config.get('public_data_license') == 'CC-BY-4.0',
            'Release licenses have not been selected')
    paths = [root / name for name in ROOT_FILES] + [root / DATA_DOCUMENT]
    paths += [root / 'config' / name for name in CONFIG_FILES]
    paths += [root / 'config/reproduction' / name for name in REPRO_FILES]
    paths += [root / 'config/selections' / name for name in SELECTION_FILES]
    paths += [root / 'docs' / name for name in PUBLIC_DOCS]
    paths += [root / 'docs/huggingface' / name for name in HF_FILES]
    for folder, patterns in (('benchmark', ('*.py', '*.html')), ('scripts', ('*.py',)),
                             ('schemas', ('*.json',)),
                             ('tests', ('test_*.py',))):
        paths += [p for pattern in patterns for p in (root / folder).glob(pattern)]
    for folder, rows in (('data/dev', public), ('examples/public-smoke', demos)):
        paths.append(root / folder / 'manifest.json')
        paths += [root / folder / task['id'] / name for task, _ in rows for name in ('task.json', 'rubric.json')]
    paths += [root / 'data/staged-dev/manifest.json', root / CATALOG_FILE]
    paths += [root / 'data/staged-dev' / ident / name for ident in staged['tasks']
              for name in ('task.json', 'rubric.json')]
    for path in paths:
        public_file(root, path)
    paths = [p for p in paths if not (p.parent == root / 'tests' and p.name in LOCAL_TESTS)]
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in paths}
    if scope == 'public':
        require(review_path is not None, 'Public scope requires --review with explicit privacy and redistribution approval')
        review = read(review_path)
        check_public_review(files, review)
        files[REVIEW_FILE] = encoded(review)
        files['release/hf-review.json'] = encoded(dict(review, tasks={k: v for k, v in review['tasks'].items() if k.startswith('data/')}))
    for folder in PUBLIC_COLLECTIONS:
        packaged_collection(files, folder, folder == 'examples/public-smoke')
    packaged_catalog(files)
    require(all(public_name(name) for name in files), 'Forbidden public release content')
    value = write_archive(out, files, {'schema_version': 'benchmark-release.1', 'release_version': VERSION,
        'scope': scope, 'distribution': {'local_only_tests_excluded': list(LOCAL_TESTS),
            'collections': list(PUBLIC_COLLECTIONS) + ['data/longitudinal']},
        'dataset_version': config['version'], 'licenses': {'code': 'MIT', 'public_data': 'CC-BY-4.0'},
        'engine_files': engine_files(root), 'engine_sha256': digest(engine_files(root)),
        'public_dev_tasks': len(public) + len(staged['tasks']),
        'single_stage_tasks': len(public), 'staged_dev_tasks': len(staged['tasks']),
        'derived_episodes': len(episodes['episodes']),
        'public_demo_tasks': len(demos), 'official_results_enabled': False,
        'excluded': ['private_test', 'runs', 'answers', 'credentials', 'client_logs', 'local_registries'],
        'verification_scope': 'checksums_not_publisher_signature'})
    return {'archive': str(Path(out).resolve()), 'release_version': VERSION, 'files': len(value['files']),
            'scope': scope, 'public_release_approved': scope == 'public', 'uploaded': False, 'official': False}


def verify_release(package):
    manifest, files = read_archive(package)
    require(manifest.get('schema_version') == 'benchmark-release.1' and manifest.get('release_version') == VERSION,
            'Unsupported release package')
    require(manifest.get('licenses') == {'code': 'MIT', 'public_data': 'CC-BY-4.0'}
            and set(ROOT_FILES) <= set(files), 'Incomplete license/release files')
    require(all(public_name(name) for name in files), 'Forbidden public release content')
    scope = manifest.get('scope', 'preview')  # Old local packages never imply public approval.
    require(scope in ('preview', 'public'), 'Unknown release scope')
    if 'scope' in manifest:
        require(manifest.get('distribution') == {'local_only_tests_excluded': list(LOCAL_TESTS),
                'collections': list(PUBLIC_COLLECTIONS) + ['data/longitudinal']}
                and not any('tests/' + name in files for name in LOCAL_TESTS), 'Release distribution differs')
    required = set(ROOT_FILES) | {DATA_DOCUMENT} | {'config/' + name for name in CONFIG_FILES}
    required |= {'config/reproduction/' + name for name in REPRO_FILES}
    required |= {'config/selections/' + name for name in SELECTION_FILES}
    expected_engine = set(engine_files(Path(__file__).resolve().parents[1]))
    require(required | expected_engine <= set(files), 'Incomplete release workflow files')
    engine = manifest.get('engine_files')
    require(isinstance(engine, dict) and set(engine) == expected_engine and digest(engine) == manifest.get('engine_sha256')
            and all(name in files and manifest['files'][name] == fingerprint for name, fingerprint in engine.items()),
            'Release workflow fingerprint differs')
    dev_count = packaged_collection(files, 'data/dev')
    staged_count = packaged_collection(files, 'data/staged-dev')
    episode_count = packaged_catalog(files)
    demo_count = packaged_collection(files, 'examples/public-smoke', True)
    if scope == 'public':
        require(REVIEW_FILE in files, 'Public release has no bound review')
        check_public_review(files, json.loads(files[REVIEW_FILE]))
        if 'release/hf-review.json' in files:
            check_public_review(files, json.loads(files['release/hf-review.json']), folders=('data/dev', 'data/staged-dev'))
    else:
        require(REVIEW_FILE not in files, 'Preview must not claim public approval')
    require(type(manifest.get('public_dev_tasks')) is int and manifest['public_dev_tasks'] == dev_count + staged_count
            and manifest.get('single_stage_tasks') == dev_count
            and manifest.get('staged_dev_tasks') == staged_count
            and manifest.get('derived_episodes') == episode_count
            and type(manifest.get('public_demo_tasks')) is int and manifest['public_demo_tasks'] == demo_count,
            'Release task counts differ from packaged collections')
    config = json.loads(files['config/benchmark.json'])
    require(config.get('code_license') == 'MIT' and config.get('public_data_license') == 'CC-BY-4.0'
            and config.get('version') == manifest.get('dataset_version'), 'Packaged release metadata differs')
    return {'status': 'verified_release_checksums', 'release_version': manifest['release_version'],
            'scope': scope, 'public_release_approved': scope == 'public',
            'files': len(files), 'public_dev_tasks': dev_count + staged_count,
            'single_stage_tasks': dev_count, 'staged_dev_tasks': staged_count,
            'derived_episodes': episode_count,
            'public_demo_tasks': demo_count, 'official': False,
            'verification_scope': 'content_integrity_not_publisher_authentication'}
