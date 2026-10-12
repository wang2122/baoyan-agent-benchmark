"""Versioned preview workflows using the existing runner and scoring contract."""
import json
import os
import re
import sys
from pathlib import Path

from .batch import batch_run
from .runtime_common import file_lock
from .core import digest, file_hash, load_tasks, read, require, safe_id, validate_task, write

VERSION = '1.0.1'
CONTEXT = 'benchmark-reproduction.2'
REPRO_FILES = ('public-smoke-apps.json', 'public-dev-apps.json',
               'materials.protocol.json', 'historical-app.protocol.json')


def engine_files(root):
    root = Path(root).resolve()
    files = [p for folder, pattern in (('benchmark', '*.py'), ('scripts', '*.py'), ('schemas', '*.json'))
             for p in (root / folder).glob(pattern)]
    files += [root / 'config/reproduction' / name for name in REPRO_FILES]
    files += [root / 'docs/scoring.md', root / 'docs/review-protocol.md',
              root / 'config/formal-protocol.example.json', root / 'docs/study.md',
              root / 'config/agents-cli.example.json', root / 'config/native-cli.protocol.json',
              root / 'docs/native-cli.md']
    require(all(p.is_file() and not p.is_symlink() for p in files), 'Missing or linked workflow file')
    return {p.relative_to(root).as_posix(): file_hash(p) for p in sorted(files)}


def suite_rows(root, suite, private_root=None):
    root = Path(root).resolve()
    if suite in ('dev', 'test'):
        return load_tasks(root, suite, private_root)
    require(suite == 'public-smoke', 'Unknown reproduction suite')
    base = root / 'examples/public-smoke'
    manifest = read(base / 'manifest.json')
    require(manifest.get('purpose') == 'public_synthetic_workflow_demo_not_formal_evaluation',
            'Demo collection must be labelled as a workflow demonstration')
    ids = manifest['tasks']
    require(ids and len(ids) == len(set(ids)), 'Invalid demo task list')
    rows = []
    for ident in ids:
        safe_id(ident)
        folder = base / ident
        require(not folder.is_symlink() and not any((folder / n).is_symlink()
                for n in ('task.json', 'rubric.json')), 'Demo files cannot be symlinks')
        task, rubric = read(folder / 'task.json'), read(folder / 'rubric.json')
        validate_task(task, rubric, 'dev')
        require(task['id'] == ident and task['execution']['web_mode'] == 'provided_materials_only',
                'Invalid public demo task')
        require(rubric['scoring_policy_sha256'] == file_hash(root / 'docs/scoring.md'),
                'Demo scoring policy changed')
        require(manifest['fingerprints'].get(ident) == {'task': digest(task), 'rubric': digest(rubric)},
                'Demo collection changed; version its manifest')
        rows.append((task, rubric))
    return rows


def make_context(root, rows, protocol, suite, **extra):
    from .workflow_versions import components
    return dict(schema_version=CONTEXT, release_version=VERSION, suite=suite,
                task_ids=[t['id'] for t, _ in rows],
                fingerprints={t['id']: {'task': digest(t), 'rubric': digest(r)} for t, r in rows},
                protocol=protocol, protocol_sha256=digest(protocol),
                scoring_policy_sha256=file_hash(Path(root) / 'docs/scoring.md'),
                engine_sha256=digest(engine_files(root)),
                components=components(root, extra.get('source', 'legacy_batch')), official=False, **extra)


def verify_context(root, context, private_root=None):
    from .workflow_versions import verify_components
    require(context.get('schema_version') in (CONTEXT, 'benchmark-reproduction.1') and context.get('release_version') == VERSION,
            'Unsupported reproduction version; use the matching release')
    require(context.get('official') is False and context['protocol'].get('formal_results_enabled') is False,
            'Preview workflows cannot produce official results')
    require(context.get('protocol_sha256') == digest(context['protocol']), 'Protocol fingerprint changed')
    require(isinstance(context.get('engine_sha256'), str) and re.fullmatch('[0-9a-f]{64}', context['engine_sha256']),
            'Invalid release audit fingerprint')
    require(context.get('scoring_policy_sha256') == file_hash(Path(root) / 'docs/scoring.md'),
            'Scoring policy differs; use the matching release')
    if context['schema_version'] == CONTEXT:
        verify_components(root, context)
    else:
        require(context.get('engine_sha256') == digest(engine_files(root)),
                'Workflow code/config differs; use the matching release or start a new batch')
    rows = suite_rows(root, context['suite'], private_root)
    require(context.get('task_ids') == [t['id'] for t, _ in rows], 'Expected suite changed or was truncated')
    require(context.get('fingerprints') == {t['id']: {'task': digest(t), 'rubric': digest(r)} for t, r in rows},
            'Task/rubric fingerprints differ')
    return rows


def batch_runs(batch):
    batch = Path(batch).resolve()
    manifest = read(batch / 'batch.json')
    require(manifest.get('plan_sha256') == digest(manifest['plan']), 'Batch plan changed')
    result = []
    for row in manifest['runs']:
        path = Path(row['path'])
        require(not path.is_absolute() and '..' not in path.parts, 'Invalid batch path')
        run = (batch / path).resolve()
        require(run.is_relative_to(batch), 'Run escapes batch directory')
        require(not (run / 'run.json').is_symlink(), 'Linked run record')
        result.append(run)
    require(result and len(result) == len(set(result)), 'Empty or duplicate batch attempts')
    return manifest, result


