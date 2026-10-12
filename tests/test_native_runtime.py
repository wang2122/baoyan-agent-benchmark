"""Real local boundaries and auth separation, with synthetic credentials and no model calls."""
import json
import os
import shlex
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark import cli_adapters, native_auth, native_runtime
from benchmark.isolation import CandidateSandbox, supported
from benchmark.network import ConnectGate


class PolicyTests(unittest.TestCase):
    def test_claude_primary_routing_preserves_native_helper_usage(self):
        with tempfile.TemporaryDirectory() as d:
            events = Path(d) / 'events.jsonl'
            final = {'type': 'result', 'subtype': 'success', 'is_error': False,
                     'session_id': 'fixture-session', 'result': 'answer', 'usage': {},
                     'modelUsage': {'opus-fixture': {}, 'haiku-fixture': {}}}
            primary = {'type': 'assistant', 'parent_tool_use_id': None,
                       'message': {'model': 'opus-fixture'}}
            helper = {'type': 'assistant', 'parent_tool_use_id': 'tool-fixture',
                      'message': {'model': 'haiku-fixture'}}
            def parse(rows):
                events.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                return cli_adapters.parse_result('claude', events)
            parsed = parse([primary, helper, final])
            self.assertEqual(parsed['reported_models'], ['haiku-fixture', 'opus-fixture'])
            self.assertEqual(parsed['primary_reported_models'], ['opus-fixture'])
            self.assertEqual(parsed['other_reported_models'], ['haiku-fixture'])
            # Actual primary model switching must still fail the exact-model check.
            changed = parse([primary, dict(helper, parent_tool_use_id=None), final])
            self.assertNotEqual(changed['primary_reported_models'], ['opus-fixture'])
            # Aggregate-only evidence does not establish a matching primary model.
            unknown = parse([final])
            self.assertNotIn('primary_reported_models', unknown)

    def test_codex_parser_requires_one_completed_turn_and_preserves_unknown_usage(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); events = base / 'events.jsonl'; answer = base / 'answer.md'
            answer.write_text('Original final answer')
            start = {'type': 'thread.started', 'thread_id': 'fixture-session', 'model': 'fixture-model'}
            completed = {'type': 'turn.completed', 'usage': {'input_tokens': 8, 'output_tokens': 3}}
            def parse(rows):
                events.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                return cli_adapters.parse_result('codex', events, answer)
            result = parse([start, {'type': 'tool_use', 'content': 'not the final answer'}, completed])
            self.assertEqual(result['answer'], 'Original final answer')
            self.assertEqual(result['session_id'], 'fixture-session')
            self.assertEqual(result['reported_models'], ['fixture-model'])
            self.assertEqual(result['usage'], {'input_tokens': 8, 'output_tokens': 3, 'cost_usd': None})
            unknown = parse([{'type': 'thread.started', 'thread_id': 'fixture-session'},
                             {'type': 'turn.completed', 'usage': {'input_tokens': True, 'output_tokens': -1}}])
            self.assertEqual(unknown['reported_models'], [])
            self.assertEqual(unknown['usage'], {'input_tokens': None, 'output_tokens': None, 'cost_usd': None})
            for rows in ([start], [completed], [start, start, completed], [start, completed, completed],
                         [start, completed, {'type': 'turn.failed'}], []):
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    parse(rows)
            link = base / 'linked-events'; link.symlink_to(events)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                cli_adapters.parse_result('codex', link, answer)

    def test_subscription_cannot_accidentally_carry_api_routing(self):
        registry = {'schema': 'native-cli-batch.1', 'repeats': 1, 'timeout_seconds': 5,
            'agents': [{'id': 'kimi', 'cli': 'kimi', 'binary': 'kimi', 'cli_version': '2.1.1',
                'model': 'k3', 'reasoning_effort': 'native_default', 'auth': 'subscription',
                'api_key_env': 'UNRELATED_API_KEY', 'base_url': 'https://api.moonshot.ai/v1'}]}
        with self.assertRaisesRegex(ValueError, 'Subscription recipe'):
            cli_adapters.validate_registry(registry)
        registry['agents'][0].pop('api_key_env'); registry['agents'][0].pop('base_url')
        cli_adapters.validate_registry(registry)
        self.assertEqual(native_auth.credential_names(registry['agents'][0]), [])

    def test_network_modes_are_explicit_and_unknown_modes_fail(self):
        a = {'cli': 'codex', 'auth': 'subscription'}
        self.assertFalse(native_runtime.network_policy(a, 'provided_materials_only')[1])
        self.assertTrue(native_runtime.network_policy(a, 'historical_source_rules_in_prompt')[1])
        with self.assertRaisesRegex(ValueError, 'Unknown task web mode'):
            native_runtime.network_policy(a, 'typo')

    def test_public_proxy_still_rejects_private_addresses_and_http(self):
        gate = ConnectGate([], public_https=True)
        try:
            for target, status in [('127.0.0.1:443', b'502'), ('example.com:80', b'403')]:
                with socket.create_connection(('127.0.0.1', gate.port), timeout=2) as connection:
                    connection.sendall(('CONNECT ' + target + ' HTTP/1.1\r\n\r\n').encode())
                    self.assertIn(status, connection.recv(1024))
        finally:
            gate.close()
        self.assertFalse(gate.thread.is_alive())

    def test_lossless_event_index_preserves_native_tools_and_invalid_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            event = {'type': 'tool_use', 'name': 'WebSearch', 'parameters': {'query': 'synthetic query'}}
            (p / 'raw').write_text(json.dumps(event) + '\ninvalid\n')
            self.assertEqual(native_runtime.index_events(p / 'raw', p / 'trace', 'gemini'), 1)
            rows = [json.loads(line) for line in (p / 'trace').read_text().splitlines()]
            self.assertEqual(rows[0], {'line': 1, 'cli': 'gemini', 'kind': 'tool_use', 'event': event})
            self.assertEqual(rows[1]['kind'], 'invalid_json')


