"""Explicit native authentication. Never inherit provider routing or personal settings."""
import base64
import json
import subprocess
import os
from pathlib import Path
from urllib.parse import urlsplit

from .cli_common import require
from .runtime_common import regular_bytes

GEMINI_PERSONAL_RETIRED = (
    'Gemini CLI personal Google sign-in (free/Pro/Ultra) ended on 2026-06-18. '
    'Use a new Antigravity recipe; this legacy subscription recipe cannot verify enterprise entitlement. '
    'Paid API access must be selected explicitly; no automatic migration or API fallback.')


def subscription_issue(agent):
    if auth_mode(agent) != 'subscription':
        return None
    return GEMINI_PERSONAL_RETIRED if agent['cli'] == 'gemini' else None


def auth_mode(agent):
    # Older frozen recipes retain their documented authentication choice.
    return agent.get('auth', 'api' if agent.get('api_key_env') else 'subscription')


def credential_names(agent):
    if auth_mode(agent) == 'api':
        return [agent['api_key_env']]
    return ['CLAUDE_CODE_OAUTH_TOKEN'] if agent['cli'] == 'claude' else []


def auth_home(cli, env):
    host = Path(env.get('HOME', str(Path.home())))
    name = {'codex': 'CODEX_HOME', 'kimi': 'KIMI_CODE_HOME', 'gemini': 'BENCH_GEMINI_AUTH_DIR'}[cli]
    return Path(env.get(name, str(host / { 'codex': '.codex', 'kimi': '.kimi-code', 'gemini': '.gemini'}[cli])))


def kimi_config(agent, env):
    try:
        import tomllib
    except ImportError:
        raise ValueError('Kimi subscription setup requires Python 3.11+ (stdlib TOML reader)') from None
    home = auth_home('kimi', env)
    config = tomllib.loads(regular_bytes(home / 'config.toml', 1024 * 1024).decode('utf-8'))
    provider = config.get('providers', {}).get('managed:kimi-code', {})
    endpoint = urlsplit(provider.get('base_url', ''))
    require(endpoint.scheme == 'https' and endpoint.hostname in ('api.kimi.com', 'api.kimi.ai')
            and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment),
            'Kimi subscription requires an official managed endpoint; run kimi login')
    oauth = provider.get('oauth', {})
    require(oauth.get('storage') == 'file' and oauth.get('key') == 'oauth/kimi-code',
            'Kimi subscription requires the official default file OAuth login')
    models = [m for m in config.get('models', {}).values() if isinstance(m, dict)
              and m.get('provider') == 'managed:kimi-code' and m.get('model') == agent['model']]
    require(models, 'Requested model is absent from the Kimi subscription login; no API fallback')
    model = {k: v for k, v in models[0].items() if k in (
        'provider', 'model', 'max_context_size', 'capabilities', 'support_efforts', 'default_effort',
        'protocol', 'beta_api', 'adaptive_thinking')}
    clean = {'default_model': 'benchmark', 'providers': {'managed:kimi-code': {
        'type': provider.get('type', 'kimi'), 'base_url': provider['base_url'], 'oauth': {
            'storage': 'file', 'key': 'oauth/kimi-code'}}}, 'models': {'benchmark': model}}
    require(clean['providers']['managed:kimi-code']['type'] == 'kimi', 'Unexpected managed Kimi provider type')
    # Preserve native search/fetch with the same subscription, without copying custom services.
    clean['services'] = {name: {'base_url': provider['base_url'].rstrip('/') + '/' + suffix,
        'oauth': {'storage': 'file', 'key': 'oauth/kimi-code'}}
        for name, suffix in (('moonshot_search', 'search'), ('moonshot_fetch', 'fetch'))}
    return clean


def toml_text(value, path=()):
    """Serialize the small, explicitly selected native configuration; no user text interpolation."""
    def scalar(v):
        if isinstance(v, bool):
            return 'true' if v else 'false'
        require(type(v) in (str, int, float, list), 'Unsupported native configuration value')
        if isinstance(v, list):
            return '[' + ', '.join(scalar(x) for x in v) + ']'
        return json.dumps(v, ensure_ascii=False, allow_nan=False)
    lines = ['[' + '.'.join(json.dumps(p) for p in path) + ']'] if path else []
    lines += [json.dumps(k) + ' = ' + scalar(v) for k, v in value.items() if not isinstance(v, dict)]
    for k, v in value.items():
        if isinstance(v, dict):
            lines += ['', toml_text(v, (*path, k))]
    return '\n'.join(lines) + '\n'


def antigravity_credential(env):
    """Read one native login, never expose the host keychain to candidate processes."""
    explicit = env.get('BENCH_ANTIGRAVITY_AUTH_FILE')
    default = Path(env.get('HOME', str(Path.home()))) / '.gemini/antigravity-cli/antigravity-oauth-token'
    if explicit or default.exists():
        raw = regular_bytes(Path(explicit) if explicit else default, 1024 * 1024)
    else:
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', 'gemini',
                                 '-a', 'antigravity', '-w'], capture_output=True, timeout=10)
        require(result.returncode == 0, 'Antigravity native login unavailable; run agy to sign in')
        raw = result.stdout.strip()
        if raw.startswith(b'go-keyring-base64:'):
            raw = base64.b64decode(raw.split(b':', 1)[1], validate=True)
    value = json.loads(raw)
    require(isinstance(value, dict) and value.get('auth_method') == 'consumer',
            'Antigravity subscription requires a consumer login; no API fallback')
    token = value.get('token')
    require(isinstance(token, dict) and all(isinstance(token.get(k), str) and token[k]
            for k in ('access_token', 'refresh_token', 'expiry', 'token_type')),
            'Invalid Antigravity native login')
    # Copy only authentication fields, never host settings, plugins or histories.
    return json.dumps({k: value[k] for k in ('auth_method', 'id_token', 'token') if k in value}).encode()


def auth_issues(agent, env=None):
    env = os.environ if env is None else env
    issue = subscription_issue(agent)
    if issue:
        return [issue]
    for name in credential_names(agent):
        if not env.get(name):
            return ['No credential configured for this run: ' + name +
                    '; use setup, --import-credentials, or the named environment variable']
    if auth_mode(agent) == 'api' or agent['cli'] == 'claude':
        return []
    cli = agent['cli']
    try:
        if cli == 'antigravity':
            antigravity_credential(env)
            return []
        home = auth_home(cli, env)
        if cli == 'codex':
            value = json.loads(regular_bytes(home / 'auth.json', 1024 * 1024))
            require(value.get('auth_mode') == 'chatgpt' and value.get('tokens'),
                    'Codex subscription requires a ChatGPT file login; run codex login')
        elif cli == 'kimi':
            kimi_config(agent, env)
            regular_bytes(home / 'credentials/kimi-code.json', 1024 * 1024)
        else:
            regular_bytes(home / 'oauth_creds.json', 1024 * 1024)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        return ['Missing or incompatible ' + cli + ' subscription file login; see docs/native-cli.md']
    return []


def stage_auth(box, agent, env=None):
    env = os.environ if env is None else env
    issues = auth_issues(agent, env)
    require(not issues, '; '.join(issues))
    cli, mode = agent['cli'], auth_mode(agent)
    selected = {name: env[name] for name in credential_names(agent)}
    if mode == 'api':
        return selected
    if cli == 'antigravity':
        target = box.home / '.gemini/antigravity-cli/antigravity-oauth-token'
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(antigravity_credential(env))
        target.chmod(0o600)
    elif cli == 'claude':
        (box.home / '.claude.json').write_text('{"hasCompletedOnboarding":true}')
    else:
        home = auth_home(cli, env)
        if cli == 'codex':
            box.copy_credential(home / 'auth.json', '.codex/auth.json')
        elif cli == 'kimi':
            box.copy_credential(home / 'credentials/kimi-code.json',
                                '.kimi-code/credentials/kimi-code.json')
            if (home / 'device_id').is_file():
                box.copy_credential(home / 'device_id', '.kimi-code/device_id')
            (box.home / '.kimi-code/config.toml').write_text(toml_text(kimi_config(agent, env)))
        elif cli == 'gemini':
            box.copy_credential(home / 'oauth_creds.json', '.gemini/oauth_creds.json')
            if (home / 'google_accounts.json').is_file():
                box.copy_credential(home / 'google_accounts.json', '.gemini/google_accounts.json')
            (box.home / '.gemini/settings.json').write_text(
                '{"security":{"auth":{"selectedType":"oauth-personal"}},"telemetry":{"enabled":false}}')
    return selected
