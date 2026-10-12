"""macOS Seatbelt candidate boundary, with fresh state and a per-attempt HTTPS gate.

This is a local process sandbox, not a VM or a defense against a compromised kernel.
The organizer/adapter are trusted. Candidate processes never receive their file access.
"""
from contextlib import ExitStack
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from .cli_common import require
from .runtime_common import regular_bytes
from .network import ConnectGate

SANDBOX = '/usr/bin/sandbox-exec'
PROFILE = 'macos-seatbelt-provider-proxy-v1'
SYSTEM_READ = ('/System/Library', '/usr/lib', '/usr/bin', '/bin', '/sbin', '/Library/Apple',
               '/private/var/db/dyld', '/System/Volumes/Preboot/Cryptexes/OS',
               '/usr/share/icu', '/usr/share/locale', '/usr/share/zoneinfo', '/private/var/db/timezone')
DEVICE_FILES = ('/dev/null', '/dev/zero', '/dev/random', '/dev/urandom', '/dev/stdin', '/dev/stdout', '/dev/stderr')


def supported():
    return platform.system() == 'Darwin' and Path(SANDBOX).is_file()


def npm_runtime_dependencies(package):
    """Resolve DSH's declared installed dependency graph, not its whole npm tree.

    DSH loads shipped profile bundles as sibling packages. Keep resolution within
    this installation so a linked package cannot grant access to a host checkout.
    Missing optional dependencies are normal for platform-specific distributions.
    """
    import re
    package = Path(package).resolve(strict=True)
    installation = next((p for p in package.parents if p.name == 'node_modules'), None)
    require(installation is not None, 'DSH must have an identifiable npm installation')
    found, pending = {package}, [package]
    name_pattern = re.compile(r'^(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+$')
    while pending:
        current = pending.pop()
        info = json.loads((current / 'package.json').read_text(encoding='utf-8'))
        required = info.get('dependencies', {})
        optional = info.get('optionalDependencies', {})
        peers = info.get('peerDependencies', {})
        peer_meta = info.get('peerDependenciesMeta', {})
        require(all(isinstance(v, dict) for v in (required, optional, peers, peer_meta)), 'Invalid npm dependency declarations')
        required = {**peers, **required}
        optional = {**{name: version for name, version in peers.items()
                      if isinstance(peer_meta.get(name), dict) and peer_meta[name].get('optional') is True}, **optional}
        for name in dict(required, **optional):
            require(isinstance(name, str) and name_pattern.fullmatch(name)
                    and all(part not in ('.', '..') for part in name.split('/')), 'Invalid npm dependency name')
            resolved = None
            for parent in (current, *current.parents):
                if not parent.is_relative_to(installation):
                    break
                candidate = parent / name if parent.name == 'node_modules' else parent / 'node_modules' / name
                if (candidate / 'package.json').is_file():
                    resolved = candidate.resolve(strict=True)
                    require(resolved.is_relative_to(installation), 'Runtime dependency escapes npm installation')
                    break
            if resolved is None:
                require(name in optional, 'Missing installed runtime dependency: ' + name)
                continue
            if resolved not in found:
                found.add(resolved)
                require(len(found) <= 2048, 'Unexpectedly large npm dependency graph')
                pending.append(resolved)
    return sorted(found - {package})


