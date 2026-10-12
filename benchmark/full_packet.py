"""Organizer-only full native packets and transactional return import.

Scoring sources never enter the answer ZIP. Imports only reconstruct review data;
no executable or saved session from another computer is ever run here.
"""
import base64
from pathlib import Path
import shutil
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from . import cli_adapters, full_batch, full_packet_runner as portable, longitudinal
from .batch import batch_run
from .cli_common import regular
from .core import candidate_prompt, digest, file_hash, read, require, safe_id, text_hash, write
from .episode_runner import ARTIFACT_INSTRUCTION, identity as episode_identity
from .full_packet_io import MANIFEST, extract_archive, fingerprint, validate_directory
from .native import batch_report, finish_record
from .reproduction import suite_rows
from .runtime_common import now

SCHEMA = 'native-full-organizer.1'
IMPORT = 'native-full-import.1'
MARKER = 'full-packet-import.json'
LAUNCHER = '''from pathlib import Path
from native_cli.full_packet_runner import entrypoint

if __name__ == '__main__':
    entrypoint(Path(__file__).resolve().parent)
'''


def execution_snapshot(root, selector):
    value = longitudinal.snapshot(root, selector)
    if value.get('kind') != 'staged':
        for stage in value['stages']:
            stage['prompt'] += ARTIFACT_INSTRUCTION
    return value


def candidate_units(root, plan):
    singles = {task['id']: task for task, _ in suite_rows(root, 'dev')}
    units, prompts, seen = [], {}, set()
    for job in plan['jobs']:
        key = (job['kind'], job['id'])
        if key in seen:
            continue
        seen.add(key)
        unit = {'id': job['id'], 'kind': job['kind'], 'stages': []}
        if job['kind'] == 'single':
            task = singles[job['id']]
            stages = [('S0', candidate_prompt(task), task, True)]
        else:
            snap = execution_snapshot(root, job['selector'])
            stages = [(s['id'], s['prompt'], snap['sources'][s['source']]['task'],
                       any(d.get('path') == 'answer.md' and d.get('required')
                           for d in snap['sources'][s['source']]['task'].get('deliverables', []))) for s in snap['stages']]
        for sid, prompt, task, required in stages:
            stage = {'id': sid, 'prompt_sha256': text_hash(prompt),
                     'web_mode': task.get('execution', {}).get('web_mode', 'historical_source_rules_in_prompt'),
                     'require_answer_file': required}
            unit['stages'].append(stage); prompts[portable.prompt_name(unit, stage)] = prompt
        units.append(unit)
    return units, prompts