def batch_context(root, batch, private_root=None):
    batch = Path(batch).resolve()
    if (batch / 'reproduction.json').is_file():
        context = read(batch / 'reproduction.json')
        return context, verify_context(root, context, private_root)
    _, runs = batch_runs(batch)
    splits = {read(run / 'run.json')['split'] for run in runs}
    require(len(splits) == 1, 'Mixed source splits')
    suite = splits.pop()
    rows = suite_rows(root, suite, private_root)
    protocols = [read(run / 'protocol.json') for run in runs]
    require(all(p == protocols[0] for p in protocols), 'Mixed source protocols')
    # Old runs are transportable, but their runtime code/config was not frozen by this workflow.
    context = make_context(root, rows, protocols[0], suite, source='legacy_batch',
                           engine_capture='export_time_not_execution_attestation')
    return context, rows


def reproduce(root, profile_path, out, baselines=None, model=None, execute=False, resume=False):
    from .workflow_versions import same_context
    from .adapters import adapter_build, preflight, select_baselines
    root, out = Path(root).resolve(), Path(out).resolve()
    profile_path = Path(profile_path)
    if not profile_path.is_absolute():
        profile_path = root / profile_path
    profile_path = profile_path.resolve()
    require(profile_path.is_relative_to(root), 'Profile must belong to this release')
    profile = read(profile_path)
    require(profile.get('schema_version') == 'reproduction-profile.1' and profile.get('official') is False,
            'Unsupported reproduction profile')
    rows = suite_rows(root, profile['suite'])
    protocol = read(root / profile['protocol'])
    require(protocol.get('status') == 'frozen_preview' and protocol.get('formal_results_enabled') is False,
            'Reproduction requires a frozen preview protocol')
    require(type(protocol.get('repeats')) is int and protocol['repeats'] > 0
            and type(protocol.get('time_limit_seconds')) is int and protocol['time_limit_seconds'] > 0
            and protocol.get('judge_version'), 'Incomplete reproduction protocol')
    selected = baselines or profile['baseline_ids']
    require(set(selected) <= set(profile['baseline_ids']), 'Baseline not allowed by this profile')
    source = select_baselines(read(root / profile['registry']), selected)
    if model:
        require(len(source['baselines']) == 1 and source['baselines'][0].get('execution_mode') != 'manual_app',
                '--model is only for a single CLI baseline; App labels must be recorded from the interface')
        require(model.strip(), 'Empty model ID')
        source['baselines'][0]['model'] = model
    if profile['suite'] == 'dev':
        require(all(b.get('execution_mode') == 'manual_app' for b in source['baselines']),
                'Historical public dev currently requires the documented App workflow')
    source.update(profile=profile['id'], repeats=protocol['repeats'],
                  timeout_seconds=protocol['time_limit_seconds'], evidence_policy=protocol['web_mode'],
                  human_intervention='submission_and_capture_only_no_answer_repair')
    context = make_context(root, rows, protocol, profile['suite'], source='fixed_profile',
                           profile_sha256=digest(profile), selection_sha256=digest(source),
                           engine_capture='before_execution')
    # Lock covers prepare, registry persistence and execution as one operation.
    with file_lock(out.parent / ('.' + out.name + '.lock'), blocking=False,
                   busy_message='Another process is operating this reproduction batch'):
        if resume:
            require(not (out / 'submission-origin.json').exists(), 'Imported answer sets are review-only; start a new execution batch')
            require((out / 'reproduction.json').is_file(), 'Cannot resume without reproduction context')
            saved_context = read(out / 'reproduction.json')
            verify_context(root, saved_context)
            require(same_context({k: v for k, v in saved_context.items() if k != 'resolved_registry_sha256'}, context),
                    'Profile, selection or code changed')
            registry = read(out / 'resolved-registry.json')
            require(saved_context.get('resolved_registry_sha256') == digest(registry),
                    'Resolved registry changed; preserve pinned commands and credentials routing')
        else:
            require(not out.exists(), 'Reproduction output already exists; preserve it or use --resume')
            report, registry = preflight(source, os.environ)
            blockers = {r['id']: r['blockers'] for r in report if r['blockers']}
            require(not blockers, 'Client preflight blocked: ' + json.dumps(blockers, ensure_ascii=False))
            registry['adapter_build_sha256'] = adapter_build()
            registry_path = out / 'resolved-registry.json'
            for b in registry['baselines']:
                if b.get('execution_mode') != 'manual_app':
                    b['command'] = [sys.executable, str(root / 'scripts/baselines.py'), '--registry',
                                    str(registry_path), 'launch', '--baseline', b['id']]
            context['resolved_registry_sha256'] = digest(registry)
            batch_run(rows, protocol, registry, out)
            write(out / 'resolved-registry.json', registry)
            write(out / 'reproduction.json', context)
            write(out / 'readiness.json', {'model_calls': 0, 'baselines': report})
        return batch_run(rows, protocol, registry, out, execute, True)
