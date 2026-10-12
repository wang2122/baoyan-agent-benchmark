"""Exercise real macOS boundaries and malicious outputs without model calls."""
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from benchmark.isolation import CandidateSandbox, regular_bytes, supported
from benchmark.network import ConnectGate, public_addresses
from benchmark.adapters import child_env, stage_auth, stage_tls


class ExportTests(unittest.TestCase):
    def test_output_links_and_nonregular_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_text('private')
            link = root / 'link'
            link.symlink_to(source)
            with self.assertRaises(OSError):
                regular_bytes(link)
            os.link(source, root / 'hardlink')
            with self.assertRaises(ValueError):
                regular_bytes(root / 'hardlink')
            os.mkfifo(root / 'pipe')
            with self.assertRaises(ValueError):
                regular_bytes(root / 'pipe')

    def test_proxy_rejects_local_dns_before_connecting(self):
        for address in ('127.0.0.1', '10.1.1.1', '169.254.169.254', '::1', '224.0.0.1'):
            with patch('benchmark.network.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, '', (address, 443))]):
                with self.assertRaises(ValueError):
                    public_addresses('approved.example')

    def test_proxy_allows_only_exact_https_targets_and_closes(self):
        gate = ConnectGate(['approved.example'])
        port = gate.port
        try:
            for target in ('evil.example:443', 'approved.example:80', 'approved.example.evil:443'):
                with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
                    connection.sendall(('CONNECT ' + target + ' HTTP/1.1\r\n\r\n').encode())
                    self.assertIn(b'403', connection.recv(1024))
        finally:
            gate.close()
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=1)