def runtime_paths(executable):
    """Allow the selected installed CLI package, not the whole npm tree or user home."""
    path = Path(executable).resolve(strict=True)
    roots = [path]
    for parent in path.parents:
        package = parent / 'package.json'
        if package.is_file():
            try:
                name = json.loads(package.read_text()).get('name')
            except (ValueError, OSError):
                continue
            if name in ('@openai/codex', '@anthropic-ai/claude-code', '@google/gemini-cli',
                        '@qwen-code/qwen-code', '@moonshot-ai/kimi-code', '@deepseek-ai/dsh'):
                roots = [parent]
                if name == '@deepseek-ai/dsh':
                    roots += npm_runtime_dependencies(parent)
                break
    node = shutil.which('node')
    if node:
        roots.append(Path(node).resolve(strict=True))
    # Homebrew CLIs can pin a different interpreter in their shebang.
    with path.open('rb') as stream:
        first = stream.read(256).split(b'\n', 1)[0]
    if first.startswith(b'#!/'):
        interpreter = Path(first[2:].decode('utf-8').split()[0])
        if interpreter.is_file() and interpreter != Path('/usr/bin/env'):
            roots.append(interpreter.resolve(strict=True))
    # Explicit installed runtime dylibs only, never an entire package-manager prefix.
    pending = [p for p in roots if p.is_file()]
    seen = set()
    while pending and len(seen) < 100:
        binary = pending.pop()
        if binary in seen:
            continue
        seen.add(binary)
        result = subprocess.run(['/usr/bin/otool', '-L', str(binary)],
                                capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines()[1:]:
            name = line.strip().split(' (', 1)[0]
            if name.startswith(('/System/', '/usr/lib/')):
                continue
            if name.startswith('@rpath/'):
                library = binary.parent.parent / 'lib' / name.removeprefix('@rpath/')
            elif name.startswith('@loader_path/'):
                library = binary.parent / name.removeprefix('@loader_path/')
            elif name.startswith('/'):
                library = Path(name)
            else:
                continue
            if library.is_file():
                library = library.resolve()
                if library not in roots:
                    roots.append(library)
                    pending.append(library)
    return roots


class ResumedDirectory:
    """Recreate an absent task-owned temporary path; never adopt existing contents."""
    def __init__(self, name):
        require(isinstance(name, str) and re.fullmatch(r'baoyan-candidate-[a-z0-9_]{8}', name),
                'Invalid continuation directory identity')
        self.path = Path(tempfile.gettempdir()).resolve() / name
        self.path.mkdir(mode=0o700)  # An occupied path fails, including a symlink.
        self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self.name = str(self.path)

    def cleanup(self):
        if self.path.exists():
            require(not self.path.is_symlink() and
                    (self.path.stat().st_dev, self.path.stat().st_ino) == self.identity,
                    'Continuation directory ownership changed; refusing cleanup')
            shutil.rmtree(self.path)


class CandidateSandbox:
    def __init__(self, executable, hosts=(), *, public_https=False, environment_keys=(), file_watch=False,
                 resume_directory=None, loopback_listener=False, system_trust=False, allow_pty=False,
                 system_user_lookup=False):
        require(supported(), 'This isolation backend requires macOS sandbox-exec; refusing unsandboxed execution')
        self.executable = str(Path(executable).resolve(strict=True))
        self.temp = (ResumedDirectory(resume_directory) if resume_directory is not None else
                     tempfile.TemporaryDirectory(prefix='baoyan-candidate-'))
        self.root = Path(self.temp.name).resolve()
        self.work, self.home, self.scratch = (self.root / name for name in ('workspace', 'home', 'tmp'))
        for folder in (self.work, self.home, self.scratch):
            folder.mkdir(mode=0o700)
        for name in ('.codex', '.claude', '.gemini', '.kimi-code'):
            (self.home / name).mkdir(mode=0o700)
        self.roots = []
        self.environment_keys = frozenset(environment_keys)
        self.file_watch = file_watch
        self.loopback_listener = loopback_listener
        self.system_trust = system_trust
        self.allow_pty = allow_pty
        self.system_user_lookup = system_user_lookup
        self.gate = None
        self.process = None
        self.report = {'backend': PROFILE, 'status': 'not_checked', 'scope': 'local_boundary_probes',
                       'credential_state': 'per_attempt', 'allowed_hosts': sorted(hosts),
                       'network': 'public_https_no_private_addresses' if public_https else 'provider_allowlist',
                       'file_watch': file_watch, 'loopback_listener': loopback_listener, 'system_trust': system_trust,
                       'system_user_lookup': system_user_lookup,
                       'pty': 'sandbox_created_only' if allow_pty else 'disabled'}
        try:
            self.roots = runtime_paths(executable)
            self.gate = ConnectGate(hosts, public_https=public_https)
            self.report['upstream_proxy'] = self.gate.upstream_proxy
            self.report['fake_ip_policy'] = 'exact_provider_names_via_configured_proxy_only'
            if public_https:
                self.report['public_web_fake_ip_policy'] = 'cloudflare_doh_public_ip_pinning'
                self.report['public_web_dns_resolver'] = 'https://1.1.1.1/dns-query'
            self.profile = self.root / 'profile.sb'
            self.profile.write_text(self.profile_text(), encoding='utf-8')
            self.report['profile_sha256'] = hashlib.sha256(self.profile.read_bytes()).hexdigest()
        except BaseException:
            self.close()
            raise

    def profile_text(self):
        def rule(kind, path):
            return '(' + kind + ' ' + json.dumps(str(path), ensure_ascii=False) + ')'
        reads = [rule('literal', '/')] + [rule('subpath', path) for path in SYSTEM_READ]
        reads += [rule('subpath', path) for path in (self.work, self.home, self.scratch)]
        reads += [rule('subpath' if p.is_dir() else 'literal', p) for p in self.roots]
        reads += [rule('literal', path) for path in (*DEVICE_FILES, '/private/etc/ssl/openssl.cnf', '/private/etc/ssl/cert.pem')]
        if self.loopback_listener:
            reads.append(rule('literal', '/private/etc/hosts'))
        # Metadata allows runtime path traversal; directory listings and contents stay denied.
        return '\n'.join([
            '(version 1)', '(deny default)', '(allow file-read-metadata)',
            '(allow process-exec process-fork)', '(allow signal (target self))', '(allow sysctl-read)',
            '(allow file-read* file-map-executable ' + ' '.join(reads) + ')',
            '(allow file-write* ' + ' '.join(rule('subpath', p) for p in (self.work, self.home, self.scratch)) +
            ' (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))',
            '(deny file-write* ' + ' '.join(rule('literal', self.work / p) for p in ('task.json', 'query.md')) + ')',
            f'(allow network-outbound (remote tcp "localhost:{self.gate.port}"))',
            '(allow network-bind (local tcp "localhost:*"))' if self.loopback_listener else '',
            '(allow network-inbound (local tcp "localhost:*"))' if self.loopback_listener else '',
            # Certificate evaluation only; securityd and host keychain files remain denied.
            '(allow mach-lookup (global-name "com.apple.trustd.agent"))' if self.system_trust else '',
            # Codex's managed-preferences initialization resolves the current OS user.
            # This permits account metadata lookup, not host HOME contents or preferences IPC.
            '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))'
            if self.system_user_lookup else '',
            # Apple's application.sb uses this extension to scope access to allocated PTYs.
            # Never grant unfiltered access to other host or candidate slave terminals.
            '(allow pseudo-tty)' if self.allow_pty else '',
            '(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))' if self.allow_pty else '',
            '(allow file-read* file-write* file-ioctl (require-all '
            '(regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))' if self.allow_pty else '',
            # Native recursive watchers need this service; file-read restrictions still apply.
            '(allow mach-lookup (global-name "com.apple.FSEvents"))' if self.file_watch else '',
            # No securityd/keychain, automation, Docker sockets, host services, or arbitrary Mach IPC.
        ]) + '\n'

    def environment(self, selected_env=None):
        selected_env = selected_env or {}
        env = {k: v for k, v in selected_env.items() if k in self.environment_keys or k.startswith('ANTHROPIC_') or
               k in ('SSL_CERT_FILE', 'CODEX_CA_CERTIFICATE', 'NODE_EXTRA_CA_CERTS', 'GEMINI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
                     'CLAUDE_CODE_DISABLE_AUTO_MEMORY', 'CLAUDE_CODE_SUBAGENT_MODEL',
                     'KIMI_MODEL_NAME', 'KIMI_MODEL_API_KEY', 'KIMI_MODEL_BASE_URL',
                     'KIMI_MODEL_PROVIDER_TYPE', 'KIMI_MODEL_MAX_CONTEXT_SIZE',
                     'KIMI_DISABLE_TELEMETRY', 'KIMI_CODE_NO_AUTO_UPDATE', 'KIMI_DISABLE_CRON')}
        node = shutil.which('node')
        search = ([str(Path(node).resolve().parent)] if node else []) + ['/usr/bin', '/bin']
        # These are child-only standard runtime settings; the host environment is never changed.
        env.update(HOME=str(self.home), TMPDIR=str(self.scratch), PATH=os.pathsep.join(search),
                   LANG='en_US.UTF-8', OPENSSL_CONF='/private/etc/ssl/openssl.cnf', CODEX_HOME=str(self.home / '.codex'),
                   CLAUDE_CONFIG_DIR=str(self.home / '.claude'),
                   CLAUDE_CODE_TMPDIR=str(self.scratch),
                   GEMINI_CLI_HOME=str(self.home),
                   KIMI_CODE_HOME=str(self.home / '.kimi-code'),
                   XDG_CONFIG_HOME=str(self.home / '.config'), XDG_CACHE_HOME=str(self.home / '.cache'),
                   HTTPS_PROXY=f'http://127.0.0.1:{self.gate.port}', HTTP_PROXY=f'http://127.0.0.1:{self.gate.port}',
                   NO_PROXY='', NODE_USE_ENV_PROXY='1')
        return env

    def stage(self, task, query):
        for name, path in (('task.json', task), ('query.md', query)):
            data = regular_bytes(path)
            (self.work / name).write_bytes(data)
            (self.work / name).chmod(0o400)

    def copy_credential(self, source, relative):
        # Copy an opaque, provider-specific official CLI credential file, never config/history/plugins.
        target = self.home / relative
        require(target.resolve().is_relative_to(self.home), 'Credential destination outside private home')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(regular_bytes(source, limit=1024 * 1024))
        target.chmod(0o600)

    def run(self, argv, *, input=None, env=None, timeout=None, log_dir=None, log_paths=None):
        require(self.process is None, 'Sandbox process already active')
        with ExitStack() as files:
            paths = []
            outputs = [subprocess.PIPE, subprocess.PIPE]
            if log_dir is not None or log_paths is not None:
                paths = list(map(Path, log_paths)) if log_paths is not None else [
                    Path(log_dir) / 'client.stdout.log', Path(log_dir) / 'client.stderr.log']
                require(len(paths) == 2 and all(not p.resolve().is_relative_to(self.root) for p in paths),
                        'Client logs must stay outside candidate access')
                # Write directly to private organizer-owned files. Logs survive timeout/SIGTERM.
                outputs = [files.enter_context(os.fdopen(os.open(path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb')) for path in paths]
            self.process = subprocess.Popen([SANDBOX, '-f', str(self.profile), *map(str, argv)],
                cwd=self.work, env=self.environment(env), text=True, stdin=subprocess.PIPE,
                stdout=outputs[0], stderr=outputs[1], start_new_session=True)
            try:
                stdout, stderr = self.process.communicate(input=input, timeout=timeout)
                if paths:
                    stdout, stderr = [regular_bytes(path).decode('utf-8', errors='replace') for path in paths]
                return subprocess.CompletedProcess(argv, self.process.returncode, stdout, stderr)
            finally:
                # Own process group only, including any children left after the CLI exits.
                self.stop_process()

    def stop_process(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
            self.process = None

    def verify(self):
        # Neither this control file nor a sibling run is readable from the candidate sandbox.
        secret = self.root / 'organizer-canary.txt'
        secret.write_text('private-evaluator-canary', encoding='utf-8')
        (self.work / 'escape-link').symlink_to(secret)
        probe = self.work / 'boundary-probe.sh'
        probe.write_text('''#!/bin/sh
set -eu
cat task.json >/dev/null
printf ok > allowed-output
if cat "$1" >/dev/null 2>&1; then exit 21; fi
if cat escape-link >/dev/null 2>&1; then exit 22; fi
if (printf changed > "$1") 2>/dev/null; then exit 23; fi
if (printf changed > task.json) 2>/dev/null; then exit 24; fi
# Descendant commands inherit the sandbox.
if /bin/sh -c 'cat "$1"' sh "$1" >/dev/null 2>&1; then exit 25; fi
# Direct local service access and unlisted HTTPS tunnels must fail.
if /usr/bin/curl --max-time 2 --noproxy '*' -sS "http://127.0.0.1:$2" >/dev/null 2>&1; then exit 26; fi
if /usr/bin/curl --max-time 2 -sS https://not-allowed.invalid/ >/dev/null 2>&1; then exit 27; fi
''', encoding='utf-8')
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            result = self.run(['/bin/sh', str(probe), str(secret), str(listener.getsockname()[1])], timeout=10)
            # Connecting successfully but timing out is not evidence of denial: no connection may queue.
            listener.settimeout(0.05)
            try:
                connection, _ = listener.accept()
                connection.close()
                raise ValueError('Sandbox reached an unrelated local listener')
            except TimeoutError:
                pass
        self.report['boundary_probe'] = {'exit_code': result.returncode,
            'proxy_denial_observed': bool(self.gate.denied_hosts)}
        require(result.returncode == 0, 'Sandbox boundary probe failed: ' + str(result.returncode))
        expected_denial = '<unreachable-or-nonpublic>' if self.gate.public_https else '<not-allowlisted>'
        require(expected_denial in self.gate.denied_hosts, 'HTTPS gate was not exercised by boundary probe')
        require(secret.read_text() == 'private-evaluator-canary', 'Organizer canary changed')
        if self.allow_pty:
            self.verify_pty(secret)
        for name in ('boundary-probe.sh', 'escape-link', 'allowed-output'):
            (self.work / name).unlink(missing_ok=True)
        secret.unlink()
        self.report.update(status='passed', checks=['candidate_read', 'candidate_write', 'outside_read_denied',
            'outside_write_denied', 'input_write_denied', 'symlink_escape_denied',
            'descendant_read_denied', 'local_service_denied', 'unlisted_proxy_host_denied'])
        if self.allow_pty:
            self.report['checks'] += ['pty_command_execution', 'pty_outside_read_denied',
                                      'pty_input_write_denied', 'host_pty_read_write_denied']
        return self.report

    def verify_pty(self, secret):
        # Allocate our own unsandboxed canary terminal; never touch a user's terminal.
        master, slave = os.openpty()
        try:
            result = self.run(['/usr/bin/script', '-q', '/dev/null', '/bin/sh', '-c', '''
set -eu
test -t 0 && test -t 1
/bin/stty -a >/dev/null
/bin/cat task.json >/dev/null
printf pty > allowed-output
if /bin/cat "$1" >/dev/null 2>&1; then exit 31; fi
if (printf changed > task.json) 2>/dev/null; then exit 32; fi
if (: < "$2") 2>/dev/null; then exit 33; fi
if (: > "$2") 2>/dev/null; then exit 34; fi
printf '\\nbenchmark-pty-probe-passed\\n'
''', 'pty-probe', str(secret), os.ttyname(slave)], timeout=10)
            # BSD script need not propagate its child's exit code: require the final sentinel too.
            require(result.returncode == 0 and 'benchmark-pty-probe-passed' in result.stdout.splitlines(),
                    'Sandbox PTY boundary probe failed')
        finally:
            os.close(slave)
            os.close(master)

    def close(self):
        self.report['cleanup'] = 'in_progress'
        try:
            self.stop_process()
            if self.gate is not None:
                self.gate.close()
                self.gate = None
            self.temp.cleanup()
        except BaseException:
            self.report['cleanup'] = 'failed'
            raise
        self.report['cleanup'] = 'complete'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
