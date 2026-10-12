"""Selected local credentials, explicit literal imports and non-secret provenance.

No directory discovery, shell sourcing, model calls or credential values in reports.
"""
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import native_auth
from .runtime_common import read, regular_bytes, require


def credentials_path(registry_path):
    return Path(registry_path).with_suffix('.credentials.json')


def read_credentials(path):
    path = Path(path)
    if not path.exists():
        return {}
    require(path.is_file() and not path.is_symlink(), 'Invalid local credentials file')
    if os.name == 'posix':
        require(path.stat().st_mode & 0o077 == 0, 'Credentials file must be private: chmod 600 <file>')
    value = read(path)
    require(isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()),
            'Invalid local credentials format')
    return value


def save_credentials(path, value):
    path = Path(path)
    require(not path.is_symlink(), 'Refusing a symlink credentials file')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.credentials-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)  # mkstemp creates mode 0600, before writing secrets.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_credentials(path, agents):
    """Read only selected credential names from an explicitly supplied JSON/.env file."""
    path = Path(path)
    names = {name for agent in agents for name in native_auth.credential_names(agent)}
    require(names, 'Selected agents use official file login, not imported API/OAuth token variables')
    text = regular_bytes(path, 1024 * 1024).decode('utf-8')
    if path.suffix == '.json':
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError('Invalid credential JSON; values are not shown') from None
        require(isinstance(value, dict), 'Credential JSON must be an object')
        selected = {name: value[name] for name in names if name in value}
    else:
        selected = {}
        for line in text.splitlines():
            match = re.fullmatch(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
            if not match or match[1] not in names:
                continue
            name, raw = match[1], match[2].strip()
            require(name not in selected, 'Duplicate selected credential name: ' + name)
            require(not any(token in raw for token in ('${', '$(', '`')),
                    'Credential import accepts literal values only; no shell expansion')
            if raw.startswith('"'):
                try:
                    value, end = json.JSONDecoder().raw_decode(raw)
                except ValueError:
                    raise ValueError('Invalid quoted credential value; values are not shown') from None
                require(not raw[end:].strip() or raw[end:].lstrip().startswith('#'),
                        'Unexpected text after quoted credential value')
            elif raw.startswith("'"):
                end = raw.find("'", 1)
                require(end > 0 and (not raw[end + 1:].strip() or raw[end + 1:].lstrip().startswith('#')),
                        'Invalid quoted credential value; values are not shown')
                value = raw[1:end]
            else:
                value = re.split(r'\s+#', raw, maxsplit=1)[0].strip()
            selected[name] = value
    require(selected, 'No credential names for the selected agents were found in the supplied file')
    require(all(isinstance(value, str) and value.strip() and '\n' not in value and '\r' not in value
                for value in selected.values()), 'Selected credentials must be nonempty single-line strings')
    return selected


def credential_sources(registry, registry_path, values, env):
    """Report provenance without values; availability is not online authentication."""
    reports = {}
    for agent in registry['agents']:
        issue = native_auth.subscription_issue(agent)
        if issue:
            reports[agent['id']] = {'status': 'unsupported_auth', 'variables': [],
                'login_source': 'legacy_personal_login_retired',
                'account_verification': 'not_checked', 'issue': issue}
            continue
        names = native_auth.credential_names(agent)
        variables = [{'name': name, 'source': 'environment' if env.get(name) else
                      'local_credentials_file' if values.get(name) else 'not_configured'} for name in names]
        if names:
            available = all(row['source'] != 'not_configured' for row in variables)
            source = None
        else:
            available = not native_auth.auth_issues(agent, env)
            source = ('selected_native_login_file_or_keychain_item' if agent['cli'] == 'antigravity'
                      else 'official_cli_login_files') if available else 'login_files_missing_or_incompatible'
        reports[agent['id']] = {'status': 'configured_not_verified' if available else 'not_configured',
                               'variables': variables, 'login_source': source,
                               'account_verification': 'not_checked'}
    return reports


@contextmanager
def credential_environment(registry, registry_path):
    """Only load explicitly named API credentials, never source arbitrary .env code."""
    values = read_credentials(credentials_path(registry_path))
    sources = credential_sources(registry, registry_path, values, os.environ)
    names = {name for a in registry['agents'] for name in native_auth.credential_names(a)}
    added = []
    try:
        for name in names:
            if not os.environ.get(name) and values.get(name):
                added.append((name, os.environ.get(name)))
                os.environ[name] = values[name]
        yield sources
    finally:
        for name, prior in added:
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior
