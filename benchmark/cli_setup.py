"""Terminal setup and local credentials; usable without the organizer repository."""
import copy
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import cli_adapters, native_auth
from .cli_common import read, require, write
from .credentials import (credential_environment, credentials_path, import_credentials,
                          read_credentials, save_credentials)

GUIDANCE = {
    'codex': ('https://developers.openai.com/codex/cli/', 'codex login（ChatGPT 登录）'),
    'claude': ('https://code.claude.com/docs/en/setup', 'claude setup-token（将输出令牌保存为 CLAUDE_CODE_OAUTH_TOKEN；不读取宿主钥匙串）'),
    'gemini': ('https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/',
               '个人 Google 登录已于 2026-06-18 停用；需新建 Antigravity 配方，旧 subscription 配方不能验证企业权益；付费 API 必须显式选择'),
    'antigravity': ('https://antigravity.google/docs/cli/install/',
                    '运行 agy 完成订阅登录；启动器仅暂存指定原生登录文件或钥匙串项到独立 HOME，不会自动转用 API'),
    'qwen': ('https://qwenlm.github.io/qwen-code-docs/en/users/quickstart/', '配置自己的 DashScope API Key'),
    'kimi': ('https://www.kimi.com/code/docs/en/kimi-code-cli/', 'kimi login（Kimi Code 订阅 OAuth）'),
    'dsh': ('https://github.com/deepseek-ai/deepseek-harness', '本配方使用 DeepSeek API Key'),
}


