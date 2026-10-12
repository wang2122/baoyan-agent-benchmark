"""Non-additive sample accounting; stages/repeats never masquerade as new cases."""
from pathlib import Path
from collections import Counter

from .core import collection, read
from .task_sources import inspect_extra_collections, load_source


def inventory(root, private_root=None):
    root = Path(root).resolve()
    groups = collection(root, private_root)
    extra = inspect_extra_collections(root)
    result = {'schema_version': 'benchmark-inventory.1', 'domain': 'baoyan',
              'sample_unit': 'independently_scored_task_or_episode',
              'split_unit': 'case_group_id', 'tracks_combined': False,
              'private_test_checked': private_root is not None, 'collections': {}}
    for split, rows in groups.items():
        result['collections'][split] = {'canonical_tasks': len(rows),
            'single_stage_tasks': len(rows), 'stages': len(rows),
            'case_groups': len({t['case_group_id'] for t, _ in rows}),
            'difficulty': dict(Counter(t['difficulty'] for t, _ in rows))}
    staged = root / 'data/staged-dev/manifest.json'
    staged_tasks = []
    if staged.is_file():
        value = read(staged)
        staged_tasks = [load_source(root, 'staged-dev', ident)[0] for ident in value['tasks']]
    result['staged_dev'] = {'canonical_tasks': len(staged_tasks),
        'stages': sum(len(t['stages']) for t in staged_tasks),
        'status': 'stored_separate_track' if staged_tasks else 'not_in_this_distribution'}
    catalog = root / 'data/longitudinal/episodes.json'
    episodes = read(catalog)['episodes'] if catalog.is_file() else []
    sources = {(s['collection'], s['task_id']) for e in episodes for s in e['stages']}
    result['longitudinal'] = {'derived_episodes': len(episodes),
        'stages': sum(len(e['stages']) for e in episodes),
        'case_groups': len({e['case_group_id'] for e in episodes}),
        'reused_source_tasks': len(sources), 'additional_independent_samples': 0,
        'status': 'derived_public_dev' if episodes else 'not_in_this_distribution'}
    result['longitudinal']['source_status'] = extra['longitudinal']['status']
    result['canonical_dev_tasks'] = len(groups.get('dev', [])) + len(staged_tasks)
    result['counting_rule'] = ('Derived episodes reuse source tasks: report tracks separately; '
        'do not add their stages, repeats or reused source tasks to the independent sample count.')
    result['targets'] = read(root / 'config/benchmark.json')['targets']
    result['formal_results_enabled'] = False
    return result
