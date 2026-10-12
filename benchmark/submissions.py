"""Portable answer/result packages and review orchestration with quoted evidence."""
import json
from pathlib import Path
import shutil
import secrets
import tempfile

from .archives import read_archive, safe_name, write_archive
from .core import (candidate, candidate_prompt, digest, encoded, file_hash,
                   guard_private_output, query_prompt, read, require, safe_id, write)
from .packaging import judge_package
from .reproduction import batch_context, batch_runs, verify_context
from .runner import aggregate, prepare
from .scoring import score

SUBMISSION = 'benchmark-submission.1'
RESULTS = 'benchmark-results.1'
REVIEW = 'benchmark-review-set.1'
RUN_FIELDS = ('task_id', 'task_sha256', 'rubric_sha256', 'protocol_sha256', 'split', 'status',
              'adapter', 'adapter_version', 'model', 'repeat', 'timeout_seconds', 'elapsed_seconds',
              'started_at', 'finished_at', 'execution_mode', 'baseline_kind', 'experiment',
              'experiment_sha256', 'prepared_experiment_sha256', 'answer_file_sha256', 'usage',
              'usage_status', 'model_observation', 'trajectory_status', 'trajectory_required', 'timing_provenance',
              'isolation_verified', 'isolation_evidence_source', 'review_settings_sha256')
EXPERIMENT_FIELDS = ('profile', 'comparison_unit', 'evidence_policy', 'human_intervention',
                     'attempt_policy', 'adapter_build_sha256', 'id', 'label', 'agent_product',
                     'client', 'client_version', 'model', 'requested_model', 'accepted_ui_model_labels',
                     'auth', 'reasoning', 'base_url', 'tool_policy', 'execution_mode', 'app_settings')
APP_FIELDS = ('product', 'surface', 'app_version', 'selected_model_label', 'mode',
              'reasoning_setting', 'web_search', 'memory')
TERMINAL = {'completed', 'failed', 'timeout', 'interrupted', 'superseded', 'launch_failed', 'missing_answer',
            'invalid_output', 'model_mismatch'}


def checked_run(run, task, rubric, context):
    run = Path(run)
    for name in ('run.json', 'task.json', 'rubric.json', 'protocol.json', 'scoring.md'):
        require((run / name).is_file() and not (run / name).is_symlink(), 'Missing or linked run snapshot')
    meta = read(run / 'run.json')
    require(read(run / 'task.json') == task and read(run / 'rubric.json') == rubric, 'Run task/rubric changed')
    require(read(run / 'protocol.json') == context['protocol'], 'Run protocol changed')
    require(meta['task_sha256'] == digest(task) and meta['rubric_sha256'] == digest(rubric)
            and meta['protocol_sha256'] == context['protocol_sha256'], 'Run fingerprints changed')
    require(file_hash(run / 'scoring.md') == context['scoring_policy_sha256'], 'Run scoring policy changed')
    require(meta['task_id'] == task['id'] and meta['split'] == task['split'], 'Run identity changed')
    for field in ('adapter', 'adapter_version', 'model'):
        require(isinstance(meta.get(field), str) and meta[field].strip(), 'Missing baseline identity: ' + field)
    safe_id(meta['adapter'])
    require(type(meta.get('repeat')) is int and meta['repeat'] > 0, 'Invalid repeat number')
    require(meta['status'] in TERMINAL | {'prepared'}, 'Finish or interrupt running attempts before packaging')
    work = run / 'workspace'
    require(read(work / 'task.json') == candidate(task)
            and (work / 'query.md').read_text(encoding='utf-8') == query_prompt(task), 'Candidate workspace changed')
    answer = work / 'answer.md'
    if answer.exists() or answer.is_symlink():
        require(answer.is_file() and not answer.is_symlink(), 'Invalid answer file')
        require(file_hash(answer) == meta.get('answer_file_sha256'), 'Original answer changed after capture')
        if meta['status'] == 'completed':
            require(answer.read_text(encoding='utf-8').strip(), 'Completed answer is empty')
    else:
        require(meta['status'] != 'completed' and meta.get('answer_file_sha256') is None,
                'Missing captured original answer')
    if meta.get('experiment') is not None:
        require(meta.get('experiment_sha256') == digest(meta['experiment']), 'Experiment metadata changed')
        require(set(meta['experiment']) <= set(EXPERIMENT_FIELDS), 'Unsupported experiment fields; do not export arbitrary environment data')
        if meta['experiment'].get('app_settings') is not None:
            require(set(meta['experiment']['app_settings']) <= set(APP_FIELDS), 'Unsupported App settings')
    if 'trajectory_required' in meta:
        require(type(meta['trajectory_required']) is bool
                and meta['trajectory_required'] == task['execution'].get('trajectory_required', False),
                'Run trajectory requirement differs from the task')
    return meta


