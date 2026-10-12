"""Prepare and audit a proposed study protocol; evidence is operator-declared."""
from datetime import datetime, timezone
import json
from pathlib import Path

from .core import digest, file_hash, guard_private_output, load_tasks, read, require, safe_id, write
from .isolation import regular_bytes
from .reports import save_report

SCHEMA = 'benchmark-study-protocol.1'
GATES = ('dataset', 'calibration', 'stability', 'runtime', 'protocol_decisions')
RUNTIME_CHECKS = ('original_answer_saved', 'model_identity_recorded', 'settings_recorded',
                  'trajectory_requirement_met', 'tools_follow_task',
                  'transfer_roundtrip_verified', 'score_replay_verified')
GATE_ARTIFACTS = {'dataset': {'dataset_manifest'}, 'calibration': {'calibration'},
                  'stability': {'statistics'}, 'runtime': {'runtime_acceptance'},
                  'protocol_decisions': {'baseline_registry', 'primary_reviewer_settings', 'protocol_decision'}}


def validate_policy(protocol):
    require(isinstance(protocol, dict), 'Study protocol must be an object')
    require(protocol.get('schema_version') == SCHEMA and protocol.get('status') in ('draft', 'frozen'),
            'Unsupported study protocol')
    require(protocol.get('formal_results_enabled') is False, 'This workflow does not enable publication/official results')
    require(protocol.get('comparison_unit') == 'native_agent_product_configuration', 'Declare product configuration comparison')
    require(protocol.get('evaluation_split') in ('dev', 'test'), 'Unknown evaluation split')
    require(type(protocol.get('repeats')) is int and protocol['repeats'] > 0
            and type(protocol.get('time_limit_seconds')) is int and protocol['time_limit_seconds'] > 0,
            'Positive repeat count and wall-clock limit required')
    require(protocol.get('primary_metric') == 'mean_task_score_with_full_coverage'
            and protocol.get('failure_policy') == 'report_incomplete_without_mean', 'Preserve the declared metric/failure policy')
    require(protocol.get('token_limit') is None and protocol.get('cost_limit') is None
            and protocol.get('resource_policy') == 'time_limited_product_comparison_tokens_and_cost_reported_not_enforced',
            'This runner cannot enforce uniform token/cost limits across native products')
    require(isinstance(protocol.get('judge_version'), str) and protocol['judge_version'].strip(), 'Record judge workflow version')
    require(protocol.get('web_mode') == 'per_task_declared', 'Preserve per-task source conditions')
    require(protocol.get('attempt_policy') == 'one_attempt_per_repeat_no_automatic_retry'
            and protocol.get('human_intervention') == 'new_conversation_no_extra_prompts_no_answer_edits', 'Record attempt/intervention rules')
    statistics = protocol.get('statistics', {})
    require(statistics.get('aggregation') == 'equal_task_weight_after_equal_repeat_weight'
            and statistics.get('interval') == 'percentile_case_group_bootstrap'
            and statistics.get('confidence_level') == .95
            and type(statistics.get('samples')) is int and 2 <= statistics['samples'] <= 100000
            and type(statistics.get('seed')) is int, 'Unsupported statistics policy')


def prepare_study(root, out, registry_path=None):
    root = Path(root).resolve()
    protocol = read(root / 'config/formal-protocol.example.json')
    validate_policy(protocol)
    guard_private_output(protocol['evaluation_split'], out, root)
    registry = read(registry_path or root / 'config/baselines.example.json')
    if registry.get('schema') == 'native-cli-batch.1':
        from .cli_adapters import validate_registry
        validate_registry(registry)
        baselines = registry['agents']
    else:
        baselines = registry['baselines']
    registry.update(repeats=protocol['repeats'], timeout_seconds=protocol['time_limit_seconds'])
    readiness = {'schema_version': 'benchmark-study-readiness.1',
        'protocol_sha256': digest(protocol), 'gates': [{'id': ident, 'status': 'pending',
            'approved_by': None, 'reason': None, 'artifacts': []} for ident in GATES]}
    runtime = {'schema_version': 'benchmark-runtime-acceptance.1', 'official': False,
        'operator': None, 'provenance': 'operator_recorded_not_authenticated',
        'baselines': [{'id': baseline['id'], 'status': 'pending', 'model_observed': None,
            'client_version': None, 'evidence_references': [],
            'checks': {name: False for name in RUNTIME_CHECKS}} for baseline in baselines]}
    decision = {'schema_version': 'benchmark-protocol-decision.1', 'protocol_sha256': digest(protocol),
                'decision': 'pending', 'reviewer': None, 'rationale': None}
    save_report(out, {'protocol.draft.json': protocol, 'readiness.template.json': readiness,
        'baselines.draft.json': registry, 'runtime-acceptance.template.json': runtime,
        'protocol-decision.template.json': decision}, 'README.md',
        '# 正式实验准备材料\n\n协议保持draft；3次、900秒为候选起点，待dev试跑确认。\n\n'
        '先用baselines.draft.json预检生成本机registry，再补模型与工具条件、主评审、校准验收口径和数据指纹。'
        '填写真实证据后运行study-check，全部通过才可study-freeze；不会自动启用正式榜单。\n')
    return {'status': 'study_draft_prepared', 'directory': str(Path(out).resolve()),
            'model_calls': 0, 'official': False}


