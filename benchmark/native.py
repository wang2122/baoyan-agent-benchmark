"""Native CLI entry points backed by the release's batch/review/statistics contract."""
from collections import Counter
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile

from . import cli_adapters, cli_batch, cli_packet, cli_packet_runner, cli_setup, native_auth
from .batch import batch_run
from .cli_common import file_lock, now, regular, result_exit_code
from .core import digest, file_hash, guard_private_output, read, require, write
from .reproduction import batch_runs, engine_files, make_context, suite_rows, verify_context
from .submissions import check_plan, checked_run
from .workflow_versions import runtime_identity, same_context

PROFILE = 'native-cli-products-v1'


def tool_policy(agent):
    """Describe the requested recipe; never certify the actual host or tool use."""
    return {'files': 'native_tools_inside_outer_sandbox',
            'web': agent.get('web_search', 'native_tools_under_task_network_policy'),
            'personal_settings': 'fresh_home_selected_credentials_no_host_history',
            'extra_skills': [], 'extra_mcp_servers': [],
            'isolation_backend': 'macos_seatbelt', 'unsupported_backend': 'refuse_execution',
            'auth': native_auth.auth_mode(agent),
            'tools_verified': False, 'host_configuration_verified': False,
            'file_read_isolation_verified': False}


def normalized_registry(root, registry, agents, protocol):
    build = runtime_identity(root, 'native_cli')
    return {'profile': PROFILE, 'comparison_unit': 'native_agent_product_configuration',
        'evidence_policy': protocol['web_mode'],
        'human_intervention': 'new_conversation_no_extra_prompts_no_answer_edits',
        'attempt_policy': 'one_attempt_per_repeat_no_automatic_retry',
        'adapter_build_sha256': build, 'repeats': registry['repeats'],
        'timeout_seconds': registry['timeout_seconds'],
        'baselines': [{'id': a['id'], 'label': a['id'], 'kind': 'agent',
            'agent_product': a['cli'], 'client': a['cli'], 'client_version': a['cli_version'],
            'model': a['model'], 'requested_model': a['model'],
            'auth': native_auth.auth_mode(a),
            'reasoning': a['reasoning_effort'], 'base_url': a.get('base_url'),
            'tool_policy': tool_policy(a), 'execution_mode': 'native_cli',
            'version': digest({'build': build, 'agent': {k: v for k, v in a.items() if k != 'binary'}})}
            for a in agents]}


def selection(root, registry_path, suite, agent_ids=None, task_ids=None, protocol_path=None, private_root=None):
    root = Path(root).resolve()
    registry = read(registry_path)
    agents = cli_adapters.select_agents(registry, agent_ids)
    all_rows = suite_rows(root, suite, private_root)
    rows = all_rows
    if task_ids is not None:
        require(task_ids and len(task_ids) == len(set(task_ids)), 'Empty/duplicate task selection')
        require(set(task_ids) <= {t['id'] for t, _ in rows}, 'Unknown task selection')
        rows = [(t, r) for t, r in rows if t['id'] in task_ids]
    require(rows, 'No tasks selected')
    protocol = read(protocol_path or root / 'config/native-cli.protocol.json')
    if protocol_path is None:
        # Custom preview budgets are captured in the context, without modifying the source recipe.
        protocol = dict(protocol, repeats=registry['repeats'], time_limit_seconds=registry['timeout_seconds'])
    require(protocol.get('formal_results_enabled') is False and
            protocol.get('repeats') == registry['repeats'] and
            protocol.get('time_limit_seconds') == registry['timeout_seconds'],
            'Native registry repeat/time policy differs from protocol')
    require(protocol.get('comparison_unit') == 'native_agent_product_configuration'
            and protocol.get('web_mode') == 'per_task_declared'
            and protocol.get('primary_metric') == 'mean_task_score_with_full_coverage'
            and protocol.get('failure_policy') == 'report_incomplete_without_mean',
            'Preserve native product comparison, source conditions and complete-coverage metric')
    require(protocol.get('token_limit') is None and protocol.get('cost_limit') is None,
            'Native CLI runner cannot enforce token or monetary limits')
    require(isinstance(protocol.get('judge_version'), str) and protocol['judge_version'].strip(),
            'Record a judge workflow version')
    if protocol.get('schema_version') == 'benchmark-study-protocol.1':
        from .study import validate_policy
        from .core import storage
        validate_policy(protocol)
        require(suite == protocol['evaluation_split'], 'Study split differs from native suite')
        require(protocol.get('required_web_modes') and all(t['execution']['web_mode']
                in protocol['required_web_modes'] for t, _ in rows), 'Study source modes are unresolved')
        for expected, actual in (
            (protocol.get('baseline_registry_sha256'), file_hash(registry_path)),
            (protocol.get('dataset_manifest_sha256'), file_hash(storage(root, suite, private_root) / 'manifest.json'))):
            require(expected is None or expected == actual, 'Pinned native study registry/dataset differs')
    else:
        require(protocol.get('status') == 'frozen_preview', 'Use a frozen preview or explicit study protocol')
    resolved = normalized_registry(root, registry, agents, protocol)
    context = make_context(root, all_rows, protocol, suite, source='native_cli',
        engine_capture='before_execution', registry_file_sha256=file_hash(registry_path),
        recipe_sha256=digest({'agents': [{k: v for k, v in a.items() if k != 'binary'} for a in agents],
                              'repeats': registry['repeats'], 'timeout_seconds': registry['timeout_seconds']}),
        selection_sha256=digest({'tasks': [t['id'] for t, _ in rows], 'agents': [a['id'] for a in agents]}),
        resolved_registry_sha256=digest(resolved))
    return registry, agents, rows, protocol, resolved, context