def export_full(root, registry_path, out, suite='dev', agent_ids=None, task_ids=None,
                protocol_path=None, include_staged=True, include_episodes=True, episode_ids=None,
                selection_path=None):
    root, out = Path(root).resolve(), Path(out).absolute()
    registry = read(registry_path); agents = cli_adapters.select_agents(registry, agent_ids)
    require(suite == 'dev', 'Full cross-computer packets currently support public dev only')
    require(all(a['cli_version'] for a in agents), 'Pin selected CLI versions before exporting packets')
    require(not out.exists() and not out.is_symlink(), 'Packet output already exists')
    out.parent.mkdir(parents=True, exist_ok=True)
    result = []
    with tempfile.TemporaryDirectory(prefix='.full-packet-', dir=out.parent) as temp:
        base = Path(temp) / 'package'; base.mkdir()
        index = {'schema': SCHEMA, 'official': False, 'agents': {}}
        for a in agents:
            agent = dict(a, binary=a['cli'])  # Never expose the organizer's executable path.
            frozen = dict(registry, agents=[agent]); aid = agent['id']
            organizer = base / 'organizer' / aid
            write(organizer / 'registry.json', frozen)
            plan = full_batch.make_plan(root, organizer / 'registry.json', suite, [aid], task_ids,
                                        protocol_path, include_staged, include_episodes, episode_ids,
                                        selection_path=selection_path)
            write(organizer / 'full-batch.json', plan)
            units, prompts = candidate_units(root, plan)
            candidate = base / 'candidates' / aid
            (candidate / 'native_cli').mkdir(parents=True)
            for name in portable.RUNTIME:
                shutil.copyfile(root / 'benchmark' / name, candidate / 'native_cli' / name)
            (candidate / 'run_agent.py').write_text(LAUNCHER, encoding='utf-8')
            write(candidate / 'registry.json', frozen)
            for name, prompt in prompts.items():
                path = candidate / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(prompt, encoding='utf-8')
            runtime = ['run_agent.py', *(f'native_cli/{name}' for name in portable.RUNTIME)]
            packet = {'schema': portable.VERSION, 'registry_sha256': digest(frozen), 'units': units,
                      'runtime_sha256': {name: file_hash(candidate / name) for name in runtime}}
            packet['packet_id'] = digest(packet); write(candidate / 'packet.json', packet)
            count = sum(len(u['stages']) for u in units)
            (candidate / 'README.md').write_text(
                '# 完整独立答题包\n\n'
                f'配置 `{aid}`，模型 `{agent["model"]}`，CLI `{agent["cli_version"]}`。'
                f'{len(units)} 个执行单元、{count} 个阶段，每项 {registry["repeats"]} 次。\n\n'
                '需要 Python 3.11+、macOS 和指定版本的原生 CLI。使用自己的账号在本机登录；'
                'setup 只能配置本机可执行路径和凭据，不能更改已冻结的模型、版本、预算或认证方式。\n\n'
                '```sh\npython3 run_agent.py setup\npython3 run_agent.py check\n'
                'python3 run_agent.py\npython3 run_agent.py --out answers --execute\n```\n\n'
                '默认只预览，--execute 才消耗额度。由脚本在沙箱中启动 Agent；不要让 Agent 在整个题包目录做题。'
                '未来题面由组织者脚本持有，沙箱每次只接收本轮题面；同一题恢复原生会话，不同题新建会话。'
                '本包不含评分标准、参考清单或其他模型答卷。\n\n'
                '正常暂停可使用 --max-stages N；然后在同一电脑执行：\n\n'
                '```sh\npython3 run_agent.py --out answers --execute --resume\n```\n\n'
                '已失败、超时、中断或结果不确定的尝试不会自动重跑；会保留并继续尚未尝试的任务。'
                '同一题中途不能迁移电脑后继续会话。\n\n'
                '结束后只交回输出的 `answers-*.zip`。只重新打包、不调用模型：\n\n'
                '```sh\npython3 run_agent.py export --out answers\n```\n\n'
                '不回传 .local/、登录文件或个人 HOME。回传包包含原始回答、事件、诊断日志以及白名单内的'
                '本题会话检查点和交付文件，仅供组织者内部复核。每文件最多 128 MiB、总计最多 2 GiB；'
                '超过限制会明确拒绝，不删减证据。第三方运行记录是提交者证据，不等于评审电脑独立验证过隔离。\n',
                encoding='utf-8')
            portable.load_packet(candidate)
            names = ['README.md', 'packet.json', 'registry.json', *runtime, *prompts]
            with ZipFile(base / 'candidates' / (aid + '.zip'), 'x', ZIP_DEFLATED) as archive:
                for name in names:
                    archive.write(regular(candidate, name), name)
            index['agents'][aid] = {'packet_id': packet['packet_id'], 'plan_sha256': digest(plan),
                                    'registry_sha256': digest(frozen)}
            result.append({'agent': aid, 'candidate_zip': str(out / 'candidates' / (aid + '.zip')),
                           'units': len(units), 'stages': count, 'repeats': registry['repeats'],
                           'original_tasks': sum(u['kind'] != 'episode' for u in units),
                           'derived_episodes': sum(u['kind'] == 'episode' for u in units)})
        index['sha256'] = digest(index); write(base / 'full-package.json', index)
        (base / 'README.md').write_text(
            '# 组织者保留目录\n\n只发送 candidates/ 内对应配置的 ZIP；organizer/ 和本目录留在评审电脑。'
            '保留打包时的代码与题库版本，回传导入会检查运行和评分组件、题目及预算是否匹配。\n\n'
            '可以直接导入原始回传 ZIP，无需手动解压，也不会执行回传包内的文件：\n\n'
            '```sh\npython3 -m benchmark cli-import --package <本目录> --results <answers-xxxx.zip> --out <新的收卷目录>\n'
            'python3 -m benchmark judge-batch --batch-dir <收卷目录> --config <你的Judge配置> --out <评审目录>\n'
            '```\n\n第二条默认只准备评审；--execute 才调用 Judge。单阶段、两阶段统一进入原题统计，'
            '连续任务单列。导入只供评审，不在评审电脑恢复对方的会话，不自动认证为正式结果。\n', encoding='utf-8')
        require(not out.exists(), 'Packet destination appeared during export'); base.rename(out)
    return {'out': str(out), 'packets': result, 'executed': False, 'official': False, 'exit_code': 0}


