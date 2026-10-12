"""Organizer export/import for native CLI answer packets. Never sends rubrics."""
import shutil
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from . import cli_adapters, cli_packet_runner as portable
from .core import candidate_prompt, digest, file_hash, load_tasks, read, require, text_hash, write
from .cli_common import regular, safe_id
from .runner import prepare
from .packaging import judge_package

LAUNCHER = '''from pathlib import Path
from native_cli.cli_packet_runner import entrypoint

if __name__ == '__main__':
    entrypoint(Path(__file__).resolve().parent)
'''


def export_packets(root, out, registry_path, agent_ids=None, task_ids=None, rows=None, protocol=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    require(not out.exists(), 'Package destination already exists')
    registry = read(registry_path)
    agents = cli_adapters.select_agents(registry, agent_ids)
    require(all(a['cli_version'] for a in agents), 'Pin CLI versions with setup before exporting packets')
    rows = load_tasks(root, 'dev') if rows is None else rows
    if task_ids is not None:
        require(task_ids and len(task_ids) == len(set(task_ids)), 'Empty/duplicate task selection')
        require(set(task_ids) <= {t['id'] for t, _ in rows}, 'Unknown task selection')
        rows = [(t, r) for t, r in rows if t['id'] in task_ids]
    require(rows, 'No tasks selected')
    protocol = read(root / 'config/protocol.json') if protocol is None else protocol
    frozen = {}
    for task, rubric in rows:
        prepare(task, rubric, out / 'organizer' / task['id'], protocol)
        frozen[task['id']] = {'task_sha256': digest(task), 'rubric_sha256': digest(rubric),
                              'protocol_sha256': digest(protocol)}
    packets = []
    for selected in agents:
        # The organizer's executable path and credentials never cross to the answer computer.
        agent = dict(selected, binary=selected['cli'])
        config = dict(registry, agents=[agent])
        candidate = out / 'candidates' / agent['id']
        (candidate / 'native_cli').mkdir(parents=True)
        (candidate / 'questions').mkdir()
        for name in portable.RUNTIME:
            shutil.copyfile(root / 'benchmark' / name, candidate / 'native_cli' / name)
        (candidate / 'run_agent.py').write_text(LAUNCHER, encoding='utf-8')
        write(candidate / 'registry.json', config)
        tasks = []
        for task, _ in rows:
            question = candidate / 'questions' / (task['id'] + '.md')
            question.write_text(candidate_prompt(task), encoding='utf-8')
            tasks.append({'id': task['id'], 'prompt_sha256': file_hash(question),
                          'web_mode': task['execution']['web_mode']})
        runtime_files = ['run_agent.py', *(f'native_cli/{n}' for n in portable.RUNTIME)]
        manifest = {'schema': portable.VERSION, 'registry_sha256': digest(config), 'tasks': tasks,
                    'runtime_sha256': {n: file_hash(candidate / n) for n in runtime_files}}
        manifest['packet_id'] = digest(manifest)
        write(candidate / 'packet.json', manifest)
        (candidate / 'README.md').write_text(
            '# 独立答题包\n\n'
            f'本包只用于 `{agent["id"]}`，模型 `{agent["model"]}`，CLI `{agent["cli_version"]}`；'
            f'{len(tasks)} 题 × {config["repeats"]} 次。题面、模型、版本和思考配置已冻结。\n\n'
            '将本 ZIP 解压到不含评分标准、参考答案或其他模型答卷的答题电脑。'
            '不要把组织者项目、其他 Agent 的结果或整个导出目录一并带过去。'
            '每个 Agent 使用独立的干净环境；一台电脑重复使用时须先移走前一模型答卷及相关历史。\n\n'
            '需要 Python 3.11+ 和 macOS Seatbelt；其他系统暂不提供执行后端，不会退回无隔离运行。在解压目录打开终端：\n\n'
            '```sh\npython3 run_agent.py setup\npython3 run_agent.py check\n'
            'python3 run_agent.py\npython3 run_agent.py --out answers --execute\n```\n\n'
            '`setup` 检查 CLI 路径和版本，提示官方安装方式和认证步骤。密钥不回显，'
            '只存本机 `.local/`，不写入冻结配方，不包含在回传包。订阅账号在本机自行登录。'
            'CLI 缺失时先按提示安装，再运行 setup；不会自动安装软件或付费调用。'
            '每题只传选定认证，使用干净 HOME，不复制个人历史、Skills 或 MCP；Kimi 订阅通过官方 kimi login 配置。\n\n'
            '默认只预览，只有 `--execute` 消耗模型额度。每题使用新进程、新会话和临时工作目录；'
            '失败立即停止。`--resume` 只继续未尝试题，已失败题不重跑，答卷不覆盖。\n\n'
            '```sh\npython3 run_agent.py --out answers --execute --resume\n```\n\n'
            '结束后只交回脚本打印的 `answers-*.zip`，不要回传 `.local/` 或登录文件。'
            '评分由组织者导入后单独进行。回传包包含原始事件和诊断日志，只供内部复核。\n\n'
            '本包与本机入口共用操作系统级隔离，探针通过后才启动原生工具。'
            'execution.json 记录认证方式、文件/网络边界和清理状态，trace.jsonl 保留原生事件索引。'
            '联网题只允许经代理访问公网 HTTPS，给定材料题只允许选定服务；服务端检索及历史来源仍需审阅轨迹。'
            '回传的隔离记录是提交者证据，导入时不自动标记为本机验证或正式评测。\n', encoding='utf-8')
        # Only enumerated transport files. Never rglob the organizer or local setup state.
        names = ['README.md', 'packet.json', 'registry.json', *runtime_files,
                 *(f"questions/{t['id']}.md" for t in tasks)]
        archive = out / 'candidates' / (agent['id'] + '.zip')
        with ZipFile(archive, 'x', ZIP_DEFLATED) as z:
            for name in names:
                z.write(regular(candidate, name), name)
        packets.append({'agent': agent['id'], 'packet_id': manifest['packet_id'],
                        'candidate_zip': str(archive), 'tasks': len(tasks)})
    write(out / 'organizer.json', {'schema': portable.VERSION, 'tasks': frozen,
                                 'packets': {p['agent']: p['packet_id'] for p in packets}})
    (out / 'README.md').write_text(
        '# 组织者保留目录\n\n只发送 candidates/ 中相应 Agent 的 ZIP。'
        'organizer/ 和 organizer.json 含评审快照，留在评分电脑。'
        '每个 Agent 单独运行、回传；不要发送整个导出目录。\n\n'
        '回传答案 ZIP 解压后，在原项目执行：\n\n'
        '```sh\npython3 -m benchmark cli-import --package <本目录> '
        '--results <回传ZIP解压目录> --out <新的收卷目录>\n```\n\n'
        '导入会验证题面、配方、会话和答卷哈希，生成已有 score / aggregate 可用的运行目录及独立评审包。\n\n'
        '## 可以发送的答题 ZIP\n\n' + '\n'.join(
            f"- [{p['agent']}](candidates/{p['agent']}.zip)：{p['tasks']} 题" for p in packets) + '\n',
        encoding='utf-8')
    return {'out': str(out), 'packets': packets, 'executed': False, 'official': False}


def import_packet(package, results, out):
    package, results, out = Path(package).resolve(), Path(results).resolve(), Path(out).resolve()
    require(not out.exists(), 'Import destination already exists')
    registry = read(regular(results, 'registry.json'))
    cli_adapters.validate_registry(registry)
    require(len(registry['agents']) == 1, 'Expected one agent per returned packet')
    agent = registry['agents'][0]
    safe_id(agent['id'])
    candidate = package / 'candidates' / agent['id']
    regular(package, f"candidates/{agent['id']}/packet.json")
    manifest, frozen_registry = portable.load_packet(candidate)
    require(registry == frozen_registry and read(regular(results, 'packet.json')) == manifest,
            'Returned packet or recipe differs from organizer copy')
    batch = read(regular(results, 'batch.json'))
    require(batch['packet_id'] == manifest['packet_id'] and batch['runner_version'] == portable.VERSION,
            'Returned batch identity mismatch')
    records = portable.inspect_results(results, manifest, registry)
    snapshot_index = read(regular(package, 'organizer.json'))
    require(snapshot_index['packets'].get(agent['id']) == manifest['packet_id'], 'Organizer packet identity changed')
    snapshots = {}
    for item in manifest['tasks']:
        sid = item['id']
        values = {name: read(regular(package, f'organizer/{sid}/{name}.json'))
                  for name in ('task', 'rubric', 'protocol')}
        require(all(digest(v) == snapshot_index['tasks'][sid][name + '_sha256'] for name, v in values.items()),
                'Organizer snapshot changed')
        require(values['task']['id'] == sid and values['task']['rubric_sha256'] == digest(values['rubric'])
                and text_hash(candidate_prompt(values['task'])) == item['prompt_sha256'], 'Organizer question mismatch')
        snapshots[sid] = values
    require(any(r['status'] != 'not_run' for r in records), 'No attempted answers to import')
    paths = []
    for row in records:
        if row['status'] == 'not_run':
            continue
        values = snapshots[row['task_id']]
        task, rubric, protocol = (values[n] for n in ('task', 'rubric', 'protocol'))
        folder = results / row['path']
        returned = read(folder / 'run.json')
        run = prepare(task, rubric, out / 'runs' / agent['id'] / row['task_id'] / f"r{row['repeat']}", protocol)
        for name, target in [('answer.md', 'answer.md'), ('events.jsonl', 'trajectory.jsonl')]:
            if name in returned['files']:
                shutil.copyfile(folder / name, run / 'workspace' / target)
        evidence = run / 'cli_evidence'
        evidence.mkdir()
        for name in ('run.json', *returned['files']):
            shutil.copyfile(folder / name, evidence / name)
        meta = read(run / 'run.json')
        meta.update(adapter=agent['id'], adapter_version=digest({'registry': registry,
                    'runtime': manifest['runtime_sha256']}), baseline_kind='agent',
                    model=agent['model'], repeat=row['repeat'], timeout_seconds=registry['timeout_seconds'],
                    cli=agent['cli'], cli_version=agent['cli_version'], execution_config=agent,
                    packet_id=manifest['packet_id'], answer_file_sha256=returned['files'].get('answer.md'),
                    trajectory_status='recorded_unverified' if 'events.jsonl' in returned['files'] else 'missing',
                    usage_status='cli_reported_unverified', official=False, isolation_verified=False,
                    host_configuration_verified=False)
        for key in ('status', 'elapsed_seconds', 'exit_code', 'session_id', 'reported_models',
                    'model_identity_status', 'usage', 'error', 'auth_mode', 'isolation'):
            meta[key] = returned.get(key)
        write(run / 'run.json', meta)
        if meta['usage']:
            write(run / 'workspace/usage.json', meta['usage'])
        if row['status'] == 'completed':
            judge_package(task, rubric, (run / 'workspace/answer.md').read_text(encoding='utf-8'),
                          out / 'judges' / agent['id'] / row['task_id'] / f"r{row['repeat']}")
        paths.append(str(run))
    result = {'packet_id': manifest['packet_id'], 'agent': agent['id'], 'imported': len(paths),
              'completed': sum(r['status'] == 'completed' for r in records), 'records': records,
              'official': False, 'scored': False, 'run_dirs': paths}
    write(out / 'import.json', result)
    write(out / 'run-dirs.json', paths)
    return result