@contextmanager
def batch_lock(out):
    out = Path(out).resolve()
    with file_lock(out.parent / ('.' + out.name + '.lock'), blocking=False,
                   busy_message='Another process owns this native batch'):
        yield


def finish_record(run, baseline, task, imported=False):
    meta = read(run / 'run.json')
    observed = meta.get('reported_models', [])
    primary = meta.get('primary_reported_models', observed)
    meta.update(adapter_version=baseline['version'],
        model_observation={'requested': baseline['model'], 'reported_models': observed,
            'primary_reported_models': primary,
            'other_reported_models': meta.get('other_reported_models', []),
            'status': 'cli_reported_match' if primary == [baseline['model']] else
                      'cli_reported_mismatch' if primary else 'not_reported',
            'provenance': 'cli_reported_not_authenticated'},
        trajectory_required=task['execution'].get('trajectory_required', False),
        timing_provenance=('submitter_reported_monotonic_clock_unverified' if imported else 'runner_monotonic_clock')
                          if meta.get('elapsed_seconds') is not None else 'not_observed',
        isolation_verified=False if imported else meta.get('isolation_verified', False), host_configuration_verified=False,
        isolation_evidence_source='submitter_report_unverified' if imported else 'local_boundary_probes', official=False)
    if not imported:
        meta['finished_at'] = now()
    write(run / 'run.json', meta)
    return meta


def inspect_run(run, task, rubric, context):
    meta = checked_run(run, task, rubric, context)
    if meta['status'] == 'prepared':
        require(not any((run / 'workspace' / name).exists() or (run / 'workspace' / name).is_symlink()
                        for name in ('answer.md', 'final-response.md', 'trajectory.jsonl', 'usage.json')),
                'Prepared attempt already contains capture files; preserve them before continuing')
    else:
        require(meta.get('artifact_sha256'), 'Native capture evidence is missing; imported batches are review-only')
        for name, expected in meta['artifact_sha256'].items():
            require(file_hash(regular(run, name)) == expected, 'Native capture artifact changed')
        if meta['status'] == 'completed':
            from .native_runtime import parse_capture
            parsed = parse_capture(meta['cli'], run / 'workspace/trajectory.jsonl', run / 'workspace/answer.md',
                                   read(run / 'execution.json'))
            require(parsed.pop('answer') == (run / 'workspace/answer.md').read_text(encoding='utf-8')
                    and all(meta.get(k) == v for k, v in parsed.items()), 'Native answer/event metadata changed')
    return meta


def batch_report(out, executed=True, stopped=None):
    manifest, runs = batch_runs(out)
    metas = [read(run / 'run.json') for run in runs]
    for row, meta in zip(manifest['runs'], metas):
        row['status'] = meta['status']
    write(Path(out) / 'batch.json', manifest)
    counts = dict(Counter(meta['status'] for meta in metas))
    completed = counts.get('completed', 0)
    report = {'batch': str(Path(out).resolve()), 'executed': executed, 'official': False,
              'planned': len(runs), 'completed': completed, 'statuses': counts,
              'stopped': stopped, 'status': 'interrupted' if counts.get('interrupted') else
                                           'completed' if completed == len(runs) else 'incomplete'}
    report['exit_code'] = 130 if counts.get('interrupted') else result_exit_code(report)
    return report


