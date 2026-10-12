"""Eight pilot baselines with a fail-closed macOS candidate sandbox."""
import argparse
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from .core import candidate_prompt, digest, file_hash, read, require, write
from .isolation import CandidateSandbox, regular_bytes, supported
from .native_auth import GEMINI_PERSONAL_RETIRED

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'agent-products-v5-seatbelt'
# Never forward unrelated provider keys into another provider's process.
ENV_NAMES = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'LANG', 'LC_ALL',
             'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS', 'HTTPS_PROXY',
             'HTTP_PROXY', 'NO_PROXY', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR')


def endpoint(b, env):
    value = env.get(b.get('base_url_env', ''), b.get('base_url'))
    require(bool(value), 'Missing endpoint: ' + b.get('base_url_env', b['id']))
    url = urlsplit(value)
    require(url.scheme == 'https' and url.hostname and not url.username and
            not url.password and not url.query and not url.fragment and '{' not in value,
            'Endpoint must be a concrete HTTPS URL without credentials/query')
    return value.rstrip('/')


def child_env(b, env):
    clean = {k: env[k] for k in ENV_NAMES if k in env}
    if b['client'] == 'claude':
        clean.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                     CLAUDE_CODE_DISABLE_AUTO_MEMORY='1')
        if b['auth'] == 'subscription' and env.get('CLAUDE_CODE_OAUTH_TOKEN'):
            clean['CLAUDE_CODE_OAUTH_TOKEN'] = env['CLAUDE_CODE_OAUTH_TOKEN']
        if b['auth'] == 'api':
            require(bool(env.get(b['api_key_env'])), 'Missing ' + b['api_key_env'])
            clean[b['auth_variable']] = env[b['api_key_env']]
            clean['ANTHROPIC_BASE_URL'] = endpoint(b, env)
            for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                         'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
                         'ANTHROPIC_DEFAULT_FABLE_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL'):
                clean[name] = b['model']
    if b['client'] == 'gemini' and b['auth'] == 'api':
        require(bool(env.get(b['api_key_env'])), 'Missing ' + b['api_key_env'])
        clean['GEMINI_API_KEY'] = env[b['api_key_env']]
    if b['client'] == 'kimi':
        require(bool(env.get(b['api_key_env'])), 'Missing ' + b['api_key_env'])
        clean.update(KIMI_MODEL_NAME=b['model'], KIMI_MODEL_API_KEY=env[b['api_key_env']],
                     KIMI_MODEL_BASE_URL=endpoint(b, env), KIMI_MODEL_PROVIDER_TYPE='kimi',
                     KIMI_MODEL_MAX_CONTEXT_SIZE='1048576', KIMI_DISABLE_TELEMETRY='1',
                     KIMI_CODE_NO_AUTO_UPDATE='1', KIMI_DISABLE_CRON='1')
    return clean


def inspect_client(client, env):
    executable = env.get(client.upper() + '_CLI') or shutil.which(client)
    require(bool(executable) and Path(executable).is_file(), 'Missing official ' + client + ' CLI')
    path = Path(executable).resolve()
    if client == 'gemini':
        # `gemini` can be an unrelated wrapper (e.g. Hermes); require npm package provenance.
        official = False
        for parent in [path.parent, *path.parents]:
            package = parent / 'package.json'
            if package.is_file():
                try:
                    official = read(package).get('name') == '@google/gemini-cli'
                except (ValueError, OSError):
                    pass
                if official:
                    break
        require(official, 'gemini is not a verified @google/gemini-cli installation; set GEMINI_CLI')
    result = subprocess.run([str(path), '--version'], capture_output=True, text=True,
                            timeout=15, env={k: env[k] for k in ENV_NAMES if k in env})
    output = result.stdout.strip()
    require(result.returncode == 0, client + ' --version failed')
    require(client in ('gemini', 'kimi') or ('codex-cli' if client == 'codex' else 'Claude Code') in output,
            'Unexpected ' + client + ' CLI identity')
    if client == 'kimi':
        help_result = subprocess.run([str(path), '--help'], capture_output=True, text=True, timeout=15)
        require(help_result.returncode == 0 and 'kimi-code' in help_result.stdout
                and '--output-format' in help_result.stdout, 'Expected official Kimi Code CLI')
    match = re.search(r'\b\d+\.\d+\.\d+\b', output)
    require(match is not None, 'Cannot parse ' + client + ' version')
    return str(path), match.group()