def copy_capture(source, dest, names, prefix, mapping, *, single=False):
    locations = {'run.json': 'cli_evidence/run.json', 'prompt.md': 'workspace/query.md',
                 'answer.md': 'workspace/answer.md', 'final-response.md': 'workspace/final-response.md',
                 'events.jsonl': 'workspace/trajectory.jsonl', 'stderr.log': 'cli.stderr.log',
                 'execution.json': 'execution.json', 'trace.jsonl': 'trace.jsonl'}
    for name in names:
        target = dest / (locations[name] if single else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            require(target.read_bytes() == regular(source, name).read_bytes(), 'Prepared candidate prompt differs')
        else:
            shutil.copyfile(regular(source, name), target)
        mapping[prefix + '/' + name] = target


def import_multi(root, folder, destination, job, record, plan, batch_created, mapping, final_folder):
    snap = execution_snapshot(root, job['selector']); agent = plan['agents'][0]
    contract = episode_identity(root, agent, plan['timeout_seconds'], snap)
    captures = record['captures']; completed = [m for m in captures if m['status'] == 'completed']
    conversation = completed[0]['session_id'] if completed else 'not_started_imported_' + job['id']
    state = {'schema_version': longitudinal.SCHEMA, 'snapshot': snap, 'snapshot_sha256': digest(snap),
             'candidate_dir': str(final_folder / 'visible'), 'product': agent['id'], 'model': agent['model'],
             'conversation_id': conversation, 'created_at': batch_created, 'released': len(captures), 'records': [],
             'released_at': captures[-1]['started_at'], 'official': False, 'isolation_verified': False,
             'host_configuration_verified': False, 'session_evidence': 'submitter_reported_unverified',
             'execution_backend': 'native_cli', 'execution_contract_sha256': digest(contract),
             'aggregation': contract['aggregation'], 'review_only': True}
    execution = {'contract': contract, 'contract_sha256': digest(contract), 'attempts': []}
    if record['status'] == 'budget_exhausted':
        execution['terminal_status'] = 'budget_exhausted'
    for meta in captures:
        sid = meta['stage_id']; src = folder / 'stages' / sid; dest = destination / 'stages' / sid
        prefix = record['path'] + '/stages/' + sid
        copy_capture(src, dest, ['run.json', *meta['files']], prefix, mapping)
        row = {'stage_id': sid, 'status': meta['status'], 'started_at': meta['started_at'],
               'finished_at': meta['finished_at'], 'elapsed_seconds': meta['elapsed_seconds'],
               'native_session_id': meta.get('session_id'), 'error': meta.get('error'),
               'files': {n: h for n, h in meta['files'].items() if n != 'prompt.md'}}
        execution['attempts'].append(row)
        if meta['status'] == 'completed':
            receipt = {'stage_id': sid, 'conversation_id': conversation, 'product': agent['id'], 'model': agent['model'],
                       'same_conversation': True, 'answer_unedited': True, 'extra_user_messages': 0,
                       'started_at': meta['started_at'], 'finished_at': meta['finished_at'],
                       'native_session_id': meta['session_id'], 'execution_evidence_sha256': meta['files']['execution.json'],
                       'provenance': 'submitter_reported_unverified'}
            data = base64.b64encode((dest / 'answer.md').read_bytes()).decode('ascii')
            state['records'].append({'stage_id': sid, 'answer_base64': data, 'answer_sha256': digest(data),
                                    'receipt': receipt, 'receipt_sha256': digest(receipt), 'recorded_at': now()})
    write(destination / 'run.json', state); write(destination / 'native-episode.json', execution)


def verify_import(directory):
    """Revalidate imported evidence on every review; never upgrade submitter provenance."""
    directory = Path(directory); marker = read(regular(directory, MARKER))
    require(marker.get('schema') == IMPORT and marker.get('review_only') is True
            and marker.get('sha256') == digest({k: v for k, v in marker.items() if k != 'sha256'}), 'Import receipt changed')
    require(marker['plan_sha256'] == digest(read(directory / 'full-batch.json')), 'Imported plan changed')
    require(marker['transport']['packet_id'] == marker['packet_id']
            and set(marker['transport']['files']) == set(marker['transport_paths']), 'Import transport membership changed')
    for original, mapped in marker['transport_paths'].items():
        require(fingerprint(regular(directory, mapped)) == marker['transport']['files'][original],
                'Imported original capture changed')
    for name, expected in marker['files'].items():
        require(file_hash(regular(directory, name)) == expected, 'Imported review record changed')
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob('*')
              if p.is_file() and p.name != '.DS_Store'}
    require(actual == set(marker['files']) | {MARKER}, 'Imported batch membership changed')
    return marker