def run_native(root, registry_path, out, suite='dev', agent_ids=None, task_ids=None,
               protocol_path=None, execute=False, resume=False, private_root=None, continue_on_error=False):
    registry, agents, rows, protocol, resolved, context = selection(
        root, registry_path, suite, agent_ids, task_ids, protocol_path, private_root)
    guard_private_output('test' if suite == 'test' else 'dev', out, root)
    out = Path(out).resolve()
    if not execute:
        return {'status': 'preview_no_model_called', 'out': str(out), 'official': False, 'exit_code': 0,
                'agents': [a['id'] for a in agents], 'tasks': [t['id'] for t, _ in rows],
                'attempts': len(agents) * len(rows) * registry['repeats'],
                'repeats': registry['repeats'], 'timeout_seconds': registry['timeout_seconds']}
    require(os.name == 'posix', 'Use macOS/Linux, or WSL on Windows')
    by_task = {t['id']: (t, r) for t, r in rows}
    by_agent = {a['id']: a for a in agents}
    by_baseline = {b['id']: b for b in resolved['baselines']}
    with batch_lock(out):
        if resume:
            require(not (out / 'submission-origin.json').exists() and not (out / 'native-import.json').exists(),
                    'Imported answer batches are review-only')
            saved = read(out / 'reproduction.json')
            verify_context(root, saved, private_root)
            require(same_context(saved, context) and read(out / 'resolved-registry.json') == resolved,
                    'Native source, registry, protocol or selection changed; use the original release or a new batch')
            manifest, existing = batch_runs(out)
            records = [inspect_run(run, *by_task[read(run / 'run.json')['task_id']], context) for run in existing]
            check_plan(manifest['plan'], context, records)
            sessions = [m.get('session_id') for m in records if m.get('session_id')]
            require(len(sessions) == len(set(sessions)), 'Session reused between native questions')
        else:
            require(not out.exists(), 'Native output already exists; preserve it or use --resume')
            sessions = []
        with cli_setup.credential_environment(dict(registry, agents=agents), registry_path) as sources:
            checks = cli_batch.inspect_agents(agents, credential_sources=sources)
            if checks['status'] != 'ready_for_trial':
                return dict(checks, exit_code=1)
            binaries = {row['id']: row['binary'] for row in checks['agents']}
            if not resume:
                batch_run(rows, protocol, resolved, out)
                write(out / 'resolved-registry.json', resolved)
                write(out / 'reproduction.json', context)
                write(out / 'readiness.json', checks)
            for run in batch_runs(out)[1]:
                meta = read(run / 'run.json')
                if meta['status'] != 'prepared':
                    continue
                a = by_agent[meta['adapter']]
                task, rubric = by_task[meta['task_id']]
                print(f"Running {a['id']} / {task['id']} / r{meta['repeat']}", file=sys.stderr, flush=True)
                cli_batch.attempt(task, rubric, protocol, a, binaries[a['id']], meta['repeat'],
                                  registry['timeout_seconds'], run, prepared=True)
                meta = finish_record(run, by_baseline[a['id']], task)
                print(f"{a['id']} / {task['id']}: {meta['status']} ({meta.get('elapsed_seconds', 0):.1f}s)",
                      file=sys.stderr, flush=True)
                if meta.get('session_id') in sessions:
                    meta.update(status='invalid_output', error='Session reused between questions',
                                duplicate_session_id=meta['session_id'], session_id=None)
                    write(run / 'run.json', meta)
                elif meta.get('session_id'):
                    sessions.append(meta['session_id'])
                report = batch_report(out, stopped=str(run.relative_to(out)) if meta['status'] != 'completed' else None)
                if meta['status'] != 'completed' and (not continue_on_error or meta['status'] == 'interrupted'):
                    return report
            return batch_report(out)