def run_study_batch(root, rows, protocol, registry, registry_file, out, execute=False, resume=False, private_root=None):
    """Bind new study batches to the workflow/config before any model execution."""
    from .batch import batch_run
    from .reproduction import make_context, verify_context
    from .workflow_versions import same_context
    validate_policy(protocol)
    split = protocol['evaluation_split']
    guard_private_output(split, out, root)
    context = make_context(root, load_tasks(root, split, private_root), protocol, split,
        source='study_protocol', engine_capture='before_execution',
        registry_file_sha256=file_hash(registry_file),
        selection_sha256=digest({'tasks': [task['id'] for task, _ in rows],
                                 'baselines': [baseline['id'] for baseline in registry['baselines']]}))
    out = Path(out).resolve()
    if resume:
        require((out / 'reproduction.json').is_file(), 'Study batch has no preparation-time version snapshot')
        saved = read(out / 'reproduction.json')
        verify_context(root, saved, private_root)
        require(same_context(saved, context), 'Study code, protocol, registry or selection changed; use a new batch')
    else:
        prepared = batch_run(rows, protocol, registry, out)
        write(out / 'reproduction.json', context)
        if not execute:
            return prepared
    require(file_hash(registry_file) == context['registry_file_sha256'], 'Study registry changed during preparation')
    return batch_run(rows, protocol, registry, out, execute, True)