def attempt_path(meta):
    return f"runs/{safe_id(meta['adapter'])}/{safe_id(meta['task_id'])}/r{meta['repeat']}"


def check_plan(plan, context, attempts):
    require(plan['protocol_sha256'] == context['protocol_sha256']
            and plan['tasks'] and set(plan['tasks']) <= set(context['task_ids'])
            and type(plan['repeats']) is int and plan['repeats'] > 0, 'Invalid submitted batch plan')
    require(all(value == context['fingerprints'][ident]['task'] for ident, value in plan['tasks'].items()),
            'Planned task fingerprints changed')
    baselines = plan['baselines']
    require(baselines and all(set(b) <= set(EXPERIMENT_FIELDS) | {'version'} for b in baselines),
            'Unsupported batch plan fields')
    ids = [safe_id(b['id']) for b in baselines]
    require(len(ids) == len(set(ids)), 'Duplicate planned baseline')
    expected = {(b, t, r) for b in ids for t in plan['tasks'] for r in range(1, plan['repeats'] + 1)}
    actual = {(a['adapter'], a['task_id'], a['repeat']) for a in attempts}
    require(len(attempts) == len(actual) and actual == expected,
            'Planned attempts are missing or were added; preserve failures and pending records')
    by_id = {b['id']: b for b in baselines}
    for attempt in attempts:
        if 'model' not in attempt:  # Archive index; full records are checked after decoding.
            continue
        baseline = by_id[attempt['adapter']]
        require(attempt['model'] == baseline['model'] and attempt['adapter_version'] == baseline['version']
                and attempt.get('timeout_seconds') == plan['timeout_seconds'], 'Attempt baseline/budget differs from plan')
        settings = {k: v for k, v in baseline.items() if k != 'version'}
        require(attempt.get('prepared_experiment_sha256', attempt.get('experiment_sha256')) == digest(settings),
                'Prepared experiment differs from plan')


