"""Migration boundaries and documented headless protocol; no provider requests."""
import base64
import copy
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark import adapters, cli_adapters, cli_setup, native_auth, native_sessions
from benchmark.cli_common import read, write
from benchmark.isolation import supported
from benchmark.native_runtime import capture, index_events

ROOT = Path(__file__).resolve().parents[1]


def recipe(api=False):
    agent = next(a for a in read(ROOT / 'config/agents-cli.example.json')['agents']
                 if a['cli'] == 'antigravity')
    if api:
        agent.update(auth='api', api_key_env='BENCH_AGY_TEST_KEY', model='fixture-model',
                     base_url='https://generativelanguage.googleapis.com')
    return agent


def events():
    return [
        {'event': 'init', 'conversation_id': 'fixture-session', 'init': {'model': 'requested-only'}},
        {'event': 'step_update', 'step_update': {'conversation_id': 'fixture-session',
         'step_type': 'tool', 'state': 'DONE', 'tool_info': {'output': 'Not the answer'}}},
        {'event': 'result', 'result': {'conversation_id': 'fixture-session', 'status': 'SUCCESS',
         'response': 'Final answer', 'num_turns': 1, 'usage': {'input_tokens': 10, 'output_tokens': 3}}},
    ]


class MigrationTests(unittest.TestCase):
    def test_new_catalog_is_distinct_and_does_not_switch_auth(self):
        catalog = read(ROOT / 'config/agents-cli.example.json')
        cli_adapters.validate_registry(catalog)
        agent = recipe()
        self.assertEqual(agent['binary'], 'agy')
        self.assertEqual(agent['cli_version'], '1.3.2')
        self.assertEqual(agent['auth'], 'subscription')
        self.assertNotIn('api_key_env', agent)
        self.assertNotIn('gemini-3-8-flash', {a['id'] for a in catalog['agents']})

    def test_old_personal_login_is_blocked_even_with_cached_credentials(self):
        agent = dict(recipe(), cli='gemini', id='legacy-gemini', binary='gemini',
                     reasoning_effort='native_default')
        with patch('benchmark.native_auth.regular_bytes', side_effect=AssertionError('Do not read retired credentials')):
            issue = native_auth.auth_issues(agent, {})
            self.assertIn('2026-06-18', issue[0])
            with self.assertRaisesRegex(ValueError, '2026-06-18'):
                native_auth.stage_auth(SimpleNamespace(), agent, {})
        cli_adapters.validate_registry(dict(schema='native-cli-batch.1', repeats=1,
                                            timeout_seconds=5, agents=[agent]))

    def test_legacy_adapter_also_blocks_personal_subscription(self):
        b = dict(id='legacy-gemini', client='gemini', model='fixture', auth='subscription')
        with patch('benchmark.adapters.inspect_client', return_value=('/fixture/gemini', '0.63.0')):
            report, _ = adapters.preflight({'baselines': [b]}, {})
        self.assertIn(native_auth.GEMINI_PERSONAL_RETIRED, report[0]['blockers'])
        with self.assertRaisesRegex(ValueError, '2026-06-18'):
            adapters.stage_auth(SimpleNamespace(), b, {})

    def test_selected_keychain_login_stays_in_temporary_home(self):
        token = {'auth_method': 'consumer', 'id_token': 'synthetic-id', 'token': {
            'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
            'expiry': '2099-01-01T00:00:00Z', 'token_type': 'Bearer'}}
        raw = b'go-keyring-base64:' + base64.b64encode(json.dumps(token).encode())
        with tempfile.TemporaryDirectory() as tmp, patch('benchmark.native_auth.subprocess.run',
                return_value=SimpleNamespace(returncode=0, stdout=raw)) as run:
            home = Path(tmp) / 'candidate'; home.mkdir()
            box = SimpleNamespace(home=home, report={'status': 'passed'})
            env = {'HOME': tmp, 'GEMINI_API_KEY': 'unselected'}
            self.assertEqual(native_auth.auth_issues(recipe(), env), [])
            selected = native_auth.stage_auth(box, recipe(), env)
            self.assertEqual(selected, {})
            target = home / '.gemini/antigravity-cli/antigravity-oauth-token'
            self.assertEqual(read(target), token)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/security', 'find-generic-password',
                                                   '-s', 'gemini', '-a', 'antigravity', '-w'])
            _, _, child = cli_adapters.invocation(recipe(), 'agy', 'prompt', home, home, home / 'answer',
                                                   box=box, selected_env=selected)
            self.assertNotIn('GEMINI_API_KEY', child)
            self.assertEqual(read(home / '.gemini/antigravity-cli/settings.json'), {})

    def test_missing_login_never_falls_back_to_api_or_reveals_keychain_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch('benchmark.native_auth.subprocess.run',
                return_value=SimpleNamespace(returncode=1, stdout=b'secret', stderr=b'secret')):
            env = {'HOME': tmp, 'GEMINI_API_KEY': 'unselected'}
            issue = native_auth.auth_issues(recipe(), env)
            self.assertIn('Missing or incompatible', issue[0])
            self.assertNotIn('secret', issue[0])
            with self.assertRaisesRegex(ValueError, 'Missing or incompatible'):
                native_auth.stage_auth(SimpleNamespace(), recipe(), env)

    def test_setup_without_login_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp, patch('benchmark.cli_adapters.shutil.which', return_value=None), \
                patch('benchmark.native_auth.antigravity_credential', side_effect=ValueError('unavailable')), \
                patch('benchmark.network.inspect_upstreams', return_value=[]):
            path = Path(tmp) / 'registry.json'
            original = {'schema': 'native-cli-batch.1', 'repeats': 1, 'timeout_seconds': 5, 'agents': [recipe()]}
            write(path, original)
            result = cli_setup.setup(path, Path(tmp) / 'local.json', interactive=False)
            self.assertEqual(result['status'], 'setup_required')
            self.assertEqual(result['agents'][0]['credentials']['status'], 'not_configured')
            self.assertEqual(read(Path(tmp) / 'local.json'), original)
            self.assertEqual(result['model_calls'], 0)

    def test_checkpoint_allowlist_excludes_auth_and_personal_settings(self):
        self.assertEqual(native_sessions.capability('antigravity')['resume_flag'], '--conversation')
        for suffix in ['antigravity-oauth-token', 'settings.json', 'cli.log', 'config/mcp_config.json']:
            with self.assertRaises(ValueError):
                native_sessions.valid_name('native/.gemini/antigravity-cli/' + suffix, 'antigravity')
        native_sessions.valid_name('native/.gemini/antigravity-cli/conversations/' + 'a' * 36 + '.db', 'antigravity')

    def test_checkpoint_keeps_tool_output_but_excludes_runtime_log_alias(self):
        sid = '12345678-1234-1234-1234-123456789abc'
        with tempfile.TemporaryDirectory(prefix='baoyan-candidate-') as tmp:
            root = Path(tmp); home = root / 'home'; work = root / 'workspace'; work.mkdir()
            native = home / '.gemini/antigravity-cli'
            database = native / 'conversations' / (sid + '.db'); database.parent.mkdir(parents=True)
            database.write_bytes(b'synthetic-session')
            tool_output = native / 'brain' / sid / '.system_generated/steps/2/content.md'
            tool_output.parent.mkdir(parents=True); tool_output.write_text('cached native web content')
            (native / 'cli.log').symlink_to(root / 'private-log')
            (native / 'antigravity-oauth-token').write_text('do-not-export')
            target = root / 'checkpoint.json'
            native_sessions.save(SimpleNamespace(home=home, work=work, root=root), 'antigravity', sid,
                target, antigravity_state={'num_turns': 1, 'usage': {'input_tokens': 10, 'output_tokens': 3}})
            checkpoint = native_sessions.load(target)
            self.assertIn('native/' + tool_output.relative_to(home).as_posix(), checkpoint['files'])
            self.assertNotIn('do-not-export', target.read_text())
            self.assertFalse(any('cli.log' in n or 'oauth-token' in n for n in checkpoint['files']))

    def test_explicit_api_builds_fresh_settings_and_native_arguments(self):
        agent = recipe(api=True)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            box = SimpleNamespace(home=home, report={'status': 'passed'})
            selected = native_auth.stage_auth(box, agent, {'BENCH_AGY_TEST_KEY': 'synthetic', 'OTHER_KEY': 'private'})
            args, stdin, env = cli_adapters.invocation(agent, '/fixture/agy', 'hello', home, home,
                                                      home / 'answer', box=box, selected_env=selected)
            self.assertEqual(read(home / '.gemini/antigravity-cli/settings.json'), {'modelProvider': 'gemini'})
            self.assertEqual(env['GEMINI_API_KEY'], 'synthetic')
            self.assertNotIn('OTHER_KEY', env)
            self.assertIsNone(stdin)
            self.assertIn('--dangerously-skip-permissions', args)
            self.assertIn('--disable-slash-commands', args)
            self.assertNotIn('--approval-mode', args)
            self.assertFalse((home / '.gemini/oauth_creds.json').exists())
            box.report['status'] = 'not_checked'
            with self.assertRaisesRegex(ValueError, 'verified outer boundary'):
                cli_adapters.invocation(agent, 'agy', 'hello', home, home, home / 'a', box=box, selected_env=selected)