def import_full(root, package, results, out):
    root, package, results, out = Path(root).resolve(), Path(package).resolve(), Path(results), Path(out).absolute()
    require(not out.exists() and not out.is_symlink(), 'Import destination already exists')
    index = read(regular(package, 'full-package.json'))
    require(index.get('schema') == SCHEMA and index.get('sha256') ==
            digest({k: v for k, v in index.items() if k != 'sha256'}), 'Organizer package changed')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.full-import-', dir=out.parent) as temp:
        temp = Path(temp)
        if results.is_file():
            source = temp / 'returned'; extract_archive(results, source)
        else:
            source = results
        transport = validate_directory(source)
        returned_registry = read(regular(source, 'registry.json')); cli_adapters.validate_registry(returned_registry)
        require(len(returned_registry['agents']) == 1, 'Expected one returned configuration')
        aid = safe_id(returned_registry['agents'][0]['id'])
        require(aid in index['agents'], 'Returned agent is not in the organizer package')
        manifest, frozen = portable.load_packet(package / 'candidates' / aid)
        require(transport['packet_id'] == manifest['packet_id'] == index['agents'][aid]['packet_id']
                and returned_registry == frozen, 'Returned packet/recipe differs from organizer copy')
        records, names = portable.inspect_results(source, manifest, frozen)
        require(set(transport['files']) == set(names) | {'summary.json'}, 'Unexpected returned contents')
        require(read(regular(source, 'summary.json')) == portable.summary(records, manifest, frozen), 'Returned summary changed')
        plan = read(regular(package, f'organizer/{aid}/full-batch.json'))
        require(digest(plan) == index['agents'][aid]['plan_sha256'] and digest(frozen) == index['agents'][aid]['registry_sha256']
                and read(regular(package, f'organizer/{aid}/registry.json')) == frozen, 'Organizer plan/recipe changed')
        require(plan['agents'] == frozen['agents'] and plan['repeats'] == frozen['repeats']
                and plan['timeout_seconds'] == frozen['timeout_seconds'], 'Organizer plan budget differs')
        units, prompts = candidate_units(root, plan)
        require(units == manifest['units'], 'Current question sources differ from packet')
        for name, prompt in prompts.items():
            require((package / 'candidates' / aid / name).read_text(encoding='utf-8') == prompt,
                    'Organizer stage prompt changed')
        stage = temp / 'batch'; stage.mkdir()
        write(stage / 'full-batch.json', plan); write(stage / 'agents.frozen.json', frozen)
        single_ids = {j['id'] for j in plan['jobs'] if j['kind'] == 'single'}
        singles = [(t, r) for t, r in suite_rows(root, 'dev') if t['id'] in single_ids]
        if singles:
            batch_run(singles, plan['protocol'], plan['resolved_registry'], stage / 'single')
            write(stage / 'single/reproduction.json', plan['context'])
            write(stage / 'single/resolved-registry.json', plan['resolved_registry'])
            write(stage / 'single/native-import.json', {'packet_id': manifest['packet_id'], 'review_only': True,
                                                      'metadata_provenance': 'submitter_reported_unverified', 'official': False})
        captured = {(r['kind'], r['unit_id'], r['repeat']): r for r in records}; mapping = {}
        require(set(captured) == {(j['kind'], j['id'], j['repeat']) for j in plan['jobs']}, 'Returned job scope changed')
        batch_created = read(source / 'batch.json')['created_at']; portable.timestamp(batch_created)
        for job in plan['jobs']:
            row = captured[job['kind'], job['id'], job['repeat']]
            if row['status'] == 'not_run':
                continue
            folder = source / row['path']; dest = full_batch.job_path(stage, job)
            require(all(portable.timestamp(m['started_at']) >= portable.timestamp(batch_created) for m in row['captures']),
                    'Stage predates answer batch')
            if job['kind'] == 'single':
                original = row['captures'][0]; src = folder / 'stages/S0'
                copy_capture(src, dest, ['run.json', *original['files']], row['path'] + '/stages/S0', mapping, single=True)
                meta = read(dest / 'run.json')
                fields = ('status', 'elapsed_seconds', 'exit_code', 'session_id', 'reported_models', 'usage', 'error',
                          'model_identity_status', 'auth_mode', 'isolation', 'started_at', 'finished_at', 'answer_capture')
                meta.update({k: original[k] for k in fields if k in original})
                meta.update(cli=frozen['agents'][0]['cli'], cli_version=frozen['agents'][0]['cli_version'],
                            packet_id=manifest['packet_id'], trajectory_status='recorded_unverified',
                            usage_status='cli_reported_unverified',
                            answer_file_sha256=file_hash(dest / 'workspace/answer.md') if (dest / 'workspace/answer.md').exists() else None)
                if meta.get('usage') is not None:
                    write(dest / 'workspace/usage.json', meta['usage'])
                write(dest / 'run.json', meta)
                finish_record(dest, plan['resolved_registry']['baselines'][0], read(dest / 'task.json'), imported=True)
            else:
                import_multi(root, folder, dest, job, row, plan, batch_created, mapping, out / job['path'])
        if singles:
            batch_report(stage / 'single', executed=False)
        for name in ('packet.json', 'registry.json', 'batch.json', 'summary.json'):
            target = stage / 'transfer' / name; target.parent.mkdir(exist_ok=True)
            shutil.copyfile(regular(source, name), target); mapping[name] = target
        require(set(mapping) == set(transport['files']), 'Not all submitted evidence was preserved')
        # Check all original plan/snapshot/score bindings using the existing review path.
        full_batch.verify(root, stage)
        marker = {'schema': IMPORT, 'review_only': True, 'official': False, 'packet_id': manifest['packet_id'],
                  'plan_sha256': digest(plan), 'metadata_provenance': 'submitter_reported_unverified',
                  'transport': transport, 'transport_paths': {n: p.relative_to(stage).as_posix() for n, p in mapping.items()},
                  'files': {p.relative_to(stage).as_posix(): file_hash(p) for p in stage.rglob('*') if p.is_file()}}
        marker['sha256'] = digest(marker); write(stage / MARKER, marker)
        verify_import(stage); require(not out.exists(), 'Import destination appeared during validation'); stage.rename(out)
    result = full_batch.report(out, plan)
    return dict(result, packet_id=manifest['packet_id'], review_only=True, isolation_verified=False,
                metadata_provenance='submitter_reported_unverified')