def preflight(registry, env):
    report, resolved = [], json.loads(json.dumps(registry))
    cache = {}
    for b in resolved['baselines']:
        if b.get('execution_mode') == 'manual_app':
            require(b.get('command') is None, 'Official App entries cannot have a CLI command')
            report.append(dict(id=b['id'], model=b['model'], client_version=None,
                               local_checks='manual_app', blockers=[], model_access='operator_check_required',
                               authentication='login_in_official_app',
                               note='No API key or CLI needed. Prepare the task, answer in the App, then record-answer with --session.'))
            continue
        problems = []
        if not supported():
            problems.append('macOS sandbox-exec unavailable; no unsandboxed fallback')
        client = b['client']
        if client not in cache:
            try:
                cache[client] = inspect_client(client, env)
            except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                cache[client] = str(error) if not isinstance(error, subprocess.TimeoutExpired) else client + ' version check timed out'
        info = cache[client]
        if isinstance(info, str):
            problems.append(info)
        else:
            b['executable'], b['client_version'] = info
            b['version'] = f'{VERSION}/{client}@{info[1]}'
            minimum = b.get('minimum_client_version')
            if minimum and tuple(map(int, info[1].split('.'))) < tuple(map(int, minimum.split('.'))):
                problems.append('Requires ' + client + ' >= ' + minimum)
        if b['auth'] == 'api':
            if not env.get(b['api_key_env']):
                problems.append('Missing ' + b['api_key_env'])
            try:
                b['base_url'] = endpoint(b, env)
                b.pop('base_url_env', None)  # freeze region/workspace in the local registry
            except ValueError as error:
                problems.append(str(error))
        if b['auth'] == 'subscription':
            host_home = Path(env.get('HOME', str(Path.home())))
            if client == 'claude' and not env.get('CLAUDE_CODE_OAUTH_TOKEN'):
                problems.append('Isolated login requires CLAUDE_CODE_OAUTH_TOKEN from claude setup-token')
            if client == 'codex' and not (Path(env.get('CODEX_HOME', str(host_home / '.codex'))) / 'auth.json').is_file():
                problems.append('Missing official Codex file login')
            if client == 'gemini':
                problems.append(GEMINI_PERSONAL_RETIRED)
        report.append(dict(id=b['id'], model=b['model'], client_version=b.get('client_version'),
                           local_checks='blocked' if problems else 'passed', blockers=problems,
                           model_access='not_probed', authentication='not_verified'))
    return report, resolved


def command(b, env, answer, prompt=None):
    exe, model = b['executable'], b['model']
    if b['client'] == 'codex':
        return [exe, 'exec', '--model', model, '--json', '--ephemeral', '--ignore-user-config',
                '--ignore-rules', '--sandbox', 'workspace-write', '--skip-git-repo-check',
                '-c', 'web_search="disabled"', '--disable', 'apps', '--disable', 'plugins',
                '--disable', 'remote_plugin', '--disable', 'hooks', '--disable', 'memories',
                '--disable', 'skill_search', '--disable', 'skill_mcp_dependency_install',
                '--disable', 'unbounded_connection_retries', '--output-last-message', str(answer), '-']
    if b['client'] == 'claude':
        return [exe, '-p', '--model', model, '--output-format', 'stream-json', '--verbose',
                '--no-session-persistence', '--setting-sources', '',
                '--settings', '{"disableAllHooks":true,"autoMemoryEnabled":false}',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--disable-slash-commands', '--no-chrome', '--permission-mode', 'dontAsk',
                '--tools', 'Read,Glob,Grep', '--allowedTools', 'Read,Glob,Grep']
    if b['client'] == 'kimi':
        return [exe, '--prompt', prompt or 'Read task.json and query.md and complete the task.',
                '--output-format', 'stream-json', '--skills-dir', str(answer.parent / 'empty-skills')]
    # Headless reads piped input. Never use auto model routing or a fallback model.
    return [exe, '--model', model, '--prompt', 'Answer the task provided on stdin.', '--output-format', 'json']


