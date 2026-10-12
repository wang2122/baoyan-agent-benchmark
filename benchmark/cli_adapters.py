"""Native CLI recipes and result readers. No SDKs, shell expansion or model calls on import.

CLI setup and execution boundaries are documented in docs/native-cli.md.
The recipes request a model; CLI telemetry is not proof of immutable model weights.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import native_auth
from .cli_common import require, safe_id, write, validate_usage

CLIS = ('codex', 'claude', 'gemini', 'antigravity', 'qwen', 'kimi', 'dsh')
FIELDS = {'id', 'cli', 'binary', 'cli_version', 'model', 'reasoning_effort',
          'web_search', 'base_url', 'api_key_env', 'auth'}
REQUIRED_FLAGS = {
    'codex': ('--model', '--json', '--ephemeral', '--ignore-user-config', '--strict-config', '--dangerously-bypass-approvals-and-sandbox'),
    'claude': ('--model', '--print', '--output-format', '--permission-mode', '--no-session-persistence', '--effort'),
    'gemini': ('--model', '--prompt', '--output-format', '--approval-mode'),
    'antigravity': ('--model', '--print', '--output-format', '--effort', '--dangerously-skip-permissions',
                    '--disable-slash-commands', '--print-timeout'),
    'qwen': ('--model', '--prompt', '--output-format', '--approval-mode', '--auth-type', '--openai-base-url'),
    'kimi': ('--model', '--prompt', '--output-format'),
    'dsh': ('--json',),
}


def validate_registry(registry):
    require(set(registry) == {'schema', 'repeats', 'timeout_seconds', 'agents'}, 'Unexpected CLI registry fields')
    require(registry['schema'] == 'native-cli-batch.1', 'Unsupported CLI registry')
    for key in ('repeats', 'timeout_seconds'):
        require(type(registry[key]) is int and registry[key] > 0, 'Positive ' + key + ' required')
    agents = registry['agents']
    require(isinstance(agents, list) and agents, 'No CLI agents configured')
    for a in agents:
        require(isinstance(a, dict) and set(a) <= FIELDS, 'Unexpected agent fields; do not put credentials in the registry')
        safe_id(a.get('id'))
        require(a.get('cli') in CLIS, 'Unknown native CLI')
        for key in ('model', 'binary'):
            require(isinstance(a.get(key), str) and a[key].strip(), 'Missing agent ' + key)
        require('cli_version' in a and (a['cli_version'] is None or
                isinstance(a['cli_version'], str) and a['cli_version'].strip()),
                'Invalid pinned CLI version')
        if a['cli'] in ('codex', 'claude', 'dsh', 'antigravity'):
            require(a.get('reasoning_effort') in ('low', 'medium', 'high', 'xhigh', 'max'), 'Explicit effort required')
        else:
            require(a.get('reasoning_effort') == 'native_default', 'This adapter records native default effort')
        if a['cli'] == 'codex':
            require(a.get('web_search') in ('disabled', 'cached', 'live'), 'Explicit Codex search mode required')
        mode = native_auth.auth_mode(a)
        require(mode in ('api', 'subscription'), 'Choose auth=subscription or api explicitly')
        require(mode == 'api' or a['cli'] in ('codex', 'claude', 'gemini', 'kimi', 'antigravity'), 'Subscription unsupported by this CLI recipe')
        require(mode == 'api' or not (a.get('api_key_env') or a.get('base_url')), 'Subscription recipe must not contain API routing')
        if mode == 'api':
            require(isinstance(a.get('api_key_env'), str) and re.fullmatch(r'[A-Z_][A-Z0-9_]*', a['api_key_env']),
                    'Name an API key environment variable, never its value')
            url = urlsplit(a.get('base_url', ''))
            require(url.scheme == 'https' and url.hostname and not (url.username or url.password or url.query or url.fragment),
                    'Use an HTTPS API endpoint without embedded credentials')
        if a['cli'] == 'dsh':
            require(a['reasoning_effort'] in ('low', 'high', 'max'), 'Unsupported DeepSeek effort')
    require(len({a['id'] for a in agents}) == len(agents), 'Duplicate agent IDs')


def select_agents(registry, ids=None):
    validate_registry(registry)
    if ids is None:
        return registry['agents']
    require(ids and len(set(ids)) == len(ids), 'Empty/duplicate agent selection')
    require(set(ids) <= {a['id'] for a in registry['agents']}, 'Unknown agent selection')
    return [a for a in registry['agents'] if a['id'] in ids]


def check_agent(agent, work, *, check_network=False, credential_source=None):
    """Inspect executable/auth; explicit setup/check may also diagnose provider DNS."""
    binary = shutil.which(agent['binary'])
    report = {'id': agent['id'], 'cli': agent['cli'], 'binary': str(Path(binary).resolve()) if binary else None,
              'expected_version': agent['cli_version'], 'installed_version': None,
              'model_access': 'not_checked', 'issues': []}
    report['auth'] = native_auth.auth_mode(agent)
    report['issues'].extend(native_auth.auth_issues(agent))
    if credential_source is not None:
        report['credentials'] = credential_source
    if check_network and not native_auth.subscription_issue(agent):
        from .native_runtime import network_policy
        from .network import inspect_upstreams
        hosts, _ = network_policy(agent, 'provided_materials_only')
        report['network'] = inspect_upstreams(hosts)
        report['issues'].extend(item['issue'] for item in report['network'] if item['issue'])
    if not agent['cli_version']:
        report['issues'].append('Pin cli_version before execution')
    if not binary:
        report['issues'].append('CLI executable not found')
        return report
    from .isolation import CandidateSandbox
    try:
        with CandidateSandbox(binary, loopback_listener=agent['cli'] == 'antigravity',
                              system_user_lookup=agent['cli'] == 'codex',
                              system_trust=agent['cli'] == 'antigravity', allow_pty=agent['cli'] == 'antigravity') as box:
            (box.work / 'task.json').write_text('{}')
            (box.work / 'query.md').write_text('CLI inspection only')
            box.verify()
            version = box.run([str(Path(binary).resolve()), '--version'], timeout=30)
            report['isolation_check'] = box.report
            if version.returncode:
                report['issues'].append('CLI cannot start inside the required sandbox')
                return report
            report['installed_version'] = version.stdout.strip()
            if agent['cli_version'] and report['installed_version'] != agent['cli_version']:
                report['issues'].append('CLI version mismatch')
            help_args = ['exec', '--help'] if agent['cli'] == 'codex' else (
                ['--profile', 'headless', '--help'] if agent['cli'] == 'dsh' else ['--help'])
            help_result = box.run([str(Path(binary).resolve()), *help_args],
                                 env={'DSH_HOME': str(box.home / '.dsh')}, timeout=30)
            if help_result.returncode:
                report['issues'].append('CLI help check failed inside sandbox')
            else:
                missing = [f for f in REQUIRED_FLAGS[agent['cli']] if f not in help_result.stdout + help_result.stderr]
                if missing:
                    report['issues'].append('Missing CLI flags: ' + ', '.join(missing))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        report['issues'].append('Required sandbox unavailable or CLI inspection failed; no unsandboxed fallback')

    return report


def invocation(agent, binary, prompt, work, runtime, answer, *, box, selected_env):
    """Native tools run only after the outer sandbox has passed boundary probes."""
    require(box.report['status'] == 'passed', 'Native tools require a verified outer boundary')
    cli, model = agent['cli'], agent['model']
    mode = native_auth.auth_mode(agent)
    env = dict(selected_env, NO_COLOR='1')
    key = selected_env.get(agent.get('api_key_env'))
    issue = native_auth.subscription_issue(agent)
    require(not issue, issue)
    if cli == 'codex':
        config = {'model_provider': 'openai', 'forced_login_method': 'chatgpt' if mode == 'subscription' else 'api',
                  'model_reasoning_effort': agent['reasoning_effort'], 'web_search': agent['web_search'],
                  'approval_policy': 'never', 'features.skip_host_skill_discovery': True}
        if mode == 'api':
            env['OPENAI_API_KEY'] = key
            config['openai_base_url'] = agent['base_url']
        args = [binary, 'exec', '--ignore-user-config', '--strict-config', '--ephemeral',
                '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox', '--json',
                '--model', model, '--cd', str(work), '--output-last-message', str(answer)]
        for name, value in config.items():
            args += ['-c', name + '=' + json.dumps(value)]
        return args + ['-'], prompt, env
    if cli == 'claude':
        env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_AUTO_MEMORY='1')
        if mode == 'api':
            env.update(ANTHROPIC_API_KEY=key, ANTHROPIC_BASE_URL=agent['base_url'])
        return [binary, '--print', '--model', model, '--effort', agent['reasoning_effort'],
                '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
                '--permission-mode', 'bypassPermissions'], prompt, env
    if cli == 'gemini':
        if mode == 'api':
            env['GEMINI_API_KEY'] = key
            env['GOOGLE_GEMINI_BASE_URL'] = agent['base_url']
            (box.home / '.gemini').mkdir(exist_ok=True)
            (box.home / '.gemini/settings.json').write_text(
                '{"security":{"auth":{"selectedType":"gemini-api-key"}},"telemetry":{"enabled":false}}')
        return [binary, '--model', model, '--prompt', prompt, '--output-format', 'stream-json',
                '--approval-mode', 'yolo'], None, env
    if cli == 'antigravity':
        env['AGY_CLI_DISABLE_AUTO_UPDATE'] = 'true'
        if mode == 'api':
            require(key, 'Antigravity API mode requires an explicitly selected key')
            env.update(GEMINI_API_KEY=key, GOOGLE_GEMINI_BASE_URL=agent['base_url'])
        home = box.home / '.gemini/antigravity-cli'
        home.mkdir(parents=True, exist_ok=True)
        write(home / 'settings.json', {'modelProvider': 'gemini'} if mode == 'api' else {})
        return [binary, '--model', model, '--effort', agent['reasoning_effort'], '--print', prompt,
                '--output-format', 'stream-json', '--disable-slash-commands', '--print-timeout', '0',
                '--dangerously-skip-permissions'], None, env
    if cli == 'qwen':
        env.update(OPENAI_API_KEY=key, OPENAI_BASE_URL=agent['base_url'], OPENAI_MODEL=model)
        return [binary, '--model', model, '--auth-type', 'openai', '--openai-base-url', agent['base_url'],
                '--approval-mode', 'yolo', '--prompt', prompt, '--output-format', 'stream-json'], None, env
    if cli == 'kimi':
        env.update(KIMI_DISABLE_TELEMETRY='true', KIMI_CODE_NO_AUTO_UPDATE='true', KIMI_DISABLE_CRON='true')
        if mode == 'api':
            (box.home / '.kimi-code/config.toml').write_text(native_auth.toml_text({
                'default_model': 'benchmark', 'providers': {'benchmark': {'type': 'kimi',
                    'base_url': agent['base_url'], 'api_key_env': agent['api_key_env']}},
                'models': {'benchmark': {'provider': 'benchmark', 'model': model}}}))
        return [binary, '--model', 'benchmark', '--prompt', prompt, '--output-format', 'stream-json'], None, env
    home = box.home / '.dsh'
    home.mkdir(exist_ok=True)
    env['DSH_HOME'] = str(home)
    overlay = runtime / 'deepseek.patch.json'
    write(overlay, [
        {'id': 'agent-default-model', 'config': {'provider': 'deepseek-official', 'model': model,
                                               'reasoningEffort': agent['reasoning_effort']}},
        {'id': 'llm-deepseek', 'config': {'apiKeyEnv': agent['api_key_env'], 'baseURL': agent['base_url'],
                                       'reasoningEffort': agent['reasoning_effort']}},
    ])
    return [binary, '--profile', 'headless', '--patch', str(overlay), '--json', '-'], prompt, env


def one(items, message):
    require(len(items) == 1, message)
    return items[0]


def sum_known(values):
    return sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None


def parse_result(cli, path, codex_answer=None, *, previous_antigravity=None):
    """Extract the final response, never concatenate tool output into the answer."""
    if cli == 'codex':
        require(path.is_file() and not path.is_symlink(), 'Missing or symlink Codex events')
    # JSONL records end at LF; U+2028/U+2029 are legal inside JSON strings.
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').split('\n') if line.strip()]
    require(events and all(isinstance(e, dict) for e in events), 'Invalid CLI event stream')
    models, session, answer = set(), None, None
    primary_models = set()
    usage = {'input_tokens': None, 'output_tokens': None, 'cost_usd': None}
    if cli == 'codex':
        starts = [e for e in events if e.get('type') == 'thread.started']
        completed = [e for e in events if e.get('type') == 'turn.completed']
        require(len(starts) == len(completed) == 1 and starts[0].get('thread_id')
                and not any(e.get('type') == 'turn.failed' for e in events), 'No completed Codex turn')
        require(codex_answer and codex_answer.is_file() and not codex_answer.is_symlink(), 'Missing Codex answer')
        answer, session = codex_answer.read_text(encoding='utf-8'), starts[0]['thread_id']
        raw = completed[0].get('usage', {})
        usage.update({key: raw.get(key) if type(raw.get(key)) is int and raw[key] >= 0 else None
                      for key in ('input_tokens', 'output_tokens')})
        # Requested model is not proof of the server's immutable model version.
        models.update(e['model'] for e in events if isinstance(e.get('model'), str))
    elif cli in ('claude', 'qwen'):
        final = one([e for e in events if e.get('type') == 'result'], 'Expected one CLI result')
        require(final.get('subtype') == 'success' and final.get('is_error') is False, 'CLI reported unsuccessful result')
        answer, session = final.get('result'), final.get('session_id')
        raw = final.get('usage') or {}
        usage.update(input_tokens=raw.get('input_tokens'), output_tokens=raw.get('output_tokens'))
        # Anthropic cache creation/read tokens are separate from uncached input tokens.
        if cli == 'claude' and usage['input_tokens'] is not None:
            usage['input_tokens'] = sum_known([raw.get(k) for k in
                ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')])
        # CLI cost estimates are retained in raw events, not presented as actual subscription bills.
        for event in events:
            if event.get('type') == 'assistant':
                model = (event.get('message') or {}).get('model')
                if model:
                    models.add(model)
                    if cli == 'claude' and event.get('parent_tool_use_id') is None:
                        primary_models.add(model)
        models.update((final.get('modelUsage') or {}).keys())
    elif cli == 'gemini':
        init = one([e for e in events if e.get('type') == 'init'], 'Expected one Gemini session')
        final = one([e for e in events if e.get('type') == 'result'], 'Expected one Gemini result')
        require(final.get('status') == 'success' and not any(
            e.get('type') == 'error' and e.get('severity') == 'error' for e in events),
            'Gemini reported unsuccessful result')
        session = init.get('session_id')
        parts = []
        for event in events:
            if event.get('type') == 'tool_use':
                parts = []  # Earlier assistant text describes work, not the final response.
            elif event.get('type') == 'message' and event.get('role') == 'assistant':
                require(isinstance(event.get('content'), str), 'Invalid Gemini assistant text')
                parts.append(event['content'])
        answer = ''.join(parts)
        stats = final.get('stats') or {}
        usage.update(input_tokens=stats.get('input_tokens'), output_tokens=stats.get('output_tokens'))
        # init.model is a requested setting. Per-model stats, when present, report actual usage.
        models.update((stats.get('models') or {}).keys())
    elif cli == 'antigravity':
        init = one([e for e in events if e.get('event') == 'init'], 'Expected one Antigravity session')
        terminal = one([e for e in events if e.get('event') == 'result'], 'Expected one Antigravity result')
        require(events[0] is init and events[-1] is terminal, 'Invalid Antigravity event ordering')
        final = terminal.get('result')
        require(isinstance(final, dict) and final.get('status') == 'SUCCESS' and not final.get('error')
                and not any(e.get('event') == 'error' for e in events),
                'Antigravity reported unsuccessful result')
        prior_turns = previous_antigravity['num_turns'] if previous_antigravity else 0
        require(type(final.get('num_turns')) is int and final['num_turns'] == prior_turns + 1,
                'Antigravity capture must advance exactly one user turn')
        session = final.get('conversation_id')
        require(init.get('conversation_id') == session, 'Antigravity conversation changed')
        for event in events:
            if event.get('event') == 'step_update':
                step = event.get('step_update')
                require(isinstance(step, dict) and step.get('conversation_id') == session,
                        'Antigravity step belongs to another conversation')
        answer = final.get('response')  # Never use tool output or partial text deltas as the answer.
        raw = final.get('usage')
        require(raw is None or isinstance(raw, dict), 'Invalid Antigravity usage')
        raw = raw or {}
        usage.update(input_tokens=raw.get('input_tokens'), output_tokens=raw.get('output_tokens'))
        # init.model is a requested setting, not independently observed model identity.
        if previous_antigravity is not None:
            prior = previous_antigravity['usage']
            for key in ('input_tokens', 'output_tokens'):
                require(type(raw.get(key)) is int and type(prior.get(key)) is int
                        and raw[key] >= prior[key], 'Invalid Antigravity cumulative usage')
                usage[key] = raw[key] - prior[key]
    elif cli == 'kimi':
        tail = one([e for e in events if e.get('type') == 'session.resume_hint'], 'Missing Kimi session completion hint')
        session = tail.get('session_id')
        messages = [e for e in events if e.get('role') in ('assistant', 'tool')]
        require(messages and messages[-1].get('role') == 'assistant' and not messages[-1].get('tool_calls'),
                'Kimi ended without a final assistant message')
        answer = messages[-1].get('content')
        # The current print stream omits model identity and token usage; keep both unknown.
    else:
        init = one([e for e in events if e.get('type') == 'session'], 'Expected one DSH session')
        final = one([e for e in events if e.get('type') == 'final'], 'Expected one DSH final response')
        endings = [e for e in events if e.get('type') == 'status' and e.get('phase') == 'turn_end']
        reason = endings[-1].get('reason') if endings else None
        # Published 0.2.0-rc.2 emits the core discriminated reason object.
        kind = reason.get('kind') if isinstance(reason, dict) else reason
        require(kind == 'completed' and not any(e.get('type') == 'error' for e in events),
                'DSH turn did not complete')
        answer, session = final.get('text'), init.get('sessionId')
        steps = [e.get('usage') or {} for e in events if e.get('type') == 'status' and e.get('phase') == 'step_end']
        usage.update(input_tokens=sum_known([s.get('inputTokens') for s in steps]),
                     output_tokens=sum_known([s.get('outputTokens') for s in steps]))
    require(isinstance(answer, str) and answer.strip(), 'Empty/missing final CLI answer')
    require(isinstance(session, str) and session.strip(), 'Missing CLI session identity')
    require(all(isinstance(m, str) and m for m in models), 'Invalid reported model identity')
    parsed = {'answer': answer, 'session_id': session, 'reported_models': sorted(models),
              'usage': validate_usage(usage)}
    if cli == 'claude' and primary_models:
        # modelUsage also includes native helper calls. Preserve every model, but
        # use root assistant messages to check the requested primary model.
        parsed['primary_reported_models'] = sorted(primary_models)
        parsed['other_reported_models'] = sorted(models - primary_models)
    if cli == 'antigravity':
        validate_usage(dict(input_tokens=raw.get('input_tokens'), output_tokens=raw.get('output_tokens'), cost_usd=None))
        parsed['antigravity_state'] = {'num_turns': final['num_turns'], 'usage': raw}
    return parsed