@unittest.skipUnless(supported(), 'macOS Seatbelt')
class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node runtime')
    def test_recursive_watch_inside_workspace_still_denies_outside(self):
        with tempfile.TemporaryDirectory() as d, CandidateSandbox('/bin/sh', file_watch=True) as box:
            (box.work / 'task.json').write_text('{}')
            (box.work / 'query.md').write_text('synthetic watcher check')
            box.verify()
            # fs.watch starts asynchronously; closing immediately would miss EMFILE.
            script = '''
const fs = require('node:fs');
const own = fs.watch(process.cwd(), {recursive:true});
own.on('error', () => {process.exitCode=91});
try {fs.watch(process.argv[1], {recursive:true});process.exitCode=92;}
catch(e) {if (!['EPERM','EACCES'].includes(e.code)) process.exitCode=93;}
setTimeout(() => own.close(), 200);
'''
            result = box.run([shutil.which('node'), '-e', script, d], timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_kimi_uses_selected_oauth_model_without_personal_settings(self):
        with tempfile.TemporaryDirectory() as d, CandidateSandbox('/bin/sh') as box:
            home = Path(d)
            (home / 'credentials').mkdir()
            (home / 'credentials/kimi-code.json').write_text('{"access_token":"fixture-only"}')
            (home / 'device_id').write_text('existing-device-fixture')
            (home / 'sessions').mkdir()
            (home / 'sessions/history').write_text('private-history')
            (home / 'mcp.json').write_text('{"private":"mcp"}')
            (home / 'config.toml').write_text('''
default_model = "other"
[providers."managed:kimi-code"]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
api_key = "must-not-be-copied"
[providers."managed:kimi-code".oauth]
storage = "file"
key = "oauth/kimi-code"
[providers.other]
api_key = "private-other-key"
[models.selected]
provider = "managed:kimi-code"
model = "k3"
max_context_size = 1048576
capabilities = ["thinking"]
[models.other]
provider = "other"
model = "other"
''')
            agent = {'cli': 'kimi', 'model': 'k3', 'auth': 'subscription'}
            selected = native_auth.stage_auth(box, agent, {'KIMI_CODE_HOME': str(home), 'MOONSHOT_API_KEY': 'unrelated'})
            self.assertEqual(selected, {})
            text = (box.home / '.kimi-code/config.toml').read_text()
            self.assertNotIn('private', text)
            self.assertNotIn('must-not-be-copied', text)
            self.assertIn('moonshot_search', text)
            self.assertFalse((box.home / '.kimi-code/sessions').exists())
            self.assertFalse((box.home / '.kimi-code/mcp.json').exists())
            self.assertEqual((box.home / '.kimi-code/device_id').read_text(), 'existing-device-fixture')
            self.assertEqual((box.home / '.kimi-code/credentials/kimi-code.json').stat().st_mode & 0o777, 0o600)
            agent['model'] = 'unavailable-model'
            with self.assertRaises(ValueError):
                native_auth.stage_auth(box, agent, {'KIMI_CODE_HOME': str(home)})

    def fixture(self, base, body=''):
        path = base / 'fake-codex'
        path.write_text('#!/bin/sh\n' + body + '''
printf 'Fixture answer' > answer.md
while [ "$#" -gt 0 ]; do
 if [ "$1" = "--output-last-message" ]; then shift; printf 'Fixture answer' > "$1"; fi
 shift
done
printf '%s\n' '{"type":"thread.started","thread_id":"fixture-session","model":"fixture-model"}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
''')
        path.chmod(0o700)
        return path

    def capture(self, base, binary, timeout=3, web='historical_source_rules_in_prompt'):
        a = {'id': 'fixture', 'cli': 'codex', 'cli_version': 'fixture', 'model': 'fixture-model',
            'auth': 'api', 'api_key_env': 'NATIVE_TEST_KEY', 'base_url': 'https://api.example.com/v1',
            'reasoning_effort': 'high', 'web_search': 'live'}
        with patch.dict(os.environ, {'NATIVE_TEST_KEY': 'selected-fixture', 'CLAUDE_CODE_OAUTH_TOKEN': 'unrelated-fixture',
                'OPENAI_BASE_URL': 'https://unrelated.example', 'NODE_OPTIONS': '--require /private/hook'}):
            return native_runtime.capture(a, str(binary), 'synthetic question', web, timeout,
                *(base / n for n in ('events.jsonl', 'stderr.log', 'answer.md', 'execution.json', 'trace.jsonl')))

    def test_native_tools_work_but_host_files_other_auth_and_logs_are_denied(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            secret = base / 'rubric-secret'; secret.write_text('evaluator-only')
            script = self.fixture(base, 'set -eu\n' +
                'test -z "${CLAUDE_CODE_OAUTH_TOKEN:-}"\ntest -z "${NODE_OPTIONS:-}"\n' +
                'test "$OPENAI_API_KEY" = selected-fixture\n' +
                'if cat ' + shlex.quote(str(secret)) + ' >/dev/null 2>&1; then exit 91; fi\n' +
                'if cat ' + shlex.quote(str(base / 'events.jsonl')) + ' >/dev/null 2>&1; then exit 92; fi\n' +
                'mkdir product; printf tool-output > product/result; cat product/result >/dev/null\n' +
                'test ! -e "$HOME/history"; printf fresh > "$HOME/history"\n')
            result = self.capture(base, script)
            self.assertEqual(result['status'], 'completed', (base / 'stderr.log').read_text())
            self.assertTrue(result['isolation_verified'])
            self.assertEqual(result['isolation']['cleanup'], 'complete')
            self.assertEqual((base / 'answer.md').read_text(), 'Fixture answer')
            self.assertNotIn('selected-fixture', (base / 'execution.json').read_text())
            self.assertEqual(result['auth_mode'], 'api')

    def test_timeout_retains_logs_and_cleans_owned_environment(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            script = self.fixture(base, 'echo before-timeout >&2\nsleep 30\n')
            result = self.capture(base, script, timeout=1, web='provided_materials_only')
            self.assertEqual(result['status'], 'timeout')
            self.assertEqual(result['isolation']['cleanup'], 'complete')
            self.assertIn('before-timeout', (base / 'stderr.log').read_text())
            self.assertFalse((base / 'answer.md').exists())

    def test_missing_backend_never_launches_unsandboxed(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve()
            script = self.fixture(base, 'touch ' + shlex.quote(str(base / 'launched')) + '\n')
            with patch('benchmark.isolation.supported', return_value=False):
                result = self.capture(base, script)
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['isolation_verified'])
            self.assertFalse((base / 'launched').exists())


if __name__ == '__main__':
    unittest.main()
