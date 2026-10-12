"""Project-official public evaluations over immutable, independently verified captures.

The capture engine keeps its original provenance labels. Registration, reuse receipts,
and complete-result admission live here; no historical answer or protocol is relabelled.
"""
import json
import os
from contextlib import ExitStack, nullcontext
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from .core import digest, file_hash, read, require, write
from .runtime_common import file_lock, now

MARKER = 'formal-batch.json'
SCHEMA = 'formal-public-batch.1'
POLICY = {
    'schema_version': 'formal-public-policy.1',
    'scope': 'project_official_public_benchmark',
    'original_tasks': 79, 'derived_episodes': 4,
    'dataset_split': 'dev', 'private_holdout_claim': False,
    'aggregation': 'equal_original_task_weight_episodes_separate',
    'admission': 'all_original_scores_and_episode_reviews_complete_and_verified',
    'reuse': 'explicit_completed_captures_same_inputs_recipe_and_engine_with_receipts',
    'interrupted_before_registration': 'retain_prior_attempt_and_restart_in_new_batch',
    'after_registration': 'one_attempt_per_repeat_no_automatic_retry',
    'reviewer': 'configuration_frozen_before_first_review_not_claimed_preregistered',
    'teacher_calibration_claim': False,
}

# Executes only our fixed organizer operations against the frozen release. It does not
# load candidate-generated Python or pass reviewer material to a candidate process.
WORKER = r'''
import json, sys, shutil
from pathlib import Path
from benchmark.core import read, write, require, digest
from benchmark import full_batch, full_judge, full_leaderboard
from benchmark.native import selection, batch_report
from benchmark.batch import batch_run
from benchmark.workflow_versions import same_context
p=json.load(sys.stdin); root=Path.cwd(); out=Path(p['batch']); answers=out/'answers'
action=p['action']; registry=out/'agents.frozen.json'
if action=='prepare':
    plan=full_batch.make_plan(root,registry,'dev',None,None,None,True,True,None)
    require(len(plan['originals'])==79 and len(plan['expected_episodes'])==4,'Formal public scope must be 79 originals and 4 episodes')
    old=Path(p['reuse']) if p.get('reuse') else None
    if old:
        prior=full_batch.verify(root,old)
        require(full_batch.plan_identity(prior)==full_batch.plan_identity(plan),'Reuse requires the same frozen engine, complete scope, recipes and protocol')
        require(not any(j['status']=='running' for j in full_batch.report(old,prior)['jobs']),'Stop the source batch before reusing answers')
    answers.mkdir()
    write(answers/'full-batch.json',plan)
    write(answers/'agents.frozen.json',read(registry))
    single_ids=sorted({j['id'] for j in plan['jobs'] if j['kind']=='single'})
    reg,agents,rows,protocol,resolved,context=selection(root,registry,'dev',None,single_ids)
    batch_run(rows,protocol,resolved,answers/'single')
    write(answers/'single/resolved-registry.json',resolved)
    write(answers/'single/reproduction.json',context)
    reused=[]; retained=[]
    if old:
        require(same_context(read(old/'single/reproduction.json'),context),'Single-stage capture context differs')
        for job in full_batch.report(old,prior)['jobs']:
            src=full_batch.job_path(old,job); dest=full_batch.job_path(answers,job)
            if job['status']=='completed':
                if dest.exists(): shutil.rmtree(dest) # Only the new, unattempted staging copy.
                shutil.copytree(src,dest)
                reused.append(job['path'])
            elif job['status']!='prepared':
                shutil.copytree(src,out/'prior-attempts'/job['path'])
                retained.append({'path':job['path'],'status':job['status']})
    batch_report(answers/'single',executed=False)
    full_batch.verify(root,answers)
    result={'reused':reused,'retained':retained,'plan_sha256':digest(plan),
            'comparison':{'originals':plan['originals'],'episodes':plan['expected_episodes'],
                'fingerprints':plan['context']['fingerprints'],'sources':plan['source_snapshots'],
                'protocol':plan['protocol']}}
elif action=='run':
    result=full_batch.run(root,registry,answers,execute=True,resume=True,include_staged=True,include_episodes=True)
elif action=='status':
    result=full_batch.report(answers,read(answers/'full-batch.json'))
elif action=='verify':
    result={'verified':True,'plan_sha256':digest(full_batch.verify(root,answers))}
elif action=='review':
    result=full_judge.run(root,answers,Path(p['config']),Path(p['review']),execute=p['execute'],resume=p['resume'])
elif action=='board':
    result=full_leaderboard.build(root,[Path(p['review']) if p.get('review') else answers],p['samples'],p['seed'])
else:
    raise ValueError('Unknown formal organizer operation')
print(json.dumps(result,ensure_ascii=False))
'''