class ProtocolTests(unittest.TestCase):
    def parse(self, rows, previous=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            path.write_text(''.join(json.dumps(e) + '\n' for e in rows))
            return cli_adapters.parse_result('antigravity', path, previous_antigravity=previous)

    def test_final_response_and_usage_without_invented_model_evidence(self):
        result = self.parse(events())
        self.assertEqual(result['answer'], 'Final answer')
        self.assertEqual(result['session_id'], 'fixture-session')
        self.assertEqual(result['reported_models'], [])
        self.assertEqual(result['usage'], {'input_tokens': 10, 'output_tokens': 3, 'cost_usd': None})

    def test_failures_mixed_sessions_and_cumulative_usage_are_rejected(self):
        cases = []
        for field, value in [('status', 'ERROR'), ('status', 'CANCELLED'), ('error', 'provider failure'),
                             ('conversation_id', 'other'), ('response', ''), ('num_turns', 2),
                             ('num_turns', True), ('usage', {'input_tokens': -1})]:
            rows = events(); rows[-1]['result'][field] = value; cases.append(rows)
        rows = events(); rows[1]['step_update']['conversation_id'] = 'other'; cases.append(rows)
        rows = events(); rows[1]['step_update'] = None; cases.append(rows)
        rows = events(); rows.append(copy.deepcopy(rows[-1])); cases.append(rows)
        rows = events(); rows.insert(1, {'event': 'error'}); cases.append(rows)
        rows = events(); rows.append(rows.pop(1)); cases.append(rows)
        cases.append(events()[:-1])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.parse(rows)

    def test_continuation_accounts_only_new_tokens_and_rejects_regression(self):
        prior = {'num_turns': 1, 'usage': {'input_tokens': 10, 'output_tokens': 3}}
        rows = events(); rows[-1]['result'].update(num_turns=2, usage={'input_tokens': 25, 'output_tokens': 9})
        result = self.parse(rows, prior)
        self.assertEqual(result['usage'], {'input_tokens': 15, 'output_tokens': 6, 'cost_usd': None})
        self.assertEqual(result['antigravity_state']['usage']['input_tokens'], 25)
        rows[-1]['result']['usage']['input_tokens'] = 9
        with self.assertRaisesRegex(ValueError, 'cumulative usage'):
            self.parse(rows, prior)

    def test_index_retains_antigravity_event_types_and_raw_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, dest = Path(tmp) / 'events', Path(tmp) / 'trace'
            path.write_text(''.join(json.dumps(e) + '\n' for e in events()))
            self.assertEqual(index_events(path, dest, 'antigravity'), 3)
            rows = [json.loads(line) for line in dest.read_text().splitlines()]
            self.assertEqual([r['kind'] for r in rows], ['init', 'step_update', 'result'])
            self.assertEqual([r['event'] for r in rows], events())

    def test_old_gemini_result_remains_readable(self):
        from tests.test_native import fake_events
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events'
            path.write_text(''.join(json.dumps(e) + '\n' for e in fake_events('gemini', 'legacy-model')))
            self.assertEqual(cli_adapters.parse_result('gemini', path)['reported_models'], ['legacy-model'])


@unittest.skipUnless(supported(), 'Requires macOS Seatbelt')
class IsolatedCaptureTests(unittest.TestCase):
    def test_synthetic_one_shot_uses_real_boundary_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'BENCH_AGY_TEST_KEY': 'synthetic'}):
            base = Path(tmp); binary = base / 'fake-agy'
            binary.write_text('#!/bin/sh\nset -eu\n'
                'test -f "$HOME/.gemini/antigravity-cli/settings.json"\n'
                'test "$GEMINI_API_KEY" = synthetic\n'
                'test ! -e "$HOME/.gemini/oauth_creds.json"\n' +
                "printf '%s' 'Final answer' > answer.md\n" +
                "printf '%s\\n' " + ' '.join(shlex.quote(json.dumps(e)) for e in events()) + '\n')
            binary.chmod(0o700)
            agent = recipe(api=True)
            result = capture(agent, str(binary), 'fixture only', 'provided_materials_only', 5,
                base / 'events', base / 'stderr', base / 'answer', base / 'execution', base / 'trace')
            self.assertEqual(result['status'], 'completed', result)
            self.assertTrue(result['isolation_verified'])
            self.assertEqual(result['isolation']['cleanup'], 'complete')
            self.assertEqual((base / 'answer').read_text(), 'Final answer')
            self.assertEqual(read(base / 'execution')['cli'], 'antigravity')


if __name__ == '__main__':
    unittest.main()
