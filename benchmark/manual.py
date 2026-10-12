"""Official App packets and original-answer capture; no App or API automation."""
from datetime import datetime
from pathlib import Path
from .core import candidate, candidate_prompt, digest, file_hash, query_prompt, read, require, write
from .runtime_common import file_lock, regular_bytes


def prepare_app_session(out, task, baseline):
    out = Path(out)
    (out / 'workspace/app-prompt.md').write_text(candidate_prompt(task), encoding='utf-8')
    write(out / 'app-session.template.json', {
        'task_id': task['id'], 'product': baseline['agent_product'],
        'surface': None, 'app_version': None, 'selected_model_label': None, 'mode': None,
        'reasoning_setting': None, 'web_search': None, 'memory': None,
        'new_conversation': None, 'extra_user_messages': None, 'model_switch_observed': None,
        'started_at': None, 'finished_at': None, 'outcome': None,
        'evidence_note': ''})


def record_app_answer(out, answer, session_path):
    # Serialize answer imports so concurrent CLI invocations cannot overwrite.
    with file_lock(Path(out) / '.answer-import.lock'):
        return _record_app_answer(out, answer, session_path)


def _record_app_answer(out, answer, session_path):
    out = Path(out).resolve()
    meta = read(out / 'run.json')
    require(meta.get('execution_mode') == 'manual_app', 'Not an official App attempt')
    require(meta['status'] == 'prepared', 'Cannot overwrite an attempted run')
    require(session_path is not None, 'Official App imports require --session with completed App settings')
    session = read(session_path)
    require(isinstance(session, dict), 'App session must be a JSON object')
    require(session.get('task_id') == meta['task_id'], 'App session/task mismatch')
    require(session.get('product') == meta['experiment']['agent_product'], 'Wrong App product')
    require(session.get('surface') in ('desktop_app', 'web_app', 'browser', 'mobile_app'),
            'Record the actual official product surface')
    for field in ('app_version', 'selected_model_label', 'mode', 'reasoning_setting'):
        require(isinstance(session.get(field), str) and session[field].strip(), 'Record App setting: ' + field)
    require(session.get('outcome') in ('completed', 'failed', 'timeout'), 'Record App outcome')
    labels = meta['experiment'].get('accepted_ui_model_labels')
    if labels and session['outcome'] == 'completed':
        require(session['selected_model_label'].strip().lower() in {label.lower() for label in labels},
                'Select the configured model in the App; do not label another model as the requested model')
    require(session.get('web_search') in ('disabled', 'enabled', 'not_available'), 'Record web_search state')
    require(session.get('memory') in ('disabled', 'not_available', 'unknown'), 'Record memory state')
    require(type(session.get('new_conversation')) is bool, 'Record whether the conversation was fresh')
    require(type(session.get('extra_user_messages')) is int and session['extra_user_messages'] >= 0,
            'Record number of additional user prompts')
    if session['outcome'] == 'completed':
        require(session['new_conversation'], 'Use a fresh App conversation for each task')
        require(session['extra_user_messages'] == 0, 'This pilot does not allow extra user prompts or answer repair')
    require(type(session.get('model_switch_observed')) is bool, 'Record whether model switching occurred')
    times = []
    for key in ('started_at', 'finished_at'):
        require(isinstance(session.get(key), str), 'Record timezone-aware ' + key)
        value = datetime.fromisoformat(session[key].replace('Z', '+00:00'))
        require(value.utcoffset() is not None, 'Timestamp must include timezone: ' + key)
        times.append(value)
    elapsed = (times[1] - times[0]).total_seconds()
    require(elapsed >= 0, 'Finish time precedes start time')
    task, rubric, protocol = (read(out / name) for name in ('task.json', 'rubric.json', 'protocol.json'))
    require(meta['task_sha256'] == digest(task) and meta['rubric_sha256'] == digest(rubric)
            and meta['protocol_sha256'] == digest(protocol), 'Organizer snapshots changed')
    work = out / 'workspace'
    require(read(work / 'task.json') == candidate(task)
            and (work / 'query.md').read_text(encoding='utf-8') == query_prompt(task)
            and (work / 'app-prompt.md').read_text(encoding='utf-8') == candidate_prompt(task),
            'App task packet changed since preparation')
    if task['execution'].get('web_mode') == 'provided_materials_only' and session['outcome'] == 'completed':
        require(session['web_search'] != 'enabled', 'This task only permits the supplied materials')
    status = session['outcome']
    if meta.get('timeout_seconds') and elapsed > meta['timeout_seconds']:
        status = 'timeout'
    if session['model_switch_observed']:
        status = 'failed'
    data = regular_bytes(answer) if answer is not None else None
    require(status != 'completed' or (data is not None and data.decode('utf-8').strip()),
            'Completed App attempt requires the original UTF-8 answer')
    dest = work / 'answer.md'
    require(not dest.exists() and not dest.is_symlink(), 'Answer already exists')
    require(not (out / 'app-session.json').exists(), 'App session already recorded')
    settings = {k: session[k] for k in ('product', 'surface', 'app_version', 'selected_model_label', 'mode',
                                      'reasoning_setting', 'web_search', 'memory')}
    meta['prepared_experiment_sha256'] = meta.get('prepared_experiment_sha256', meta['experiment_sha256'])
    meta['experiment'] = dict(meta['experiment'], app_settings=settings)
    meta['experiment_sha256'] = digest(meta['experiment'])
    if data is not None:
        dest.write_bytes(data)  # Preserve original bytes; never polish or reformat the answer.
    write(out / 'app-session.json', session)
    observation = {'requested': meta['experiment'].get('requested_model') or meta['model'],
                   'reported': [], 'ui_label': session['selected_model_label'],
                   'status': 'operator_reported_unverified', 'model_switch_observed': session['model_switch_observed']}
    write(work / 'model-observation.json', observation)
    meta.update(status=status, started_at=session['started_at'], finished_at=session['finished_at'],
                elapsed_seconds=elapsed, timing_provenance='operator_reported',
                model_observation=observation, app_session_sha256=digest(session),
                answer_file_sha256=file_hash(dest) if data is not None else None,
                trajectory_status='missing', usage=None, usage_status='not_observed',
                trajectory_required=task['execution'].get('trajectory_required', False),
                isolation_verified=False, isolation_evidence_source='manual_app_not_sandboxed', official=False)
    write(out / 'run.json', meta)
    return meta