def is_formal(path):
    return (Path(path) / MARKER).is_file()


def tree_hashes(directory):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'Missing or linked evidence directory')
    result = {}
    for path in sorted(directory.rglob('*')):
        require(not path.is_symlink(), 'Linked file in frozen evidence')
        if '__pycache__' not in path.parts and path.is_file() and path.suffix != '.pyc':
            result[path.relative_to(directory).as_posix()] = file_hash(path)
    return result


def snapshot(source, dest):
    """Allowlisted source/data only, never runs, credentials, HOME or user history."""
    from .reproduction import engine_files
    source = Path(source).resolve()
    names = set(engine_files(source))
    for folder in ('data/dev', 'data/staged-dev', 'data/longitudinal', 'examples/public-smoke'):
        if (source / folder).exists():
            names.update((Path(folder) / name).as_posix() for name in tree_hashes(source / folder))
    # Catalog location is versioned with the release; keep public data catalogs too.
    names.update('data/' + name for name in tree_hashes(source / 'data') if not name.startswith('test/'))
    names.add('config/native-cli.protocol.json')
    names.add('benchmark/leaderboard.html')
    names.update(name for name in ('config/benchmark.json', 'config/protocol.json', 'config/judge.example.json',
                                  'config/baselines.example.json') if (source / name).is_file())
    for name in sorted(names):
        src = source / name
        require(not src.is_symlink() and all(not p.is_symlink() for p in src.parents), 'Linked snapshot source')
        target = dest / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    return tree_hashes(dest)