def export_submission(root, batch, out, private_root=None, include_reviews=False):
    if include_reviews:
        require(verify_results(root, batch, private_root)['exit_code'] == 0,
                'Finish and review planned attempts before exporting results')
    context, rows = batch_context(root, batch, private_root)
    manifest, runs = batch_runs(batch)
    guard_private_output('test' if context['suite'] == 'test' else 'dev', out, root)
    by_id = {t['id']: (t, r) for t, r in rows}
    files, attempts = {}, []
    for run in runs:
        ident = read(run / 'run.json')['task_id']
        require(ident in by_id, 'Run outside expected suite')
        task, rubric = by_id[ident]
        meta = checked_run(run, task, rubric, context)
        path = attempt_path(meta)
        require(path not in {a['path'] for a in attempts}, 'Duplicate attempt')
        record = {key: meta[key] for key in RUN_FIELDS if key in meta}
        record['official'] = False
        files[path + '/run.json'] = encoded(record)
        files[path + '/candidate/task.json'] = encoded(candidate(task))
        files[path + '/candidate/prompt.md'] = candidate_prompt(task).encode('utf-8')
        answer = run / 'workspace/answer.md'
        if answer.exists():
            files[path + '/answer.md'] = answer.read_bytes()
        if include_reviews and meta['status'] == 'completed':
            require((run / 'judgment.json').is_file() and (run / 'score.json').is_file(),
                    'Completed attempts must be scored before exporting results')
            judgment, saved = read(run / 'judgment.json'), read(run / 'score.json')
            check_judge(context, judgment)
            require(score(task, rubric, answer.read_text(encoding='utf-8'), judgment) == saved,
                    'Saved score is not reproducible')
            files[path + '/judgment.json'] = encoded(judgment)
            files[path + '/score.json'] = encoded(saved)
            if (run / 'review-settings.json').is_file():
                files[path + '/review-settings.json'] = encoded(read(run / 'review-settings.json'))
        attempts.append({'path': path, 'task_id': ident, 'adapter': meta['adapter'], 'repeat': meta['repeat']})
    # Batch plans contain the runner's explicit public fields, never argv or credentials.
    plan = manifest['plan']
    check_plan(plan, context, [decode_json(files, a['path'] + '/run.json') for a in attempts])
    value = write_archive(out, files, dict(schema_version=RESULTS if include_reviews else SUBMISSION,
        official=False, context=context, batch_plan=plan, attempts=attempts,
        metadata_provenance='submitter_reported_not_authenticated',
        note='No environment, credentials, client logs or evaluator rubric/reference files are included.'))
    return {'archive': str(Path(out).resolve()), 'attempts': len(attempts), 'files': len(value['files']),
            'contains_reviews': include_reviews, 'official': False}


def check_judge(context, judgment):
    version = context['protocol'].get('judge_version')
    require(version is None or judgment.get('judge_version') == version,
            'Judge protocol version differs; follow docs/review-protocol.md')


def check_review_settings(settings, judgment):
    require(set(settings) <= {'reviewer', 'reviewer_type', 'judge_version', 'provider', 'model',
                             'model_version_evidence', 'reasoning_setting', 'note'}, 'Unsupported reviewer settings')
    require(settings.get('reviewer') == judgment.get('reviewer')
            and settings.get('reviewer_type') in ('human', 'llm'),
            'Record matching reviewer identity and type in review-settings.json')
    require(settings.get('judge_version') == judgment.get('judge_version'), 'Reviewer settings version differs')
    if settings['reviewer_type'] == 'llm':
        require(all(isinstance(settings.get(k), str) and settings[k].strip() for k in ('provider', 'model')),
                'LLM reviews require actual provider and model labels')
    fields = ('reviewer', 'reviewer_type', 'judge_version', 'provider', 'model',
              'model_version_evidence', 'reasoning_setting')
    return digest({key: settings.get(key) for key in fields})


def decode_json(files, name):
    require(name in files, 'Missing declared package file: ' + name)
    return json.loads(files[name].decode('utf-8'))