def export_native(root, registry_path, out, suite='dev', agent_ids=None, task_ids=None,
                  protocol_path=None, private_root=None):
    registry, agents, rows, protocol, resolved, context = selection(
        root, registry_path, suite, agent_ids, task_ids, protocol_path, private_root)
    require(all(a['cli_version'] for a in agents), 'Pin CLI versions with setup before exporting packets')
    out = Path(out).resolve()
    guard_private_output('test' if suite == 'test' else 'dev', out, root)
    require(not out.exists(), 'Native packet output already exists')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-export-', dir=out.parent) as temp:
        stage = Path(temp) / 'package'
        result = cli_packet.export_packets(root, stage, registry_path, [a['id'] for a in agents],
            rows=rows, protocol=protocol)
        write(stage / 'native-context.json', {'context': context, 'resolved_registry': resolved})
        require(not out.exists(), 'Native output appeared during export')
        stage.rename(out)
    for packet in result['packets']:
        packet['candidate_zip'] = str(out / 'candidates' / (packet['agent'] + '.zip'))
    return dict(result, out=str(out), suite=suite, exit_code=0)


def import_native(root, package, results, out, private_root=None):
    package, results, out = Path(package).resolve(), Path(results).resolve(), Path(out).resolve()
    value = read(regular(package, 'native-context.json'))
    context, resolved = value['context'], value['resolved_registry']
    all_rows = verify_context(root, context, private_root)
    require(digest(resolved) == context['resolved_registry_sha256'], 'Native organizer registry changed')
    require(not out.exists(), 'Import output already exists')
    guard_private_output('test' if context['suite'] == 'test' else 'dev', out, root)
    registry = read(regular(results, 'registry.json'))
    cli_adapters.validate_registry(registry)
    require(len(registry['agents']) == 1, 'Expected one native agent per packet')
    agent = registry['agents'][0]
    candidate = package / 'candidates' / agent['id']
    packet, _ = cli_packet_runner.load_packet(candidate)
    baseline = [b for b in resolved['baselines'] if b['id'] == agent['id']]
    require(len(baseline) == 1 and normalized_registry(root, registry, [agent], context['protocol'])['baselines'] == baseline,
            'Packet recipe differs from preparation-time native settings')
    require(registry['repeats'] == resolved['repeats'] and registry['timeout_seconds'] == resolved['timeout_seconds'],
            'Returned native repeat/time policy changed')
    selected = {t['id'] for t in packet['tasks']}
    require(selected <= {t['id'] for t, _ in all_rows}, 'Packet includes tasks outside the pinned suite')
    rows = [(t, r) for t, r in all_rows if t['id'] in selected]
    subset = dict(resolved, baselines=baseline)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-import-', dir=out.parent) as temp:
        captured = Path(temp) / 'captured'
        cli_packet.import_packet(package, results, captured)  # Validate all evidence before preparing output.
        stage = Path(temp) / 'batch'
        batch_run(rows, context['protocol'], subset, stage)
        write(stage / 'resolved-registry.json', subset)
        write(stage / 'reproduction.json', context)
        for run in batch_runs(stage)[1]:
            meta = read(run / 'run.json')
            returned = captured / 'runs' / agent['id'] / meta['task_id'] / f"r{meta['repeat']}"
            if not returned.exists():
                continue
            source = read(returned / 'run.json')
            for name in ('answer.md', 'trajectory.jsonl', 'usage.json'):
                if (returned / 'workspace' / name).is_file():
                    shutil.copyfile(returned / 'workspace' / name, run / 'workspace' / name)
            shutil.copytree(returned / 'cli_evidence', run / 'cli_evidence')
            for field in ('status', 'elapsed_seconds', 'exit_code', 'session_id', 'reported_models', 'usage', 'error',
                          'model_identity_status', 'answer_file_sha256', 'trajectory_status', 'usage_status',
                          'cli', 'cli_version', 'packet_id', 'auth_mode', 'isolation'):
                if field in source:
                    meta[field] = source[field]
            original_meta = read(run / 'cli_evidence/run.json')
            for field in ('started_at', 'finished_at'):
                if field in original_meta:
                    meta[field] = original_meta[field]
            write(run / 'run.json', meta)
            finish_record(run, baseline[0], read(run / 'task.json'), imported=True)
        write(stage / 'native-import.json', {'packet_id': packet['packet_id'], 'official': False,
            'metadata_provenance': 'cli_reported_not_authenticated', 'review_only': True})
        report = batch_report(stage, executed=False)
        require(not out.exists(), 'Import output appeared during validation')
        stage.rename(out)
        # batch_run's relative run paths survive the transactional directory move.
    return dict(report, batch=str(out), packet_id=packet['packet_id'])