def worker(batch, action, **options):
    batch = Path(batch).resolve()
    payload = dict(options, batch=str(batch), action=action)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env.pop('PYTHONPATH', None)
    proc = subprocess.Popen([sys.executable, '-B', '-c', WORKER], cwd=batch / 'source',
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        output, _ = proc.communicate(json.dumps(payload))
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        proc.communicate()
        raise
    require(proc.returncode == 0, 'Frozen evaluation operation failed; see retained diagnostics')
    try:
        return json.loads(output)
    except ValueError:
        raise ValueError('Frozen evaluation did not return a valid report') from None


def verify(batch):
    batch = Path(batch).resolve()
    manifest = read(batch / MARKER)
    require(manifest.get('schema_version') == SCHEMA and manifest.get('policy') == POLICY,
            'Unsupported formal public evaluation policy')
    require(manifest.get('manifest_sha256') == digest({k:v for k,v in manifest.items() if k!='manifest_sha256'}),
            'Formal registration changed')
    require(manifest['worker_sha256'] == digest(WORKER), 'Use the matching formal organizer version')
    require(tree_hashes(batch / 'source') == manifest['source_files'], 'Frozen release changed')
    require(read(batch / 'agents.frozen.json') == manifest['registry'], 'Formal recipes changed')
    require(digest(read(batch / 'answers/full-batch.json')) == manifest['capture_plan_sha256'],
            'Formal capture plan changed')
    for relative, files in manifest['reuse']['completed_files'].items():
        require(tree_hashes(batch / 'answers' / relative) == files, 'Reused answer/evidence changed')
    for relative, files in manifest['reuse']['prior_attempt_files'].items():
        require(tree_hashes(batch / 'prior-attempts' / relative) == files, 'Prior interrupted/failed evidence changed')
    return manifest


def prepare(root, registry_path, agent_ids, out, source_root=None, reuse=None):
    from .cli_adapters import select_agents
    root, out = Path(root).resolve(), Path(out).absolute()
    require(not out.exists() and not out.is_symlink(), 'Formal output exists; choose a new directory')
    registry = read(registry_path)
    frozen = {'schema': registry['schema'], 'agents': select_agents(registry, agent_ids),
              'repeats': registry['repeats'], 'timeout_seconds': registry['timeout_seconds']}
    require(all(a.get('cli_version') for a in frozen['agents']), 'Pin native client versions first')
    out.parent.mkdir(parents=True, exist_ok=True)
    prior = Path(reuse).resolve() if reuse else None
    reuse_lock = (file_lock(prior.parent / ('.' + prior.name + '.full.lock'), blocking=False,
                            busy_message='Stop the source batch before registration') if prior else nullcontext())
    with file_lock(out.parent / ('.' + out.name + '.formal.lock'), blocking=False, busy_message='Formal preparation already running'), reuse_lock:
        if prior:
            from .full_batch import report
            tree_hashes(prior)  # Reject symlinks before copying any capture or retained evidence.
            prior_states = report(prior, read(prior / 'full-batch.json'))['statuses']
            require(set(prior_states) <= {'prepared', 'completed', 'interrupted'},
                    'Only completed/unattempted/user-interrupted captures can be registered; do not retry failed answers')
        with tempfile.TemporaryDirectory(prefix='.formal-prepare-', dir=out.parent) as tmp:
            stage = Path(tmp) / 'batch'; stage.mkdir()
            source_files = snapshot(source_root or root, stage / 'source')
            write(stage / 'agents.frozen.json', frozen)
            result = worker(stage, 'prepare', reuse=str(Path(reuse).resolve()) if reuse else None)
            receipts = {'source_batch': str(Path(reuse).resolve()) if reuse else None,
                        'completed': result['reused'], 'prior_attempts': result['retained'],
                        'completed_files': {r: tree_hashes(stage / 'answers' / r) for r in result['reused']},
                        'prior_attempt_files': {r['path']: tree_hashes(stage / 'prior-attempts' / r['path'])
                                                for r in result['retained']}}
            manifest = {'schema_version': SCHEMA, 'official': True, 'registered_at': now(), 'policy': POLICY,
                        'comparison_sha256': digest(result['comparison']), 'registry': frozen,
                        'source_files': source_files, 'worker_sha256': digest(WORKER),
                        'capture_plan_sha256': result['plan_sha256'], 'reuse': receipts}
            manifest['manifest_sha256'] = digest(manifest)
            write(stage / MARKER, manifest)
            require(not out.exists(), 'Formal output appeared during preparation')
            stage.rename(out)
    progress = status(out)
    return {'status':'formal_prepared', 'official':True, 'batch':str(out), 'model_calls':0, 'exit_code':0,
            'execution_units':progress['planned'], 'reused_completed':progress['reused_completed'],
            'prior_attempts_retained':progress['prior_attempts_retained'],
            'remaining_units':progress['planned']-progress['completed'], 'publication_ready':False}


def status(batch):
    manifest = verify(batch)
    value = worker(batch, 'status')
    value.update(official=True, evaluation_scope=POLICY['scope'], batch=str(Path(batch).resolve()),
                 registration_sha256=manifest['manifest_sha256'],
                 reused_completed=len(manifest['reuse']['completed']),
                 prior_attempts_retained=len(manifest['reuse']['prior_attempts']),
                 note='Formal public registration; final admission requires complete verified reviews.')
    value['publication_ready'] = (all(r['admission_status']=='admitted' for r in board(batch)['rows'])
                                  if (Path(batch)/'formal-review.json').exists() else False)
    return value


def run(batch, credentials_registry):
    from .credentials import credential_environment
    batch = Path(batch).resolve()
    with file_lock(batch.parent / ('.' + batch.name + '.formal.lock'), blocking=False, busy_message='Formal batch is busy'):
        manifest = verify(batch)
        # Credentials stay outside the frozen package. Only selected names are loaded.
        with credential_environment(manifest['registry'], credentials_registry):
            result = worker(batch, 'run')
        value = status(batch)
        value['capture_status'] = result['status']
        if result.get('status') == 'setup_required':
            value['readiness'] = result
        value['exit_code'] = result.get('exit_code', 1)
        return value


def review(batch, config, out, execute=False, resume=False):
    batch, out = Path(batch).resolve(), Path(out).resolve()
    require(not out.is_relative_to(batch), 'Keep review output outside formal source/capture package')
    with file_lock(batch.parent / ('.' + batch.name + '.formal.lock'), blocking=False, busy_message='Formal batch is busy'):
        manifest = verify(batch)
        state = worker(batch, 'status')
        require(state['status'] == 'completed', 'Finish all formal tasks before freezing judge inputs')
        from .judge import configuration
        binding = {'registration_sha256': manifest['manifest_sha256'], 'review': str(out),
                   'config_sha256': digest(configuration(config))}
        pointer = batch / 'formal-review.json'
        if pointer.exists():
            require(read(pointer) == binding, 'Formal review configuration/output changed')
        result = worker(batch, 'review', config=str(Path(config).resolve()), review=str(out), execute=execute, resume=resume)
        write(pointer, binding)
    result.update(official=True, evaluation_scope=POLICY['scope'])
    if execute:
        result['leaderboard'] = export([batch], out / 'leaderboard', update=(out / 'leaderboard').exists())
    return result


def board(batch, samples=2000, seed=0):
    batch = Path(batch).resolve(); manifest = verify(batch)
    pointer = batch / 'formal-review.json'; review = None
    if pointer.exists():
        binding = read(pointer)
        require(binding['registration_sha256'] == manifest['manifest_sha256'], 'Formal reviewer registration differs')
        review = binding['review']
        header = read(Path(review) / 'full-judge.json')
        require(Path(header['source']).resolve() == batch / 'answers'
                and digest(header['config']) == binding['config_sha256'], 'Formal review source/config differs')
    value = worker(batch, 'board', review=review, samples=samples, seed=seed)
    require(value['expected_tasks']==79 and value['expected_episodes']==4, 'Formal scope changed')
    for row in value['rows']:
        aid = row['adapter']
        row.update(official=True, admission_status='admitted' if row['complete'] and row['episodes_complete'] else 'pending',
                   capture_release_sha256=digest(manifest['source_files']),
                   reused_completed=sum(p.split('/')[1]==aid for p in manifest['reuse']['completed']),
                   prior_attempts_retained=sum(p['path'].split('/')[1]==aid for p in manifest['reuse']['prior_attempts']))
    value.update(official=True, evaluation_scope=POLICY['scope'], policy=POLICY,
                 comparison_sha256=manifest['comparison_sha256'],
                 note='项目正式公开评测；79 道原题与 4 条连续任务分别报告。未完成评分不显示完整均分；不宣称私有留出测试或教师校准。')
    return value


def export(batches, out, update=False, samples=2000, seed=0, expected_repeats=None):
    from .leaderboard import publish
    require(batches and len({str(Path(b).resolve()) for b in batches})==len(batches), 'Empty or duplicate formal batches')
    with ExitStack() as locks:
        for batch in sorted({Path(b).resolve() for b in batches}):
            locks.enter_context(file_lock(batch.parent / ('.'+batch.name+'.formal.lock'), blocking=False,
                                           busy_message='Formal evaluation is running; use status and export after it stops'))
        values = [board(batch, samples, seed) for batch in batches]
    require(len({v['comparison_sha256'] for v in values})==1, 'Formal public scopes/protocols differ')
    require(len({p for v in values for p in v['reviewer_profiles']})<=1, 'Formal judge configurations differ')
    rows = [row for v in values for row in v['rows']]
    require(len({r['adapter'] for r in rows})==len(rows), 'Duplicate formal configuration; do not select the best repeated batch')
    require(expected_repeats is None or all(r['expected_repeats']==expected_repeats for r in rows), 'Formal repeat count differs')
    ready = all(r['admission_status']=='admitted' for r in rows)
    value = dict(values[0], rows=rows, publication_ready=ready,
                 reviewer_profiles=sorted({p for v in values for p in v['reviewer_profiles']}),
                 schema_version='formal-public-leaderboard.1', generated_at=now())
    out = Path(out).absolute(); out.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(out.parent / ('.'+out.name+'.leaderboard.lock'), blocking=False, busy_message='Formal export already running'):
        publish(value, out, update)
    return {'status': 'formal_results_complete' if ready else 'formal_results_pending', 'official':True,
            'publication_ready':ready, 'page':str(out/'index.html'), 'data':str(out/'leaderboard.json'),
            'configurations':len(rows), 'exit_code':0 if ready else 1}