def parse_output(client, text):
    if client == 'kimi':
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        require(events and all(isinstance(e, dict) for e in events), 'Invalid Kimi Code JSONL')
        require(not any(e.get('error') or e.get('type') in ('error', 'turn.failed') for e in events),
                'Kimi Code reported failure')
        messages = [e for e in events if e.get('role') in ('assistant', 'tool')]
        require(messages and messages[-1].get('role') == 'assistant'
                and not messages[-1].get('tool_calls') and isinstance(messages[-1].get('content'), str),
                'Missing final Kimi Code assistant message')
        # Kimi print JSONL omits model/usage; success also requires process exit 0 in launch.
        return messages[-1]['content'], events, [], dict(input_tokens=None, output_tokens=None, cost_usd=None)
    if client == 'codex':
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
        require(events and all(isinstance(e, dict) for e in events), 'Invalid Codex JSONL')
        require(not any(e.get('type') in ('error', 'turn.failed') for e in events), 'Codex reported failure')
        require(any(e.get('type') == 'turn.completed' for e in events), 'Missing completed Codex turn')
        usage = next((e.get('usage', {}) for e in reversed(events) if e.get('type') == 'turn.completed'), {})
        return None, events, [], {'input_tokens': usage.get('input_tokens'),
                                 'output_tokens': usage.get('output_tokens'), 'cost_usd': None}
    if client == 'claude':
        # Retain every emitted tool/message event, not just the final result object.
        # Legacy JSON results remain readable for saved diagnostics and integrations.
        try:
            parsed = json.loads(text)
            events = [parsed]
        except json.JSONDecodeError:
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
        require(events and all(isinstance(e, dict) for e in events), 'Invalid Claude Code JSON/JSONL')
        results = [e for e in events if e.get('type') == 'result']
        require(len(results) == 1 and events[-1] is results[0], 'Missing final Claude Code result')
        require(not any(e.get('type') == 'error' or e.get('is_error') for e in events), 'Claude Code reported failure')
        value = results[0]
        require(value.get('type') == 'result' and value.get('subtype') == 'success'
                and not value.get('is_error'), 'Claude Code reported incomplete/failed run')
        usage = value.get('usage') or {}
        # CLI cost estimates are not the cash cost of a subscription or a third-party API.
        return value.get('result'), events, list(value.get('modelUsage') or {}), {
            'input_tokens': (sum(usage.get(k, 0) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
                             if isinstance(usage.get('input_tokens'), int) else None),
            'output_tokens': usage.get('output_tokens'), 'cost_usd': None}
    value = json.loads(text)
    require(isinstance(value, dict), 'Expected JSON result object')
    require(not value.get('error'), 'Gemini CLI reported failure')
    models = (value.get('stats') or {}).get('models') or {}
    return value.get('response'), [value], list(models), {
        'input_tokens': None, 'output_tokens': None, 'cost_usd': None}


def provider_hosts(b, env):
    if b['auth'] == 'api':
        return [urlsplit(endpoint(b, env)).hostname]
    return {'codex': ['chatgpt.com', 'api.openai.com', 'auth.openai.com'],
            'claude': ['api.anthropic.com', 'claude.ai'],
            'gemini': ['cloudcode-pa.googleapis.com', 'oauth2.googleapis.com',
                       'accounts.google.com', 'generativelanguage.googleapis.com']}[b['client']]


def stage_tls(box, b, env):
    # Use existing trust material explicitly: platform Keychain IPC is intentionally unavailable.
    source = ((env.get('CODEX_CA_CERTIFICATE') if b['client'] == 'codex' else env.get('NODE_EXTRA_CA_CERTS'))
              or env.get('SSL_CERT_FILE') or '/private/etc/ssl/cert.pem')
    data = regular_bytes(Path(source), limit=4 * 1024 * 1024)
    require(b'PRIVATE KEY' not in data, 'CA bundle must contain public certificates only')
    ssl.create_default_context(cadata=data.decode('ascii'))  # fail closed on malformed/empty certificates
    target = box.home / 'ca-bundle.pem'
    target.write_bytes(data)
    target.chmod(0o400)
    return {'SSL_CERT_FILE': str(target),
            'CODEX_CA_CERTIFICATE' if b['client'] == 'codex' else 'NODE_EXTRA_CA_CERTS': str(target)}


def stage_auth(box, b, env):
    require(not (b['client'] == 'gemini' and b['auth'] == 'subscription'), GEMINI_PERSONAL_RETIRED)
    if b['client'] == 'kimi':
        (box.work / 'empty-skills').mkdir()
    if b['auth'] == 'api':
        if b['client'] == 'gemini':
            (box.home / '.gemini/settings.json').write_text(
                '{"security":{"auth":{"selectedType":"gemini-api-key"}},"telemetry":{"enabled":false}}', encoding='utf-8')
        return  # only the selected API key is in child_env
    host_home = Path(env.get('HOME', str(Path.home())))
    if b['client'] == 'codex':
        source = Path(env.get('CODEX_HOME', str(host_home / '.codex'))) / 'auth.json'
        require(source.is_file(), 'Codex file login missing; authenticate with the official CLI first')
        box.copy_credential(source, '.codex/auth.json')
    elif b['client'] == 'claude':
        require(bool(env.get('CLAUDE_CODE_OAUTH_TOKEN')),
                'Isolated Claude subscription requires CLAUDE_CODE_OAUTH_TOKEN from official claude setup-token; host Keychain is not exposed')
        (box.home / '.claude.json').write_text('{"hasCompletedOnboarding":true}', encoding='utf-8')
    else:
        source = Path(env.get('BENCH_GEMINI_AUTH_DIR', str(host_home / '.gemini')))
        require((source / 'oauth_creds.json').is_file(), 'Official Gemini file login missing')
        box.copy_credential(source / 'oauth_creds.json', '.gemini/oauth_creds.json')
        if (source / 'google_accounts.json').is_file():
            box.copy_credential(source / 'google_accounts.json', '.gemini/google_accounts.json')
        (box.home / '.gemini/settings.json').write_text(
            '{"security":{"auth":{"selectedType":"oauth-personal"}},"telemetry":{"enabled":false}}', encoding='utf-8')


def launch(registry, ident):
    matches = [b for b in registry['baselines'] if b['id'] == ident]
    require(len(matches) == 1, 'Unknown/duplicate baseline')
    b = matches[0]
    require(b.get('execution_mode') != 'manual_app',
            'This baseline uses the official App; import its answer with record-answer --session')
    require(registry.get('adapter_build_sha256') == adapter_build(), 'Adapter code changed; resolve registry again')
    exe, version = inspect_client(b['client'], dict(os.environ, **{b['client'].upper() + '_CLI': b['executable']}))
    require(exe == b['executable'] and version == b['client_version'], 'CLI changed since preflight; resolve again')
    env = child_env(b, os.environ)
    work = Path.cwd().resolve()
    task, query = Path(os.environ['BENCH_TASK']), Path(os.environ['BENCH_QUERY'])
    answer = Path(os.environ['BENCH_ANSWER'])
    for path in (task, query, answer):
        require(path.resolve().parent == work and not path.is_symlink(), 'Invalid candidate workspace path')
    require(not answer.exists(), 'Answer already exists')
    require(read(task)['execution'].get('web_mode') == 'provided_materials_only',
            'This pilot launcher only supports tasks marked provided_materials_only')
    prompt = candidate_prompt(read(task))
    report_path = Path(os.environ.get('BENCH_ISOLATION_REPORT', str(work / 'isolation.json')))
    require(report_path.resolve() in (work / 'isolation.json', work.parent / 'adapter-isolation.json'),
            'Invalid organizer isolation report path')
    require(not report_path.is_symlink(), 'Isolation report cannot be a symlink')
    report = {'status': 'not_started'}
    phase = 'isolation'
    def terminate(signum, frame):
        raise SystemExit(143)  # do not use InterruptedError: selectors may swallow it
    previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        with CandidateSandbox(exe, provider_hosts(b, os.environ), system_user_lookup=b['client'] == 'codex') as box:
            box.stage(task, query)
            report = box.verify()
            # Report is written by the trusted adapter OUTSIDE the candidate sandbox.
            write(report_path, report)
            phase = 'tls_setup'
            env.update(stage_tls(box, b, os.environ))
            report['tls_verification'] = 'enabled'
            report['ca_bundle_sha256'] = file_hash(box.home / 'ca-bundle.pem')
            phase = 'authentication_setup'
            stage_auth(box, b, os.environ)
            staged_answer = box.work / 'answer.md'
            report['client_started'] = True
            write(report_path, report)
            phase = 'client_execution'
            result = box.run(command(b, env, staged_answer, prompt),
                             input=None if b['client'] == 'kimi' else prompt, env=env, log_dir=work)
            require(result.returncode == 0, 'Client failed; inspect private client.stderr.log')
            for name, original in (('task.json', task), ('query.md', query)):
                require(regular_bytes(box.work / name) == regular_bytes(original), 'Candidate input changed')
            phase = 'output_validation'
            final, events, models, usage = parse_output(b['client'], result.stdout)
            write(work / 'model-observation.json', dict(requested=b['model'], reported=models,
                  status=('reported_match' if all(m == b['model'] for m in models) else 'mismatch') if models else 'unverified'))
            phase = 'model_identity'
            require(not models or all(m == b['model'] for m in models), 'Reported model differs from requested model; no automatic fallback accepted')
            (work / 'trajectory.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')
            write(work / 'usage.json', usage)
            if final is None:
                final = regular_bytes(staged_answer).decode('utf-8')
            require(isinstance(final, str) and final.strip(), 'Empty final answer')
            answer.write_text(final, encoding='utf-8')
    except (ValueError, OSError, SystemExit, KeyboardInterrupt) as error:
        # Error text can contain provider output/secrets: retain only a category here.
        write(work / 'failure.json', {'phase': phase, 'exception_type': type(error).__name__,
                                     'detail': 'Inspect private adapter/client logs; no automatic retry.'})
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        # Context-manager cleanup runs on normal exit, errors and runner SIGTERM timeouts.
        report.setdefault('cleanup', 'not_started')
        write(report_path, report)


def adapter_build():
    return digest({p: file_hash(ROOT / 'benchmark' / p)
                   for p in ('adapters.py', 'isolation.py', 'network.py', 'runner.py', 'batch.py', 'core.py',
                             'runtime_common.py')})


def select_baselines(registry, ids):
    if not ids:
        return registry
    require(len(ids) == len(set(ids)), 'Duplicate baseline selection')
    available = {b['id'] for b in registry['baselines']}
    require(set(ids) <= available, 'Unknown baseline selection: ' + ', '.join(sorted(set(ids) - available)))
    return dict(registry, baselines=[b for b in registry['baselines'] if b['id'] in ids])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Eight-configuration pilot: local checks only unless launch is requested')
    parser.add_argument('--registry', type=Path, default=ROOT / 'config/baselines.example.json')
    parser.add_argument('--baseline', action='append', dest='selected', help='Select baseline ID (repeatable)')
    sub = parser.add_subparsers(dest='action', required=True)
    check = sub.add_parser('check')
    check.add_argument('--resolve-out', type=Path, help='Write runnable registry only when all local checks pass')
    check.add_argument('--report-out', type=Path, help='Save local readiness report without credentials')
    sub.add_parser('isolation-check', help='Probe OS boundaries and installed CLI startup; no credentials or model calls')
    run = sub.add_parser('launch')
    run.add_argument('--baseline', required=True)
    args = parser.parse_args(argv)
    if args.action == 'isolation-check':
        reports = []
        selected = select_baselines(read(args.registry), args.selected)['baselines']
        manual = [b['id'] for b in selected if b.get('execution_mode') == 'manual_app']
        automated = [b for b in selected if b.get('execution_mode') != 'manual_app']
        clients = (['shell'] if automated else []) + sorted({b['client'] for b in automated})
        for client in clients:
            try:
                exe = '/bin/sh' if client == 'shell' else inspect_client(client, os.environ)[0]
            except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                reports.append({'client': client, 'status': 'blocked', 'error': str(error)})
                continue
            with CandidateSandbox(exe, system_user_lookup=client == 'codex') as box:
                (box.work / 'task.json').write_text('{}', encoding='utf-8')
                (box.work / 'query.md').write_text('isolation probe', encoding='utf-8')
                report = dict(box.verify(), client=client)
                if client != 'shell':
                    result = box.run([box.executable, '--version'], timeout=15)
                    require(result.returncode == 0, client + ' cannot start inside sandbox')
                    report['client_startup'] = 'passed'
            report['cleanup'] = 'complete'
            reports.append(report)
        print(json.dumps({'model_calls': 0, 'reports': reports, 'manual_apps_not_sandboxed': manual}, ensure_ascii=False, indent=2))
        return 0 if all(r['status'] == 'passed' for r in reports) else 1
    registry = select_baselines(read(args.registry), args.selected)
    if args.action == 'launch':
        launch(registry, args.baseline)
        return 0
    report, resolved = preflight(registry, os.environ)
    ok = all(r['local_checks'] in ('passed', 'manual_app') for r in report)
    result = dict(local_checks_passed=ok, baselines=report, model_calls=0,
                  note='Local checks do not verify login, entitlement, endpoint compatibility or isolation.')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.report_out:
        require(not args.report_out.exists(), 'Report already exists; choose a new path')
        write(args.report_out, result)
    if args.resolve_out:
        require(ok, 'Resolve blocked: address all local blockers above')
        out = args.resolve_out.resolve()
        require(not out.exists(), 'Resolved registry already exists; preserve it and choose a new path')
        resolved['adapter_build_sha256'] = adapter_build()
        for b in resolved['baselines']:
            if b.get('execution_mode') == 'manual_app':
                continue
            b['command'] = [sys.executable, str(ROOT / 'scripts/baselines.py'), '--registry', str(out),
                            'launch', '--baseline', b['id']]
        write(out, resolved)
    return 0 if ok else 1