def setup(registry_path, out, agent_ids=None, interactive=True, frozen=False, *,
          catalog_path=None, import_path=None):
    """No inference, implicit installation, or global CLI configuration changes."""
    registry_path, out = Path(registry_path), Path(out)
    require(not out.is_symlink(), 'Refusing a symlink registry')
    require(not frozen or out.resolve() != registry_path.resolve(), 'Packet registry is immutable')
    source = read(registry_path)
    cli_adapters.validate_registry(source)
    if catalog_path is not None and not frozen:
        catalog = read(catalog_path)
        cli_adapters.validate_registry(catalog)
        # Saved recipes win. The catalog makes not-yet-configured products selectable.
        available = {a['id']: a for a in catalog['agents']}
        available.update({a['id']: a for a in source['agents']})
        source = dict(source, agents=list(available.values()))
    if interactive and agent_ids is None and len(source['agents']) > 1:
        print('选择本次使用的 Agent（只检查所选项）：')
        for i, agent in enumerate(source['agents'], 1):
            print(f"  {i}. {agent['id']} / {agent['model']}")
        selected = input('输入编号，逗号分隔；all 选择全部：').strip()
        if selected == 'all':
            agent_ids = None
        else:
            require(selected, 'No agents selected')
            try:
                numbers = [int(n.strip()) for n in selected.split(',')]
                require(all(1 <= n <= len(source['agents']) for n in numbers), 'Invalid selection')
                agent_ids = [source['agents'][n - 1]['id'] for n in numbers]
            except ValueError:
                raise ValueError('Invalid selection') from None
    agents = copy.deepcopy(cli_adapters.select_agents(source, agent_ids))
    local = dict(source, agents=agents)
    previous_by_id = {}
    if out.exists():
        previous = read(out)
        cli_adapters.validate_registry(previous)
        previous_by_id = {a['id']: a for a in previous['agents']}
        for agent in agents:
            saved = previous_by_id.get(agent['id'])
            if saved and all(saved.get(k) == v for k, v in agent.items() if k != 'binary'):
                agent['binary'] = saved['binary']
        if frozen:
            require(set(previous_by_id) <= {a['id'] for a in agents}, 'Frozen packet cannot add other agents')
        elif set(previous_by_id) - {a['id'] for a in agents}:
            require(all(previous[k] == source[k] for k in ('repeats', 'timeout_seconds')),
                    'New budgets would change other saved agents; use a separate --out registry')
    secret_path = credentials_path(out)
    secrets = read_credentials(secret_path)
    for agent in agents:
        print(f"\n{agent['id']}：模型 {agent['model']}；CLI {agent['cli']}")
        if interactive and not frozen and agent['cli'] in ('codex', 'claude', 'kimi', 'gemini', 'antigravity'):
            if agent['cli'] in ('gemini', 'antigravity'):
                print(GUIDANCE[agent['cli']][1])
            current = native_auth.auth_mode(agent)
            choice = input(f'认证方式 subscription/api [{current}]，回车沿用：').strip() or current
            require(choice in ('subscription', 'api'), 'Choose subscription or api')
            agent['auth'] = choice
            if choice == 'subscription':
                agent.pop('api_key_env', None)
                agent.pop('base_url', None)
            else:
                defaults = {'codex': ('OPENAI_API_KEY', 'https://api.openai.com/v1'),
                    'claude': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com'),
                    'kimi': ('MOONSHOT_API_KEY', 'https://api.moonshot.ai/v1'),
                    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com'),
                    'antigravity': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com')}
                name, url = defaults[agent['cli']]
                agent['api_key_env'] = input(f'凭据环境变量名 [{agent.get("api_key_env", name)}]：').strip() or agent.get('api_key_env', name)
                agent['base_url'] = input(f'API 端点 [{agent.get("base_url", url)}]：').strip() or agent.get('base_url', url)
    if import_path is not None:
        imported = import_credentials(import_path, agents)
        require(all(name not in secrets or secrets[name] == value for name, value in imported.items()),
                'Imported credentials differ from saved values; use a separate --out to preserve existing credentials')
        secrets.update(imported)
        print('已读取所选 Agent 的凭据；只在本机保存，不显示值、不验证账号权限。')
    for agent in agents:
        if interactive:
            location = input(f"CLI 路径 [{agent['binary']}]，回车沿用：").strip()
            if location:
                agent['binary'] = location
        if not shutil.which(agent['binary']):
            print('未找到 CLI。按官方说明安装后，重新运行 setup：' + GUIDANCE[agent['cli']][0])
        else:
            with tempfile.TemporaryDirectory(prefix='benchmark-setup-') as work:
                report = cli_adapters.check_agent(agent, work, check_network=True)
            version = report['installed_version']
            if version and version != agent['cli_version'] and not frozen:
                if agent['cli_version'] is None or interactive and input(
                        f"检测到版本 {version}，是否将新配方固定为此版本？[y/N]：").strip().lower() == 'y':
                    agent['cli_version'] = version
            if version and version != agent['cli_version'] and frozen:
                print(f"版本不符：答题包要求 {agent['cli_version']}；请安装匹配版本，不能改动冻结配方。")
        names = native_auth.credential_names(agent)
        name = names[0] if names else None
        if name and not os.environ.get(name) and not secrets.get(name) and interactive:
            print(f'需要 {name}，认证：{native_auth.auth_mode(agent)}。输入不会显示；本机保存到 {secret_path}，不打包回传。')
            if agent['cli'] == 'claude' and native_auth.auth_mode(agent) == 'subscription':
                print('先在终端运行官方 claude setup-token，再粘贴订阅 OAuth 令牌。')
            value = getpass.getpass('凭据（留空稍后配置）：').strip()
            if value:
                secrets[name] = value
        if not name:
            print('认证使用这台电脑自己的账号：' + GUIDANCE[agent['cli']][1])
            if interactive and agent['cli'] in ('codex', 'kimi') and input(
                    '现在打开官方登录流程？[y/N]：').strip().lower() == 'y':
                args = ['login']
                executable = shutil.which(agent['binary'])
                require(executable, 'Install CLI before login')
                subprocess.run([executable, *args], check=False)
    configured = dict(previous_by_id)
    configured.update({agent['id']: agent for agent in agents})
    local['agents'] = list(configured.values())
    cli_adapters.validate_registry(local)
    out.parent.mkdir(parents=True, exist_ok=True)
    write(out, local)
    if secrets:
        save_credentials(secret_path, secrets)
    reports = []
    selected = dict(local, agents=agents)
    with credential_environment(selected, out) as sources, tempfile.TemporaryDirectory(prefix='benchmark-setup-') as work:
        for agent in agents:
            reports.append(cli_adapters.check_agent(agent, work, check_network=True,
                                                   credential_source=sources[agent['id']]))
    status = 'ready_for_trial' if all(not r['issues'] for r in reports) else 'setup_required'
    print(f'配置已保存：{out}；状态：{status}。未调用模型，登录状态、模型权限和余额仍需实际试跑确认。')
    return {'status': status, 'registry': str(out), 'agents': reports, 'model_calls': 0,
            'official': False, 'frozen_recipe': frozen,
            'selected_agents': [a['id'] for a in agents], 'configured_agents': list(configured)}


def interactive_terminal(non_interactive):
    if non_interactive:
        return False
    require(sys.stdin.isatty(), 'setup needs a terminal; use --non-interactive for checks without prompts')
    return True
