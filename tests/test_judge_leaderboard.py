"""Synthetic offline fixtures only; these are not benchmark results."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark.core import read, write
from benchmark.judge import configuration, judge_batch, request_judgment
from benchmark.leaderboard import export_leaderboard
from benchmark.reproduction import batch_runs, reproduce
from benchmark.submissions import apply_reviews
import test_reproduction as fixtures

ROOT, PROFILE = fixtures.ROOT, fixtures.PROFILE
SINGLE_TASKS = fixtures.SINGLE_TASKS


class JudgeLeaderboardTests(unittest.TestCase):
    capture = fixtures.ReproductionTests.capture
    fill_reviews = fixtures.ReproductionTests.fill_reviews

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.batch = self.base / 'batch'
        reproduce(ROOT, PROFILE, self.batch, ['gemini-app'])
        self.runs = batch_runs(self.batch)[1]
        for run in self.runs:
            self.capture(run)
        self.config = read(ROOT / 'config/judge.example.json')
        self.config.update(provider='synthetic', model='fixture-model', expected_response_model='fixture-model',
                           endpoint='https://judge.example.org/v1/chat/completions')
        self.config_path = self.base / 'judge.json'
        write(self.config_path, self.config)
        self.out = self.base / 'judge'

    @staticmethod
    def judgment(config, messages, key, pending=False):
        inputs = json.loads(messages[1]['content'])
        assert set(inputs) == {'task.json', 'rubric.json', 'answer.md', 'scoring.md', 'query.md', 'judgment-template.json'}
        assert 'gemini-app' not in messages[1]['content']
        value = json.loads(inputs['judgment-template.json'])
        value['candidates_reviewed'] = True
        for row in value['nodes']:
            row.update(grade='pending' if pending else 0, reason='Synthetic fixture missing requirement')
        return value

    def execute(self, **kwargs):
        return judge_batch(ROOT, self.batch, self.config_path, self.out, **kwargs)

    def test_prepare_then_execute_and_resume_never_repeats_calls(self):
        with patch('benchmark.judge.request_judgment', side_effect=self.judgment) as api:
            self.assertTrue(self.execute()['execute_required'])
            api.assert_not_called()
            with patch.dict('os.environ', {'BENCHMARK_JUDGE_API_KEY': 'secret-fixture'}):
                result = self.execute(resume=True, execute=True)
                self.assertEqual(result['exit_code'], 0)
                self.assertEqual(api.call_count, 2)
                self.execute(resume=True, execute=True)
                self.assertEqual(api.call_count, 2)
        data = read(self.out / 'leaderboard/leaderboard.json')
        self.assertFalse(data['official'])
        self.assertEqual(data['rows'][0]['mean_score'], 0)
        self.assertEqual(data['rows'][0]['scored_attempts'], 2)
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertNotIn('secret-fixture', path.read_text())

    def test_pending_is_visible_and_does_not_become_zero(self):
        def pending(*args):
            return self.judgment(*args, pending=True)
        with patch('benchmark.judge.request_judgment', side_effect=pending), \
             patch.dict('os.environ', {'BENCHMARK_JUDGE_API_KEY': 'secret-fixture'}):
            self.assertEqual(self.execute(execute=True)['exit_code'], 1)
        row = read(self.out / 'leaderboard/leaderboard.json')['rows'][0]
        self.assertIsNone(row['mean_score'])
        self.assertEqual(row['pending_attempts'], 2)
        self.assertEqual(row['missing_reviews'], 0)

    def test_invalid_quote_and_failure_retained_no_retry_or_score(self):
        def invalid(*args):
            value = self.judgment(*args)
            value['nodes'][0].update(grade=1, answer_quote='Fabricated quote not in answer')
            return value
        with patch('benchmark.judge.request_judgment', side_effect=invalid) as api, \
             patch.dict('os.environ', {'BENCHMARK_JUDGE_API_KEY': 'secret-fixture'}):
            self.assertEqual(self.execute(execute=True)['status'], 'judge_failed')
            self.execute(execute=True, resume=True)
            self.assertEqual(api.call_count, 2)
        self.assertFalse((self.out / 'scored').exists())
        self.assertFalse(list((self.out / 'reviews').rglob('judgment.json')))

    def test_resume_rejects_config_or_inputs_changed_before_request(self):
        self.execute()
        config = dict(self.config, model='changed')
        write(self.config_path, config)
        with patch('benchmark.judge.request_judgment') as api:
            with self.assertRaisesRegex(ValueError, 'configuration'):
                self.execute(resume=True, execute=True)
            api.assert_not_called()
        write(self.config_path, self.config)
        packet = next((self.out / 'reviews').rglob('answer.md'))
        packet.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.execute(resume=True, execute=True)

    def test_missing_key_and_interrupted_request_never_call_or_retry(self):
        self.execute()
        with patch.dict('os.environ', {}, clear=True), patch('benchmark.judge.request_judgment') as api:
            with self.assertRaisesRegex(ValueError, 'Missing judge API key'):
                self.execute(resume=True, execute=True)
            api.assert_not_called()
        for folder in (self.out / 'reviews/packets').iterdir():
            write(folder / 'judge-status.json', {'status': 'running'})
        with patch.dict('os.environ', {'BENCHMARK_JUDGE_API_KEY': 'secret-fixture'}), \
             patch('benchmark.judge.request_judgment') as api:
            self.assertEqual(self.execute(resume=True, execute=True)['status'], 'judge_failed')
            api.assert_not_called()

    def test_successful_review_edit_is_rejected_on_resume(self):
        with patch('benchmark.judge.request_judgment', side_effect=self.judgment), \
             patch.dict('os.environ', {'BENCHMARK_JUDGE_API_KEY': 'secret-fixture'}):
            self.execute(execute=True)
            path = next((self.out / 'reviews').rglob('judgment.json'))
            judgment = read(path)
            judgment['nodes'][0]['reason'] = 'Manual change'
            write(path, judgment)
            with self.assertRaisesRegex(ValueError, 'output changed'):
                self.execute(resume=True, execute=True)

    def test_leaderboard_empty_update_and_preserves_user_edits(self):
        out = self.base / 'board'
        self.assertEqual(export_leaderboard(ROOT, [], out)['configurations'], 0)
        self.assertEqual(read(out / 'leaderboard.json')['expected_tasks'], SINGLE_TASKS)
        export_leaderboard(ROOT, [self.batch], out, update=True, samples=10)
        row = read(out / 'leaderboard.json')['rows'][0]
        self.assertIsNone(row['mean_score'])
        self.assertEqual(row['missing_reviews'], 2)
        with (out / 'index.html').open('a') as stream:
            stream.write('user edit')
        with self.assertRaisesRegex(ValueError, 'changed'):
            export_leaderboard(ROOT, [], out, update=True)
        self.assertTrue((out / 'index.html').read_text().endswith('user edit'))

    def test_leaderboard_rejects_tampered_score_and_duplicate_attempts(self):
        scored = self.base / 'scored'
        apply_reviews(ROOT, self.batch, self.fill_reviews(self.batch), scored)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            export_leaderboard(ROOT, [scored, scored], self.base / 'board', samples=10)
        score_path = batch_runs(scored)[1][0] / 'score.json'
        result = read(score_path); result['score'] = 99
        write(score_path, result)
        with self.assertRaises(ValueError):
            export_leaderboard(ROOT, [scored], self.base / 'board', samples=10)
        self.assertFalse((self.base / 'board').exists())

    def test_output_escapes_script_and_omits_answers_and_paths(self):
        scored = self.base / 'scored'
        apply_reviews(ROOT, self.batch, self.fill_reviews(self.batch), scored)
        out = self.base / 'board'
        export_leaderboard(ROOT, [scored], out, samples=10)
        page = (out / 'index.html').read_text()
        self.assertNotIn(str(self.base), page)
        self.assertNotIn('Synthetic answer only.', page)
        self.assertNotIn('rubric.json', page)
        # An untrusted model/config label must not break out of the inline JSON.
        import benchmark.leaderboard as module
        original = module.report_statistics
        def report(*args, **kwargs):
            result = original(*args, **kwargs)
            path = Path(args[2]) / 'statistics.json'
            value = read(path); value['rows'][0]['model'] = '</script><script>alert(1)</script>'
            write(path, value)
            return result
        with patch('benchmark.leaderboard.report_statistics', side_effect=report):
            export_leaderboard(ROOT, [scored], out, update=True, samples=10)
        page = (out / 'index.html').read_text()
        self.assertNotIn('</script><script>alert', page)
        self.assertIn('\\u003c/script>', page)

    def test_transport_rejects_response_model_and_truncation(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(payload).encode()
        payload = {'model': 'other', 'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
        with patch('benchmark.judge.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with self.assertRaisesRegex(ValueError, 'model differs'):
                request_judgment(self.config, [], 'secret-fixture')
            payload['model'] = 'fixture-model'; payload['choices'][0]['finish_reason'] = 'length'
            with self.assertRaisesRegex(ValueError, 'truncated'):
                request_judgment(self.config, [], 'secret-fixture')
            payload['choices'][0]['finish_reason'] = 'stop'
            self.assertEqual(request_judgment(self.config, [], 'secret-fixture'), {})

    def test_configuration_disallows_inline_secret_extra_parameters_and_redirect_credentials(self):
        for extra in ({'api_key': 'secret'}, {'parameters': {'model': 'other', 'max_tokens': 100}},
                      {'endpoint': 'https://user:secret@example.com/completions'}):
            write(self.config_path, dict(self.config, **extra))
            with self.assertRaises(ValueError):
                configuration(self.config_path)


if __name__ == '__main__':
    unittest.main()
