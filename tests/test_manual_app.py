"""Manual App boundaries, capture and mixed batch behavior; never opens a product."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from benchmark.adapters import ROOT, preflight, launch
from benchmark.batch import batch_run, batch_status
from benchmark.core import candidate_prompt, load_tasks, read, write
from benchmark.manual import record_app_answer
from benchmark.__main__ import main


class ManualAppTests(unittest.TestCase):
    def setUp(self):
        self.registry = read(ROOT / 'config/baselines.example.json')
        self.baseline = next(b for b in self.registry['baselines'] if b['id'] == 'kimi-k3')
        self.registry = dict(self.registry, baselines=[self.baseline])
        self.protocol = read(ROOT / 'config/protocol.json')
        task, rubric = load_tasks(ROOT, 'dev')[0]
        self.rows = [(dict(task, execution=dict(task['execution'], web_mode='provided_materials_only')), rubric)]

    def prepare(self, directory):
        out = Path(directory) / 'batch'
        result = batch_run(self.rows, self.protocol, self.registry, out)
        self.assertEqual(result['manual_pending'], 1)
        run = out / 'kimi-k3' / self.rows[0][0]['id'] / 'r1'
        session = read(run / 'app-session.template.json')
        session.update(surface='desktop_app', app_version='test-app-1.0', selected_model_label='K3', mode='普通对话',
                       reasoning_setting='默认', web_search='disabled', memory='disabled',
                       new_conversation=True, extra_user_messages=0, model_switch_observed=False,
                       started_at='2026-10-08T20:00:00+08:00', finished_at='2026-10-08T20:05:00+08:00',
                       outcome='completed')
        session_path = Path(directory) / 'filled-session.json'
        write(session_path, session)
        answer = Path(directory) / 'original.md'
        answer.write_bytes('完整原始答卷\r\n保留格式。\r\n'.encode())
        return out, run, session_path, answer

    def test_app_needs_neither_cli_nor_api_and_cannot_launch(self):
        with patch('benchmark.adapters.inspect_client') as inspect:
            report, resolved = preflight(self.registry, {})
            inspect.assert_not_called()
        self.assertEqual(report[0]['local_checks'], 'manual_app')
        self.assertEqual(report[0]['blockers'], [])
        self.assertNotIn('api_key_env', resolved['baselines'][0])
        with self.assertRaisesRegex(ValueError, 'official App'):
            launch(self.registry, 'kimi-k3')

    def test_all_three_app_configs_skip_cli_credentials_and_execution(self):
        registry = read(ROOT / 'config/baselines.example.json')
        registry = dict(registry, baselines=[b for b in registry['baselines'] if b.get('execution_mode') == 'manual_app'])
        self.assertEqual({b['id'] for b in registry['baselines']},
                         {'gemini-app', 'kimi-k3', 'qwen-app'})
        with patch('benchmark.adapters.inspect_client') as inspect:
            report, resolved = preflight(registry, {})
            inspect.assert_not_called()
        self.assertTrue(all(r['local_checks'] == 'manual_app' and not r['blockers'] for r in report))
        self.assertTrue(all(b['command'] is None and 'api_key_env' not in b for b in resolved['baselines']))
        with tempfile.TemporaryDirectory() as directory, patch('benchmark.batch.execute') as execute:
            result = batch_run(self.rows, self.protocol, registry, Path(directory) / 'batch', True)
            self.assertEqual(result['manual_pending'], 3)
            self.assertEqual(result['exit_code'], 0)
            execute.assert_not_called()
        by_id = {b['id']: b for b in registry['baselines']}
        self.assertEqual(by_id['gemini-app']['model'], 'gemini-app-unverified')
        self.assertNotIn('requested_model', by_id['gemini-app'])

    def test_packet_contains_only_candidate_fields_and_same_cli_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            _, run, _, _ = self.prepare(directory)
            prompt = (run / 'workspace/app-prompt.md').read_text()
            self.assertEqual(prompt, candidate_prompt(self.rows[0][0]))
            self.assertNotIn('rubric_sha256', prompt)
            self.assertNotIn('teacher_calibration', prompt)
            self.assertFalse((run / 'workspace/rubric.json').exists())

    def test_raw_answer_and_settings_are_preserved_without_claiming_api_or_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            out, run, session, answer = self.prepare(directory)
            result = main(['record-answer', '--run-dir', str(run), '--answer', str(answer), '--session', str(session)])
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['elapsed_seconds'], 300)
            self.assertFalse(result['isolation_verified'])
            self.assertEqual(result['model_observation']['status'], 'operator_reported_unverified')
            self.assertEqual((run / 'workspace/answer.md').read_bytes(), answer.read_bytes())
            self.assertEqual(result['experiment']['app_settings']['app_version'], 'test-app-1.0')
            with self.assertRaises(ValueError):
                record_app_answer(run, answer, session)
            with patch('benchmark.batch.execute') as execute:
                batch_run(self.rows, self.protocol, self.registry, out, True, True)
                execute.assert_not_called()
            self.assertEqual(batch_status(out)['baselines']['kimi-k3']['statuses'], {'completed': 1})

    def test_execute_leaves_app_tasks_pending_and_does_not_start_a_process(self):
        with tempfile.TemporaryDirectory() as directory, patch('benchmark.batch.execute') as execute:
            result = batch_run(self.rows, self.protocol, self.registry, Path(directory) / 'batch', True)
            self.assertEqual(result['manual_pending'], 1)
            execute.assert_not_called()

    def test_wrong_model_and_enabled_search_are_rejected_before_copying_answer(self):
        for key, value in [('selected_model_label', 'K2.5'), ('web_search', 'enabled'), ('extra_user_messages', 1)]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                _, run, session, answer = self.prepare(directory)
                data = read(session); data[key] = value; write(session, data)
                with self.assertRaises(ValueError):
                    record_app_answer(run, answer, session)
                self.assertFalse((run / 'workspace/answer.md').exists())
                self.assertEqual(read(run / 'run.json')['status'], 'prepared')

    def test_failed_or_overtime_attempt_is_not_completed(self):
        for overtime in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                _, run, session, answer = self.prepare(directory)
                data = read(session)
                if overtime:
                    data['finished_at'] = '2026-10-08T20:16:00+08:00'
                else:
                    data['outcome'] = 'failed'; answer = None
                write(session, data)
                result = record_app_answer(run, answer, session)
                self.assertEqual(result['status'], 'timeout' if overtime else 'failed')

    def test_qwen_app_does_not_claim_the_requested_0902_snapshot(self):
        all_baselines = read(ROOT / 'config/baselines.example.json')['baselines']
        self.baseline = next(b for b in all_baselines if b['id'] == 'qwen-app')
        self.registry = dict(self.registry, baselines=[self.baseline])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'batch'
            batch_run(self.rows, self.protocol, self.registry, out)
            run = out / 'qwen-app' / self.rows[0][0]['id'] / 'r1'
            session = read(run / 'app-session.template.json')
            session.update(surface='desktop_app', app_version='test-app-1', selected_model_label='界面未显示版本', mode='普通对话',
                           reasoning_setting='默认', web_search='disabled', memory='unknown',
                           new_conversation=True, extra_user_messages=0, model_switch_observed=False,
                           started_at='2026-10-08T20:00:00+08:00', finished_at='2026-10-08T20:05:00+08:00',
                           outcome='completed')
            write(Path(directory) / 'session.json', session)
            answer = Path(directory) / 'answer.md'; answer.write_text('原始答卷')
            result = record_app_answer(run, answer, Path(directory) / 'session.json')
            self.assertEqual(result['model'], 'qwen-app-unverified')
            self.assertEqual(result['model_observation']['requested'], 'qwen3.8-max-0902')
            self.assertEqual(result['model_observation']['status'], 'operator_reported_unverified')


if __name__ == '__main__':
    unittest.main()
