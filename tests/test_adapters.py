"""Adapter routing and output contract; no provider requests or real CLI launches."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from benchmark.adapters import ROOT, child_env, endpoint, inspect_client, parse_output, preflight, select_baselines, stage_auth
from benchmark.batch import batch_run
from benchmark.core import load_tasks, read


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.registry = read(ROOT / 'config/baselines.example.json')

    def test_provider_credentials_do_not_cross_processes(self):
        env = {'HOME': '/tmp/home', 'PATH': '/usr/bin', 'ANTHROPIC_API_KEY': 'wrong-key',
               'ANTHROPIC_AUTH_TOKEN': 'wrong-token', 'OPENAI_API_KEY': 'wrong-openai',
               'ANTHROPIC_BASE_URL': 'https://wrong.example', 'QWEN_BASE_URL': 'https://workspace.cn-beijing.maas.aliyuncs.com/apps/anthropic'}
        for b in self.registry['baselines']:
            if b.get('api_key_env'):
                env[b['api_key_env']] = b['id'] + '-secret'
        for b in self.registry['baselines']:
            clean = child_env(b, env)
            self.assertFalse(any(k in clean for k in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'MOONSHOT_API_KEY')))
            if b['auth'] == 'api':
                self.assertEqual(clean[b['auth_variable']], b['id'] + '-secret')
                if b['client'] == 'gemini':
                    self.assertNotIn('ANTHROPIC_API_KEY', clean)
                    self.assertEqual(clean['GEMINI_API_KEY'], 'gemini-flash-secret')
                    continue
                if b['client'] == 'kimi':
                    self.assertEqual(clean['KIMI_MODEL_NAME'], 'kimi-k3')
                    self.assertNotIn('ANTHROPIC_API_KEY', clean)
                    continue
                other = 'ANTHROPIC_API_KEY' if b['auth_variable'] == 'ANTHROPIC_AUTH_TOKEN' else 'ANTHROPIC_AUTH_TOKEN'
                self.assertNotIn(other, clean)
                self.assertEqual(clean['ANTHROPIC_DEFAULT_HAIKU_MODEL'], b['model'])
                self.assertEqual(clean['ANTHROPIC_DEFAULT_FABLE_MODEL'], b['model'])
            else:
                self.assertNotIn('GEMINI_API_KEY', clean)
                self.assertNotIn('ANTHROPIC_API_KEY', clean)
                self.assertNotIn('ANTHROPIC_AUTH_TOKEN', clean)
                self.assertNotIn('ANTHROPIC_BASE_URL', clean)

    def test_gemini_api_uses_fresh_settings_without_host_login(self):
        from types import SimpleNamespace
        from benchmark.isolation import CandidateSandbox
        b = dict(id='optional-gemini-api', client='gemini', auth='api',
                 api_key_env='GEMINI_API_KEY', auth_variable='GEMINI_API_KEY',
                 base_url='https://generativelanguage.googleapis.com', model='fixture-model')
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.gemini').mkdir()
            box = SimpleNamespace(home=home, work=home, scratch=home,
                                  environment_keys=frozenset(),
                                  gate=SimpleNamespace(port=12345))
            stage_auth(box, b, {})
            self.assertEqual(read(home / '.gemini/settings.json')['security']['auth']['selectedType'], 'gemini-api-key')
            self.assertFalse((home / '.gemini/oauth_creds.json').exists())
            clean = CandidateSandbox.environment(box, child_env(b, {'GEMINI_API_KEY': 'synthetic'}))
            self.assertEqual(clean['GEMINI_API_KEY'], 'synthetic')
            self.assertNotIn('GOOGLE_APPLICATION_CREDENTIALS', clean)

    def test_offline_check_reports_blockers_without_credentials(self):
        with patch('benchmark.adapters.inspect_client', return_value=('/usr/bin/fake', '2.1.267')):
            report, resolved = preflight(self.registry, {})
        self.assertEqual(len(report), 8)
        by_id = {r['id']: r for r in report}
        self.assertIn('Requires claude >= 2.1.280', by_id['claude-opus']['blockers'])
        self.assertIn('Requires claude >= 2.1.284', by_id['claude-sonnet']['blockers'])
        self.assertIn('Missing DEEPSEEK_API_KEY', by_id['deepseek-flash']['blockers'])
        for ident in ('gemini-app',):
            self.assertEqual(by_id[ident]['local_checks'], 'manual_app')
            self.assertEqual(by_id[ident]['blockers'], [])
        self.assertEqual(by_id['qwen-app']['local_checks'], 'manual_app')
        self.assertEqual(by_id['kimi-k3']['local_checks'], 'manual_app')
        self.assertTrue(all(b['command'] is None for b in resolved['baselines']))

    def test_rejects_hermes_wrapper_without_launching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / 'gemini'
            exe.write_text('#!/bin/sh\nexec hermes -p gemini "$@"\n')
            with patch('benchmark.adapters.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError, '@google/gemini-cli'):
                    inspect_client('gemini', {'GEMINI_CLI': str(exe)})
                run.assert_not_called()

    def test_failed_or_partial_output_is_not_an_answer(self):
        for client, output in [('claude', '{"type":"result","subtype":"error_max_turns","result":"partial"}'),
                               ('gemini', '{"response":"partial","error":{"code":1}}'),
                               ('codex', '{"type":"turn.failed"}\n')]:
            with self.assertRaises(ValueError):
                parse_output(client, output)
        answer, _, models, usage = parse_output('claude', json.dumps(dict(type='result', subtype='success',
                  result='original answer', modelUsage={'kimi-k3': {}}, usage={'input_tokens': 2, 'output_tokens': 3})))
        self.assertEqual(answer, 'original answer')
        self.assertEqual(models, ['kimi-k3'])
        self.assertIsNone(usage['cost_usd'])

    def test_endpoint_rejects_embedded_credentials_and_placeholder(self):
        for value in ('http://example.com', 'https://key:secret@example.com',
                      'https://{WorkspaceId}.example.com', 'https://example.com?key=secret'):
            with self.assertRaises(ValueError):
                endpoint({'id': 'test', 'base_url': value}, {})

    def test_eight_unique_baselines_prepare_without_execution(self):
        ids = [b['id'] for b in self.registry['baselines']]
        self.assertEqual(len(set(ids)), 8)
        with tempfile.TemporaryDirectory() as directory, patch('benchmark.batch.execute') as execute:
            result = batch_run(load_tasks(ROOT, 'dev')[:1], read(ROOT / 'config/protocol.json'),
                               self.registry, Path(directory) / 'batch')
            self.assertEqual(result['attempts'], 8)
            self.assertFalse(result['executed'])
            execute.assert_not_called()

    def test_kimi_preserves_final_answer_and_rejects_tool_only_tail(self):
        trace = '\n'.join(json.dumps(e) for e in [
            {'role': 'assistant', 'content': 'Planning', 'tool_calls': [{'id': '1'}]},
            {'role': 'tool', 'content': 'source'}, {'role': 'assistant', 'content': '完整答卷\n第二行'}])
        answer, events, models, usage = parse_output('kimi', trace)
        self.assertEqual(answer, '完整答卷\n第二行')
        self.assertEqual(len(events), 3)
        self.assertEqual(models, [])
        self.assertIsNone(usage['input_tokens'])
        with self.assertRaises(ValueError):
            parse_output('kimi', '{"role":"assistant","tool_calls":[{}]}')

    def test_selection_cannot_silently_drop_unknown_model(self):
        selected = select_baselines(self.registry, ['codex-astra', 'kimi-k3'])
        self.assertEqual([b['model'] for b in selected['baselines']], ['gpt-6-astra', 'kimi-k3'])
        with self.assertRaises(ValueError):
            select_baselines(self.registry, ['old-model'])

    def test_resume_keeps_failed_attempt_and_rejects_changed_plan(self):
        from benchmark.core import write
        from benchmark.batch import batch_status
        registry = dict(self.registry, baselines=[dict(next(b for b in self.registry['baselines'] if b['id'] == 'claude-opus'),
                            version='test-v1', command=['unused'])])
        rows = load_tasks(ROOT, 'dev')[:2]
        rows = [(dict(t, execution=dict(t['execution'], web_mode='provided_materials_only')), r) for t, r in rows]
        protocol = read(ROOT / 'config/protocol.json')
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'batch'
            batch_run(rows, protocol, registry, out)
            first = out / 'claude-opus' / rows[0][0]['id'] / 'r1/run.json'
            meta = read(first); meta['status'] = 'failed'; write(first, meta)
            def fake_execute(run, *args):
                m = read(run / 'run.json'); m['status'] = 'completed'; write(run / 'run.json', m); return m
            with patch('benchmark.batch.execute', side_effect=fake_execute) as execute:
                batch_run(rows, protocol, registry, out, True, True)
                self.assertEqual(execute.call_count, 1)
            self.assertEqual(batch_status(out)['baselines']['claude-opus']['statuses'], {'failed': 1, 'completed': 1})
            with self.assertRaisesRegex(ValueError, 'changed'):
                batch_run(rows, protocol, dict(registry, timeout_seconds=50), out, True, True)


if __name__ == '__main__':
    unittest.main()