def import_submission(root, package, out, private_root=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    require(not out.exists(), 'Import output already exists; never overwrite captured runs')
    manifest, files = read_archive(package)
    require(manifest.get('schema_version') in (SUBMISSION, RESULTS) and manifest.get('official') is False,
            'Unsupported answer/result package')
    context = manifest['context']
    rows = verify_context(root, context, private_root)
    guard_private_output('test' if context['suite'] == 'test' else 'dev', out, root)
    by_id = {t['id']: (t, r) for t, r in rows}
    attempts = manifest['attempts']
    require(isinstance(attempts, list) and attempts, 'Empty attempt list')
    check_plan(manifest['batch_plan'], context, attempts)
    used, checked, reviewer_profiles = set(), [], set()
    for item in attempts:
        path = item['path']; safe_name(path)
        meta = decode_json(files, path + '/run.json')
        require(set(meta) <= set(RUN_FIELDS) | {'official'}, 'Unsupported submitted run fields')
        require(meta.get('official') is False and path == attempt_path(meta)
                and item['task_id'] == meta['task_id'] and item['adapter'] == meta['adapter']
                and item['repeat'] == meta['repeat'], 'Package attempt identity mismatch')
        require(meta['task_id'] in by_id, 'Package task is not in this release')
        task, rubric = by_id[meta['task_id']]
        require(meta['task_sha256'] == digest(task) and meta['rubric_sha256'] == digest(rubric)
                and meta['protocol_sha256'] == context['protocol_sha256'], 'Submitted snapshot differs')
        require(decode_json(files, path + '/candidate/task.json') == candidate(task)
                and files[path + '/candidate/prompt.md'] == candidate_prompt(task).encode('utf-8'),
                'Submitted candidate input differs from this release')
        names = {path + '/run.json', path + '/candidate/task.json', path + '/candidate/prompt.md'}
        if path + '/answer.md' in files:
            from .archives import hash_bytes
            require(hash_bytes(files[path + '/answer.md']) == meta.get('answer_file_sha256'), 'Answer capture hash differs')
            if meta['status'] == 'completed':
                files[path + '/answer.md'].decode('utf-8')
            names.add(path + '/answer.md')
        else:
            require(meta['status'] != 'completed' and meta.get('answer_file_sha256') is None, 'Missing original answer')
        if manifest['schema_version'] == RESULTS and meta['status'] == 'completed':
            judgment = decode_json(files, path + '/judgment.json')
            check_judge(context, judgment)
            # Same universal-newline text reading as the scoring CLI, while raw bytes stay intact.
            answer = files[path + '/answer.md'].decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            require(score(task, rubric, answer, judgment) == decode_json(files, path + '/score.json'),
                    'Packaged score cannot be reproduced')
            names |= {path + '/judgment.json', path + '/score.json'}
            if path + '/review-settings.json' in files:
                settings = decode_json(files, path + '/review-settings.json')
                require(meta.get('review_settings_sha256') == digest(settings), 'Packaged reviewer settings changed')
                reviewer_profiles.add(check_review_settings(settings, judgment))
                names.add(path + '/review-settings.json')
            else:
                require(context.get('source') not in ('fixed_profile', 'study_protocol', 'native_cli'), 'Missing packaged reviewer settings')
        require(not used & names, 'Duplicate package attempt')
        used |= names
        checked.append((path, task, rubric, meta))
    require(used == set(files), 'Unexpected files in answer/result package')
    check_plan(manifest['batch_plan'], context, [meta for _, _, _, meta in checked])
    require(len(reviewer_profiles) <= 1, 'Mixed packaged reviewer identities/settings')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.answer-import-', dir=out.parent) as temp:
        stage = Path(temp) / 'batch'
        for path, task, rubric, submitted in checked:
            run = stage / path
            prepare(task, rubric, run, context['protocol'])
            meta = dict(submitted, source_isolation_verified=submitted.get('isolation_verified', False),
                        isolation_verified=False, isolation_evidence_source='imported_self_report_not_reprobed',
                        submission_verification='content_hashes_not_authentication', official=False)
            write(run / 'submitted-run.json', submitted)
            write(run / 'run.json', meta)
            if meta.get('execution_mode') == 'manual_app':
                (run / 'workspace/app-prompt.md').write_text(candidate_prompt(task), encoding='utf-8')
            if path + '/answer.md' in files:
                (run / 'workspace/answer.md').write_bytes(files[path + '/answer.md'])
            for name in ('judgment.json', 'score.json', 'review-settings.json'):
                if path + '/' + name in files:
                    (run / name).write_bytes(files[path + '/' + name])
            checked_run(run, task, rubric, context)
        plan = manifest['batch_plan']
        write(stage / 'batch.json', {'official': False, 'plan': plan, 'plan_sha256': digest(plan),
              'runs': [{'path': p, 'status': m['status']} for p, _, _, m in checked]})
        write(stage / 'reproduction.json', context)
        write(stage / 'submission-origin.json', {'schema_version': manifest['schema_version'],
              'archive_sha256': file_hash(package), 'metadata_provenance': 'submitter_reported_not_authenticated'})
        require(not out.exists(), 'Import output appeared during validation')
        stage.rename(out)
    return {'batch': str(out), 'attempts': len(checked), 'imported_reviews': manifest['schema_version'] == RESULTS,
            'official': False}


def review_batch(root, batch, out, private_root=None):
    out = Path(out).resolve()
    require(not out.exists(), 'Review output already exists')
    context, rows = batch_context(root, batch, private_root)
    guard_private_output('test' if context['suite'] == 'test' else 'dev', out, root)
    batch_manifest, runs = batch_runs(batch)
    by_id = {t['id']: (t, r) for t, r in rows}
    planned, checked = [], []
    # Validate the entire input before creating any review packet.
    for run in runs:
        task, rubric = by_id[read(run / 'run.json')['task_id']]
        meta = checked_run(run, task, rubric, context)
        checked.append(meta)
        if meta['status'] == 'completed':
            planned.append((run, task, rubric, meta))
    check_plan(batch_manifest['plan'], context, checked)
    require(planned, 'No completed answers to review')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.review-packets-', dir=out.parent) as temp:
        stage = Path(temp) / 'packets'
        packets = []
        secrets.SystemRandom().shuffle(planned)
        for run, task, rubric, meta in planned:
            relative = attempt_path(meta)
            packet_path = 'packets/' + secrets.token_hex(16)
            destination = stage / packet_path
            judge_package(task, rubric, (run / 'workspace/answer.md').read_text(encoding='utf-8'), destination)
            write(destination / 'review-settings.json', {'judge_version': context['protocol'].get('judge_version'),
                  'reviewer': None, 'reviewer_type': None, 'provider': None, 'model': None,
                  'model_version_evidence': None, 'reasoning_setting': None,
                  'note': 'Record the actual human or separate LLM reviewer; unknown values remain null.'})
            packets.append({'path': relative, 'packet_path': packet_path,
                            'answer_file_sha256': meta['answer_file_sha256'], 'task_id': task['id']})
        # This organizer mapping is never part of an individual judge packet.
        write(stage / 'review-manifest.json', {'schema_version': REVIEW, 'context_sha256': digest(context),
              'packets': packets, 'blind_packet_names': True, 'official': False})
        shutil.copyfile(Path(root) / 'docs/review-protocol.md', stage / 'review-protocol.md')
        require(not out.exists(), 'Review output appeared during preparation')
        stage.rename(out)
    return {'review_directory': str(out), 'answers': len(packets), 'judge_version': context['protocol'].get('judge_version'),
            'official': False}


def verify_results(root, batch, private_root=None):
    context, rows = batch_context(root, batch, private_root)
    batch_manifest, runs = batch_runs(batch)
    by_id = {t['id']: (t, r) for t, r in rows}
    missing, unfinished, reviewer_profiles, checked = [], [], set(), []
    for run in runs:
        task, rubric = by_id[read(run / 'run.json')['task_id']]
        meta = checked_run(run, task, rubric, context)
        checked.append(meta)
        if meta['status'] == 'completed':
            if not (run / 'score.json').exists() or read(run / 'score.json').get('score') is None:
                missing.append(attempt_path(meta))
            else:
                judgment = read(run / 'judgment.json')
                check_judge(context, judgment)
                if (run / 'review-settings.json').exists():
                    settings = read(run / 'review-settings.json')
                    require(meta.get('review_settings_sha256') == digest(settings), 'Saved reviewer settings changed')
                    reviewer_profiles.add(check_review_settings(settings, judgment))
                else:
                    require(context.get('source') not in ('fixed_profile', 'study_protocol', 'native_cli'), 'Pinned results require reviewer settings')
        elif meta['status'] not in TERMINAL:
            unfinished.append(attempt_path(meta))
    check_plan(batch_manifest['plan'], context, checked)
    require(len(reviewer_profiles) <= 1, 'Mixed reviewer identities/settings; preserve separate score sets')
    report = aggregate(rows, runs, context['protocol'])
    return {'status': 'incomplete' if missing or unfinished else 'verified_replay',
            'exit_code': 1 if missing or unfinished else 0, 'official': False,
            'missing_reviews': missing, 'unfinished_attempts': unfinished,
            'verification_scope': 'saved_judgments_and_calculation_not_new_semantic_review', 'summary': report}


def apply_reviews(root, batch, reviews, out, private_root=None):
    out, reviews = Path(out).resolve(), Path(reviews).resolve()
    require(not out.exists(), 'Scored output already exists; preserve prior results')
    context, rows = batch_context(root, batch, private_root)
    manifest = read(reviews / 'review-manifest.json')
    require(manifest.get('schema_version') == REVIEW and manifest.get('context_sha256') == digest(context),
            'Review set belongs to another batch/version')
    _, runs = batch_runs(batch)
    by_id = {t['id']: (t, r) for t, r in rows}
    packets = {p['path']: p for p in manifest['packets']}
    require(len(packets) == len(manifest['packets']), 'Duplicate review packet')
    packet_paths = [p.get('packet_path', p['path']) for p in manifest['packets']]
    require(len(packet_paths) == len(set(packet_paths)), 'Duplicate judge packet location')
    judged, expected, reviewer_profiles = {}, set(), set()
    for run in runs:
        task, rubric = by_id[read(run / 'run.json')['task_id']]
        meta = checked_run(run, task, rubric, context)
        if meta['status'] != 'completed':
            continue
        path = attempt_path(meta); expected.add(path)
        require(path in packets and packets[path]['answer_file_sha256'] == meta['answer_file_sha256'],
                'Review packet answer changed')
        packet_path = packets[path].get('packet_path', path)  # Preserve legacy packet compatibility.
        safe_name(packet_path)
        folder = (reviews / packet_path).resolve()
        require(folder.is_relative_to(reviews) and not (reviews / packet_path).is_symlink(), 'Judge packet escapes review directory')
        require(all((folder / name).is_file() and not (folder / name).is_symlink()
                    for name in ('judgment.json', 'review-settings.json')), 'Missing or linked judge records')
        judgment = read(folder / 'judgment.json')
        check_judge(context, judgment)
        settings = read(folder / 'review-settings.json')
        reviewer_profiles.add(check_review_settings(settings, judgment))
        judged[path] = (judgment, score(task, rubric, (run / 'workspace/answer.md').read_text(encoding='utf-8'), judgment), settings)
    require(expected and set(packets) == expected, 'Missing or extraneous review packets')
    require(len(reviewer_profiles) == 1, 'Use one recorded reviewer profile per score set')
    # Reuse the answer-only transport path so no client logs/credentials are copied to the score set.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.review-apply-', dir=out.parent) as temp:
        package = Path(temp) / 'answers.zip'
        export_submission(root, batch, package, private_root)
        stage = Path(temp) / 'scored'
        import_submission(root, package, stage, private_root)
        for path, (judgment, result, settings) in judged.items():
            write(stage / path / 'judgment.json', judgment)
            write(stage / path / 'score.json', result)
            write(stage / path / 'review-settings.json', settings)
            meta = read(stage / path / 'run.json')
            meta['review_settings_sha256'] = digest(settings)
            write(stage / path / 'run.json', meta)
        value = verify_results(root, stage, private_root)
        write(stage / 'summary.json', value['summary'])
        write(stage / 'verification.json', value)
        require(not out.exists(), 'Scored output appeared during validation')
        stage.rename(out)
    return dict(value, batch=str(out))