@unittest.skipUnless(supported(), 'macOS Seatbelt integration tests')
class SandboxTests(unittest.TestCase):
    def test_ten_parallel_public_web_boundaries_do_not_depend_on_dns(self):
        def probe(i):
            with CandidateSandbox('/bin/sh', public_https=True, allow_pty=(i % 5 == 0)) as box:
                self.inputs(box)
                return box.verify()
        with patch('benchmark.network.socket.getaddrinfo', side_effect=AssertionError('No DNS in probes')):
            with ThreadPoolExecutor(10) as pool:
                reports = list(pool.map(probe, range(10)))
        self.assertTrue(all(r['status'] == 'passed' and r['cleanup'] == 'complete' for r in reports))

    def inputs(self, box):
        (box.work / 'task.json').write_text('{}')
        (box.work / 'query.md').write_text('test')

    def test_real_boundary_probes_and_cleanup(self):
        with CandidateSandbox('/bin/sh') as box:
            root = box.root
            self.inputs(box)
            report = box.verify()
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(len(report['checks']), 9)
            self.assertIn('<not-allowlisted>', box.gate.denied_hosts)
        self.assertFalse(root.exists())

    def test_other_attempt_and_host_home_are_unreadable(self):
        with CandidateSandbox('/bin/sh') as first, CandidateSandbox('/bin/sh') as second:
            secret = first.work / 'answer.md'
            secret.write_text('other candidate answer')
            result = second.run(['/bin/cat', str(secret)], timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('other candidate answer', result.stdout)
            original_home = str(Path.home())
            result = second.run(['/bin/ls', original_home], timeout=5)
            self.assertNotEqual(result.returncode, 0)

    def test_codex_user_lookup_preserves_file_and_process_boundaries(self):
        import pwd
        with CandidateSandbox('/bin/sh') as other, \
                CandidateSandbox('/bin/sh', system_user_lookup=True) as box:
            self.inputs(box)
            self.assertEqual(box.verify()['status'], 'passed')
            self.assertTrue(box.report['system_user_lookup'])
            self.assertNotIn('com.apple.system.opendirectoryd.libinfo', other.profile_text())
            result = box.run(['/usr/bin/id', '-nu', str(os.getuid())], timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), pwd.getpwuid(os.getuid()).pw_name)
            secret = other.work / 'answer.md'
            secret.write_text('other candidate answer')
            for path in (secret, Path.home(), Path.home() / '.codex',
                         Path.home() / 'Library/Preferences', '/Library/Preferences',
                         '/private/etc/passwd'):
                with self.subTest(path=str(path)):
                    command = '/bin/ls' if Path(path).is_dir() else '/bin/cat'
                    result = box.run([command, str(path)], timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('other candidate answer', result.stdout)
        self.assertFalse(box.root.exists())

    def test_pty_executes_without_exposing_host_terminal_or_files(self):
        with CandidateSandbox('/bin/sh', allow_pty=True) as box:
            self.inputs(box)
            report = box.verify()
            self.assertEqual(report['pty'], 'sandbox_created_only')
            self.assertEqual(len(report['checks']), 13)
            self.assertIn('host_pty_read_write_denied', report['checks'])
        self.assertFalse(box.root.exists())

    def test_another_sandbox_cannot_open_allocated_pty(self):
        with CandidateSandbox('/bin/sh', allow_pty=True) as first, \
                CandidateSandbox('/bin/sh', allow_pty=True) as second, ThreadPoolExecutor(1) as pool:
            future = pool.submit(first.run, ['/usr/bin/script', '-q', '/dev/null', '/bin/sh', '-c',
                'test -t 0 && echo $$ > tty-child.pid; sleep 3'], timeout=8)
            marker = first.work / 'tty-child.pid'
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            # ttyname() may need a forbidden /dev listing. Resolve only our known child from the parent.
            pid = marker.read_text().strip()
            tty = '/dev/' + subprocess.run(['/bin/ps', '-p', pid, '-o', 'tty='],
                                           capture_output=True, text=True, check=True).stdout.strip()
            self.assertRegex(tty, r'^/dev/ttys[0-9]+$')
            result = second.run(['/bin/sh', '-c',
                'if (: < "$1") 2>/dev/null; then exit 1; fi; '
                'if (: > "$1") 2>/dev/null; then exit 2; fi', 'probe', tty], timeout=2)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(future.result().returncode, 0)

    def test_pty_timeout_terminates_terminal_child(self):
        with CandidateSandbox('/bin/sh', allow_pty=True) as box:
            with self.assertRaises(subprocess.TimeoutExpired):
                box.run(['/usr/bin/script', '-q', '/dev/null', '/bin/sh', '-c',
                         'echo $$ > child.pid; sleep 30'], timeout=0.5)
            pid = (box.work / 'child.pid').read_text().strip()
            # This PID came from our own temporary sandbox, not a host session.
            result = subprocess.run(['/bin/ps', '-p', pid, '-o', 'stat='], capture_output=True, text=True)
            self.assertFalse(result.stdout.strip())
            self.assertIsNone(box.process)

    def test_runtime_environment_does_not_inherit_secrets_or_settings(self):
        with CandidateSandbox('/bin/sh') as box:
            env = box.environment({'DEEPSEEK_API_KEY': 'unrelated', 'BENCH_TASK': '/private/task',
                                   'NODE_OPTIONS': '--require /private/hook', 'ANTHROPIC_API_KEY': 'selected',
                                   'CLAUDE_CODE_TMPDIR': '/tmp/host-claude'})
            self.assertNotIn('DEEPSEEK_API_KEY', env)
            self.assertNotIn('BENCH_TASK', env)
            self.assertNotIn('NODE_OPTIONS', env)
            self.assertEqual(env['ANTHROPIC_API_KEY'], 'selected')
            self.assertEqual(env['HOME'], str(box.home))
            self.assertNotEqual(env['HOME'], os.environ.get('HOME'))
            self.assertEqual(env['CLAUDE_CODE_TMPDIR'], str(box.scratch))
            result = box.run(['/bin/sh', '-c', 'mkdir "$CLAUDE_CODE_TMPDIR/claude-test"'], env=env)
            self.assertEqual(result.returncode, 0)

    def test_only_official_credential_file_is_copied(self):
        with tempfile.TemporaryDirectory() as directory, CandidateSandbox('/bin/sh') as box:
            host = Path(directory)
            (host / '.codex').mkdir()
            (host / '.codex/auth.json').write_text('{"synthetic_token":"not-a-real-secret"}')
            (host / '.codex/config.toml').write_text('private config')
            (host / '.codex/history.jsonl').write_text('private history')
            stage_auth(box, {'client': 'codex', 'auth': 'subscription'}, {'HOME': str(host)})
            self.assertEqual([p.name for p in (box.home / '.codex').iterdir()], ['auth.json'])

    def test_tls_uses_public_bundle_without_exposing_host_keychain(self):
        with tempfile.TemporaryDirectory() as directory, CandidateSandbox('/bin/sh') as box:
            env = stage_tls(box, {'client': 'codex'}, {})
            self.assertEqual(env['CODEX_CA_CERTIFICATE'], str(box.home / 'ca-bundle.pem'))
            self.assertEqual((box.home / 'ca-bundle.pem').read_bytes(), Path('/private/etc/ssl/cert.pem').read_bytes())
            self.assertEqual(box.environment(env)['CODEX_CA_CERTIFICATE'], env['CODEX_CA_CERTIFICATE'])
            bad = Path(directory) / 'bad.pem'
            bad.write_text('-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----')
            with self.assertRaisesRegex(ValueError, 'public certificates only'):
                stage_tls(box, {'client': 'codex'}, {'CODEX_CA_CERTIFICATE': str(bad)})
            bad.write_text('not a certificate')
            with self.assertRaises((ValueError, OSError)):
                stage_tls(box, {'client': 'codex'}, {'CODEX_CA_CERTIFICATE': str(bad)})

    def test_timeout_kills_owned_process_and_cleans_scratch(self):
        with CandidateSandbox('/bin/sh') as box:
            root = box.root
            with self.assertRaises(subprocess.TimeoutExpired):
                box.run(['/bin/sh', '-c', 'sleep 30'], timeout=0.1)
            self.assertIsNone(box.process)
        self.assertFalse(root.exists())


@unittest.skipUnless(supported(), 'macOS Seatbelt integration tests')
class LauncherTests(unittest.TestCase):
    def test_codex_file_login_and_last_message_in_isolated_attempt(self):
        import sys
        from benchmark.adapters import ROOT, VERSION, adapter_build
        from benchmark.core import read, write
        from benchmark.reproduction import suite_rows
        from benchmark.runner import execute, prepare
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            host = base / 'host'; (host / '.codex').mkdir(parents=True)
            (host / '.codex/auth.json').write_text('{"synthetic_login":"fixture-only"}')
            (host / '.codex/history.jsonl').write_text('host-history-must-stay-private')
            cli = base / 'fake-codex'
            cli.write_text('''#!/bin/sh
if [ "$1" = "--version" ]; then printf 'codex-cli 0.154.0\\n'; exit 0; fi
test -f "$CODEX_HOME/auth.json" || exit 91
test ! -e "$CODEX_HOME/history.jsonl" || exit 92
test -z "$OPENAI_API_KEY" || exit 93
test -z "$DEEPSEEK_API_KEY" || exit 94
cat >/dev/null
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then shift; answer_path="$1"; fi
  shift
done
printf 'Synthetic Codex final answer\\n' > "$answer_path"
printf '%s\\n' '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":4}}'
''')
            cli.chmod(0o700)
            b = dict(id='fake-codex', kind='agent', client='codex', auth='subscription', executable=str(cli),
                     client_version='0.154.0', version=VERSION + '/codex@0.154.0', model='fixture-model')
            registry = base / 'registry.json'
            argv = [sys.executable, str(ROOT / 'scripts/baselines.py'), '--registry', str(registry),
                    'launch', '--baseline', b['id']]
            b['command'] = argv
            write(registry, {'baselines': [b], 'adapter_build_sha256': adapter_build()})
            task, rubric = suite_rows(ROOT, 'public-smoke')[0]
            run = prepare(task, rubric, base / 'attempt', read(ROOT / 'config/reproduction/materials.protocol.json'))
            with patch.dict(os.environ, {'CODEX_HOME': str(host / '.codex'), 'OPENAI_API_KEY': 'unrelated-fixture',
                                         'DEEPSEEK_API_KEY': 'unrelated-fixture'}):
                meta = execute(run, argv, b['id'], b['version'], b['model'], 1, 10)
            self.assertEqual(meta['status'], 'completed', (run / 'adapter.stderr.log').read_text())
            self.assertTrue(meta['isolation_verified'])
            self.assertEqual((run / 'workspace/answer.md').read_text(), 'Synthetic Codex final answer\n')
            self.assertEqual(meta['usage']['output_tokens'], 4)
            self.assertEqual(meta['model_observation']['status'], 'unverified')
            self.assertEqual(meta['isolation']['cleanup'], 'complete')

    def exercise(self, sleep=False):
        import shlex
        import sys
        from benchmark.adapters import ROOT, VERSION, adapter_build
        from benchmark.core import load_tasks, read, write
        from benchmark.runner import execute, prepare
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            secret = base / 'rubric-secret.txt'
            secret.write_text('evaluator-only')
            cli = base / 'fake-claude'
            body = ('#!/bin/sh\n'
                    'if [ "$1" = "--version" ]; then printf "2.1.280 (Claude Code)\\n"; exit 0; fi\n'
                    'cat >/dev/null\n'
                    'if cat ' + shlex.quote(str(secret)) + ' >/dev/null 2>&1; then exit 98; fi\n')
            if sleep:
                body += 'printf \"diagnostic before timeout\\n\" >&2\nsleep 30\n'
            body += 'printf %s ' + shlex.quote(json.dumps(dict(type='result', subtype='success', result='Synthetic original answer',
                                         modelUsage={'fake-model': {}}, usage={}))) + '\n'
            cli.write_text(body)
            cli.chmod(0o700)
            b = dict(id='fake', kind='agent', client='claude', auth='api', executable=str(cli),
                     client_version='2.1.280', version=VERSION + '/claude@2.1.280', model='fake-model',
                     api_key_env='BENCH_FAKE_API_KEY', auth_variable='ANTHROPIC_API_KEY', base_url='https://api.example.com')
            registry = base / 'registry.json'
            argv = [sys.executable, str(ROOT / 'scripts/baselines.py'), '--registry', str(registry), 'launch', '--baseline', 'fake']
            b['command'] = argv
            write(registry, {'baselines': [b], 'adapter_build_sha256': adapter_build()})
            task, rubric = load_tasks(ROOT, 'dev')[0]
            task = dict(task, execution=dict(task['execution'], web_mode='provided_materials_only'))
            out = prepare(task, rubric, base / 'attempt', read(ROOT / 'config/protocol.json'))
            argv = [sys.executable, str(ROOT / 'scripts/baselines.py'), '--registry', str(registry), 'launch', '--baseline', 'fake']
            with patch.dict(os.environ, {'BENCH_FAKE_API_KEY': 'synthetic-not-a-real-key'}):
                meta = execute(out, argv, b['id'], b['version'], b['model'], 1, 2 if sleep else 10)
            self.assertEqual(meta['status'], 'timeout' if sleep else 'completed',
                             (out / 'adapter.stderr.log').read_text())
            self.assertTrue(meta['isolation_verified'])
            self.assertTrue(meta['isolation']['client_started'])
            self.assertEqual(meta['isolation']['cleanup'], 'complete')
            if not sleep:
                self.assertEqual((out / 'workspace/answer.md').read_text(), 'Synthetic original answer')
                self.assertNotIn('synthetic-not-a-real-key', (out / 'adapter-isolation.json').read_text())
            else:
                self.assertFalse((out / 'workspace/answer.md').exists())
                self.assertIn('diagnostic before timeout', (out / 'workspace/client.stderr.log').read_text())
                self.assertEqual((out / 'workspace/client.stderr.log').stat().st_mode & 0o777, 0o600)
                self.assertEqual(read(out / 'workspace/failure.json')['phase'], 'client_execution')

    def test_full_launcher_exports_answer_and_reports_verified_boundary(self):
        self.exercise()

    def test_runner_timeout_cleans_nested_candidate_process(self):
        self.exercise(sleep=True)


if __name__ == '__main__':
    unittest.main()