def check_study(root, protocol_path, readiness_path=None):
    protocol_path = Path(protocol_path).resolve()
    protocol = read(protocol_path)
    validate_policy(protocol)
    evidence_protocol_sha256 = digest(protocol)
    if protocol['status'] == 'frozen':
        source = {key: value for key, value in protocol.items()
                  if key not in ('source_protocol_sha256', 'readiness_file_sha256', 'readiness_sha256', 'frozen_at')}
        source['status'] = 'draft'
        require(digest(source) == protocol.get('source_protocol_sha256'), 'Frozen source protocol fingerprint changed')
        evidence_protocol_sha256 = protocol['source_protocol_sha256']
    blocked = []
    for name in ('primary_reviewer_settings_sha256', 'baseline_registry_sha256', 'dataset_manifest_sha256'):
        value = protocol.get(name)
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            blocked.append(name + '_unresolved')
    for name in ('calibration_acceptance_criteria', 'evidence_cutoff'):
        if not isinstance(protocol.get(name), str) or not protocol[name].strip():
            blocked.append(name + '_unresolved')
    modes = protocol.get('required_web_modes')
    if not isinstance(modes, list) or not modes or not all(isinstance(mode, str) and mode.strip() for mode in modes):
        blocked.append('required_web_modes_unresolved')
    evidence = {}
    if readiness_path is None:
        blocked.extend('missing_' + gate + '_approval' for gate in GATES)
    else:
        readiness_path = Path(readiness_path).resolve()
        readiness = read(readiness_path)
        require(isinstance(readiness, dict), 'Readiness must be an object')
        require(readiness.get('schema_version') == 'benchmark-study-readiness.1'
                and readiness.get('protocol_sha256') == evidence_protocol_sha256, 'Readiness belongs to another protocol draft')
        if protocol['status'] == 'frozen':
            require(protocol.get('readiness_sha256') == digest(readiness), 'Frozen readiness record changed')
        gates = readiness['gates']
        require(isinstance(gates, list) and all(isinstance(gate, dict) for gate in gates) and len(gates) == len(GATES)
                and {gate.get('id') for gate in gates} == set(GATES), 'Readiness must preserve every gate exactly once')
        for gate in gates:
            ident = gate['id']
            require(gate.get('status') in ('pending', 'approved', 'rejected'), 'Unknown readiness status')
            if gate['status'] != 'approved':
                blocked.append(ident + '_not_approved')
                continue
            if not all(isinstance(gate.get(name), str) and gate[name].strip() for name in ('approved_by', 'reason')):
                blocked.append(ident + '_approval_missing_reviewer_or_reason')
            artifacts = gate.get('artifacts')
            require(isinstance(artifacts, list), 'Readiness artifacts must be a list')
            if not artifacts:
                blocked.append(ident + '_missing_artifacts')
            if not GATE_ARTIFACTS[ident] <= {artifact.get('kind') for artifact in artifacts if isinstance(artifact, dict)}:
                blocked.append(ident + '_missing_required_artifact_kinds')
            for artifact in artifacts:
                require(isinstance(artifact, dict) and set(artifact) == {'kind', 'path', 'sha256'}
                        and isinstance(artifact['kind'], str) and isinstance(artifact['path'], str), 'Invalid readiness artifact')
                path = Path(artifact['path'])
                if not path.is_absolute():
                    path = readiness_path.parent / path
                raw = regular_bytes(path)
                from .archives import hash_bytes
                require(hash_bytes(raw) == artifact['sha256'], 'Readiness artifact fingerprint changed')
                require(artifact['kind'] not in evidence, 'Duplicate readiness artifact kind')
                value = json.loads(raw.decode('utf-8'))
                require(isinstance(value, dict), 'Readiness evidence must be a JSON object')
                evidence[artifact['kind']] = {'sha256': artifact['sha256'], 'value': value}
    expected_pins = {'baseline_registry': 'baseline_registry_sha256', 'dataset_manifest': 'dataset_manifest_sha256'}
    for kind, field in expected_pins.items():
        if kind not in evidence or evidence[kind]['sha256'] != protocol.get(field):
            blocked.append(kind + '_missing_or_unpinned')
    registry = evidence.get('baseline_registry', {}).get('value', {})
    if registry.get('schema') == 'native-cli-batch.1':
        from .cli_adapters import validate_registry
        validate_registry(registry)
        baselines = registry['agents']
    else:
        baselines = registry.get('baselines', [])
    require(isinstance(baselines, list) and all(isinstance(baseline, dict) for baseline in baselines), 'Invalid evidence registry')
    expected_baselines = {safe_id(baseline['id']) for baseline in baselines}
    if (not expected_baselines or len(expected_baselines) != len(baselines)
            or registry.get('repeats') != protocol['repeats']
            or registry.get('timeout_seconds') != protocol['time_limit_seconds']):
        blocked.append('baseline_registry_empty_or_policy_mismatch')
    dataset = evidence.get('dataset_manifest', {}).get('value', {})
    task_ids = dataset.get('tasks', [])
    require(isinstance(task_ids, list) and all(isinstance(ident, str) for ident in task_ids), 'Invalid dataset task list')
    for ident in task_ids:
        safe_id(ident)
    fingerprints = dataset.get('fingerprints', {})
    require(isinstance(fingerprints, dict), 'Invalid dataset fingerprints')
    valid_fingerprints = all(isinstance(value, dict) and set(value) == {'task', 'rubric'}
        and all(isinstance(fingerprint, str) and len(fingerprint) == 64
                and all(char in '0123456789abcdef' for char in fingerprint) for fingerprint in value.values())
        for value in fingerprints.values())
    if (dataset.get('schema_version') != '1.0' or dataset.get('split') != protocol['evaluation_split']
            or not task_ids or len(task_ids) != len(set(task_ids))
            or set(fingerprints) != set(task_ids) or not valid_fingerprints):
        blocked.append('dataset_manifest_invalid_or_wrong_split')
    reviewer = evidence.get('primary_reviewer_settings', {}).get('value')
    primary_profile = None
    if reviewer is None or digest(reviewer) != protocol.get('primary_reviewer_settings_sha256'):
        blocked.append('primary_reviewer_settings_missing_or_unpinned')
    else:
        from .submissions import check_review_settings
        try:
            require(isinstance(reviewer.get('reviewer'), str) and reviewer['reviewer'].strip(), 'Missing reviewer')
            require(reviewer.get('judge_version') == protocol['judge_version'], 'Reviewer version differs')
            primary_profile = check_review_settings(reviewer, reviewer)
        except (ValueError, TypeError):
            blocked.append('primary_reviewer_settings_invalid')
    calibration = evidence.get('calibration', {}).get('value', {})
    require(isinstance(calibration.get('reviewers', []), list)
            and all(isinstance(item, dict) for item in calibration.get('reviewers', [])), 'Invalid calibration reviewer list')
    if (calibration.get('schema_version') != 'benchmark-calibration.1'
            or calibration.get('completed_answers', 0) < 1 or len(calibration.get('reviewers', [])) < 2
            or primary_profile not in {item.get('profile_sha256') for item in calibration.get('reviewers', [])}):
        blocked.append('calibration_report_missing_or_empty')
    elif calibration.get('disagreements'):
        adjudication = evidence.get('adjudication', {}).get('value', {})
        if (adjudication.get('schema_version') != 'benchmark-adjudication.1'
                or adjudication.get('comparison_sha256') != digest(calibration)
                or adjudication.get('pending') != 0
                or adjudication.get('resolved') != len(calibration['disagreements'])):
            blocked.append('calibration_disputes_not_adjudicated')
    stability = evidence.get('statistics', {}).get('value', {})
    stat_rows = stability.get('rows', [])
    require(isinstance(stat_rows, list) and all(isinstance(row, dict) for row in stat_rows), 'Invalid statistics rows')
    if (stability.get('schema_version') != 'benchmark-statistics.1' or not stat_rows
            or {row.get('adapter') for row in stat_rows} != expected_baselines
            or any(not row.get('complete') or row.get('expected_repeats') != protocol['repeats']
                   or row.get('timeout_seconds') != protocol['time_limit_seconds'] for row in stat_rows)
            or stability.get('aggregation') != protocol['statistics']['aggregation']
            or stability.get('bootstrap_samples') != protocol['statistics']['samples']
            or stability.get('seed') != protocol['statistics']['seed']
            or stability.get('reviewer_profiles') != [primary_profile]):
        blocked.append('stability_report_incomplete_or_policy_mismatch')
    runtime = evidence.get('runtime_acceptance', {}).get('value', {})
    runtime_rows = runtime.get('baselines', [])
    require(isinstance(runtime_rows, list) and all(isinstance(row, dict) and isinstance(row.get('checks', {}), dict)
            for row in runtime_rows), 'Invalid runtime acceptance rows')
    if (runtime.get('schema_version') != 'benchmark-runtime-acceptance.1'
            or not isinstance(runtime.get('operator'), str) or not runtime['operator'].strip()
            or len(runtime_rows) != len(expected_baselines)
            or {row.get('id') for row in runtime_rows} != expected_baselines
            or any(row.get('status') != 'passed' or not row.get('model_observed') or not row.get('client_version')
                   or not row.get('evidence_references')
                   or any(row.get('checks', {}).get(name) is not True for name in RUNTIME_CHECKS) for row in runtime_rows)):
        blocked.append('runtime_acceptance_incomplete')
    if registry.get('schema') == 'native-cli-batch.1':
        expected_versions = {baseline['id']: baseline['cli_version'] for baseline in baselines}
        if any(not expected_versions.get(row.get('id')) or
               row.get('client_version') != expected_versions.get(row.get('id')) for row in runtime_rows):
            blocked.append('native_runtime_cli_version_mismatch')
    decision = evidence.get('protocol_decision', {}).get('value', {})
    if (decision.get('schema_version') != 'benchmark-protocol-decision.1'
            or decision.get('protocol_sha256') != evidence_protocol_sha256 or decision.get('decision') != 'approve'
            or not all(isinstance(decision.get(name), str) and decision[name].strip() for name in ('reviewer', 'rationale'))):
        blocked.append('protocol_decision_missing_or_unapproved')
    status = ('study_incomplete' if blocked else 'frozen_protocol_checked' if protocol['status'] == 'frozen' else 'ready_for_operator_freeze')
    return {'schema_version': 'benchmark-study-check.1', 'status': status,
        'exit_code': 1 if blocked else 0, 'official': False, 'ready_to_freeze': not blocked and protocol['status'] == 'draft',
        'evidence_complete': not blocked,
        'protocol_sha256': digest(protocol), 'blocked': sorted(set(blocked)),
        'verified_artifact_hashes': {kind: item['sha256'] for kind, item in evidence.items()},
        'verification_scope': 'policy_consistency_file_integrity_and_operator_declarations_not_semantic_or_runtime_authentication'}


def freeze_study(root, protocol_path, readiness_path, out):
    protocol = read(protocol_path)
    require(isinstance(protocol, dict) and protocol.get('status') == 'draft',
            'Study is already frozen or invalid; preserve it and create a new version')
    result = check_study(root, protocol_path, readiness_path)
    require(result['ready_to_freeze'], 'Study cannot freeze: ' + ', '.join(result['blocked']))
    guard_private_output(protocol['evaluation_split'], out, root)
    readiness = read(readiness_path)
    frozen = {**protocol, 'status': 'frozen', 'source_protocol_sha256': digest(protocol),
              'readiness_file_sha256': file_hash(readiness_path), 'readiness_sha256': digest(readiness),
              'frozen_at': datetime.now(timezone.utc).isoformat()}
    save_report(out, {'protocol.frozen.json': frozen, 'readiness.json': readiness, 'freeze-check.json': result}, 'README.md',
                '# 已冻结实验协议\n\n本快照来自带证据引用的组织者审批记录。它不认证评审质量或远端运行真实性，不自动发布榜单。\n')
    return {'status': 'study_protocol_frozen', 'directory': str(Path(out).resolve()),
            'protocol_sha256': digest(frozen), 'official': False}
