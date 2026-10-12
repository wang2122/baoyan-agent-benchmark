"""Same-session lifecycle with a synthetic executable; never calls a model."""
import base64
import copy
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmark.core import digest, read, write
from benchmark.reproduction import suite_rows
from benchmark.episode_runner import run_episode
from benchmark.episode_state import load_checkpoint, restore_checkpoint, save_checkpoint, valid_name
from benchmark.isolation import supported
from benchmark.longitudinal import SCHEMA, JUDGE_VERSION, apply_review, export_review, load_run, prepare, record_answer

ROOT = Path(__file__).resolve().parents[1]


class CheckpointTests(unittest.TestCase):
    def test_only_artifacts_and_session_logs_survive(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            box = SimpleNamespace(home=base / 'home', work=base / 'work')
            for name, content in [('home/.codex/sessions/rollout.jsonl', '{}'),
                                  ('home/.codex/auth.json', 'secret'), ('home/history', 'private'),
                                  ('work/artifacts/action-record.md', '行动记录\r\n'), ('work/answer.md', 'answer')]:
                p = base / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content.encode())
            save_checkpoint(box, 'session', base / 'checkpoint.json')
            value = load_checkpoint(base / 'checkpoint.json')
            self.assertEqual(set(value['files']), {'sessions/rollout.jsonl', 'artifacts/action-record.md'})
            restored = SimpleNamespace(home=base / 'new-home', work=base / 'new-work')
            self.assertEqual(restore_checkpoint(restored, base / 'checkpoint.json'), 'session')
            self.assertEqual((restored.work / 'artifacts/action-record.md').read_bytes(), '行动记录\r\n'.encode())
            with self.assertRaises(ValueError):
                restore_checkpoint(restored, base / 'checkpoint.json')
            value['files']['artifacts/action-record.md']['sha256'] = 'changed'
            write(base / 'bad.json', value)
            with self.assertRaises(ValueError):
                load_checkpoint(base / 'bad.json')
            (box.work / 'artifacts/link').symlink_to(box.home / '.codex/auth.json')
            with self.assertRaises(ValueError):
                save_checkpoint(box, 'session', base / 'linked.json')

    def test_invalid_paths_are_rejected(self):
        for path in ('../auth.json', '/artifacts/a', 'artifacts/../auth.json', 'sessions/auth.json',
                     'artifacts/a\\b', 'artifacts/C:a', 'artifacts//a', 'auth.json'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                valid_name(path)


@unittest.skipUnless(supported(), 'macOS Seatbelt')
class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve(); self.out = self.base / 'episode'
        task, rubric = suite_rows(ROOT, 'public-smoke')[0]
        self.tid = task['id']
        self.snap = {'schema_version': SCHEMA, 'version': 'synthetic',
                     'episode': {'id': 'FIXTURE', 'case_group_id': 'synthetic'},
                     'sources': {'dev/fixture': {'task': task, 'rubric': rubric}},
                     'stages': [{'id': 'S0', 'source': 'dev/fixture', 'source_stage': None, 'prompt': 'CURRENT_ONLY'},
                                {'id': 'S1', 'source': 'dev/fixture', 'source_stage': None, 'prompt': 'FUTURE_ONLY'}],
                     'scoring_policy': (ROOT / 'docs/scoring.md').read_text()}
        self.binary = self.base / 'fake-codex'
        self.script()
        self.registry = self.base / 'registry.json'
        write(self.registry, {'schema': 'native-cli-batch.1', 'repeats': 1, 'timeout_seconds': 5,
            'agents': [{'id': 'fixture', 'cli': 'codex', 'binary': str(self.binary), 'cli_version': 'fixture',
                        'model': 'fixture-model', 'auth': 'api', 'api_key_env': 'EPISODE_TEST_KEY',
                        'base_url': 'https://api.example.com/v1', 'reasoning_effort': 'high', 'web_search': 'disabled'}]})
        for target, value in [('benchmark.episode_runner.snapshot', lambda *a: copy.deepcopy(self.snap)),
                              ('benchmark.episode_runner.check_continuation', lambda *a: None),
                              ('benchmark.episode_runner.cli_adapters.check_agent', lambda *a, **k: {'issues': [], 'binary': str(self.binary)}),
                              ('benchmark.episode_runner.cli_setup.credential_environment', lambda *a: nullcontext({'fixture': None}))]:
            p = patch(target, side_effect=value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'EPISODE_TEST_KEY': 'synthetic-key'}); p.start(); self.addCleanup(p.stop)

    def script(self, extra=''):
        self.binary.write_text('''#!/bin/sh
set -eu
prompt=$(cat)
case "$*" in *--ephemeral*) exit 81;; esac
if [ -f "$HOME/.codex/sessions/fixture.jsonl" ]; then
 case "$*" in *"exec resume"*"fixture-session"*) ;; *) exit 82;; esac
 test "$(cat artifacts/action-record.md)" = stage-zero
 test ! -e "$HOME/unrelated-history"
 case "$prompt" in *FUTURE_ONLY*) ;; *) exit 83;; esac
 printf stage-one > artifacts/action-record.md
else
 case "$prompt" in *FUTURE_ONLY*) exit 84;; esac
 printf stage-zero > artifacts/action-record.md
 mkdir -p "$HOME/.codex/sessions"
 printf '{}' > "$HOME/.codex/sessions/fixture.jsonl"
 printf private > "$HOME/unrelated-history"
fi
''' + extra + '''
printf 'Fixture answer\\r\\n' > answer.md
while [ "$#" -gt 0 ]; do
 if [ "$1" = "--output-last-message" ]; then shift; printf 'Fixture closing reply\\r\\n' > "$1"; fi
 shift
done
printf '%s\\n' '{"type":"thread.started","thread_id":"fixture-session","model":"fixture-model"}'
printf '%s\\n' '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
''')
        self.binary.chmod(0o700)

    def run_episode(self, **kwargs):
        return run_episode(ROOT, self.registry, 'FIXTURE', 'fixture', self.out, **kwargs)

    def test_preview_has_no_output_and_resume_requires_execution(self):
        self.assertEqual(self.run_episode()['status'], 'preview_no_model_called')
        self.assertFalse(self.out.exists())
        with self.assertRaises(ValueError):
            self.run_episode(resume=True)

    def test_manual_defaults_unchanged_and_cannot_replace_native_answers(self):
        with patch('benchmark.longitudinal.snapshot', return_value=self.snap):
            prepare(ROOT, 'FIXTURE', self.base / 'manual', self.base / 'work', 'product', 'model', 'chat')
            self.assertNotIn('aggregation', load_run(self.base / 'manual'))
            prepare(ROOT, 'FIXTURE', self.base / 'manual-mean', self.base / 'work-mean', 'product', 'model', 'chat',
                    aggregation='unique_source_macro_mean.1')
            self.assertEqual(load_run(self.base / 'manual-mean')['aggregation'], 'unique_source_macro_mean.1')
        self.run_episode(execute=True, max_stages=1)
        with self.assertRaisesRegex(ValueError, 'cannot be replaced'):
            record_answer(self.out, 'S1', self.base / 'anything', self.base / 'anything')

    def test_unit_review_never_receives_later_artifacts(self):
        second = copy.deepcopy(self.snap['sources']['dev/fixture'])
        second['rubric']['id'] = 'SECOND-FIXTURE'
        self.snap['sources']['dev/second'] = second
        self.snap['stages'][1]['source'] = 'dev/second'
        self.run_episode(execute=True)
        review = self.base / 'review'; export_review(self.out, review)
        self.assertEqual(set(read(review / 'units' / self.tid / 'artifacts.json')), {'S0'})
        self.assertEqual(set(read(review / 'units/SECOND-FIXTURE/artifacts.json')), {'S0', 'S1'})

    def test_same_session_artifacts_original_bytes_resume_and_scoring(self):
        self.assertEqual(self.run_episode(execute=True, max_stages=1)['status'], 'partial')
        self.assertFalse((self.out / 'visible/S1-prompt.md').exists())
        first = (self.out / 'stages/S0/answer.md').read_bytes()
        self.assertEqual(first, b'Fixture answer\r\n')
        with self.assertRaisesRegex(ValueError, 'Complete all stages'):
            export_review(self.out, self.base / 'premature')
        self.assertEqual(self.run_episode(execute=True, resume=True)['status'], 'completed')
        self.assertEqual((self.out / 'stages/S0/answer.md').read_bytes(), first)
        self.assertEqual(load_run(self.out)['conversation_id'], 'fixture-session')
        self.assertEqual(self.run_episode(execute=True, resume=True)['completed_stages'], 2)
        review = self.base / 'review'; export_review(self.out, review)
        artifacts = read(review / 'artifacts.json')
        self.assertEqual(base64.b64decode(artifacts['S0']['artifacts/action-record.md']['base64']), b'stage-zero')
        self.assertNotIn('sessions', str(artifacts))
        judgment = read(review / 'judgment.json'); judgment['reviewer'] = 'fixture-reviewer'
        for unit in judgment['units'].values():
            unit.update(reviewer='fixture-reviewer', judge_version=JUDGE_VERSION, candidates_reviewed=True)
            for row in unit['nodes']: row.update(grade=0, reason='Synthetic omission')
            for row in unit['coverage']: row.update(status='miss', reason='Synthetic omission')
        for row in judgment['chain']:
            row.update(status='fail', reason='Synthetic omission')
        judgment['chain'][0].update(status='pass', reason='Synthetic file continuation', evidence=[
            {'stage_id': 'S0', 'artifact_path': 'artifacts/action-record.md', 'answer_quote': 'stage-zero'},
            {'stage_id': 'S1', 'artifact_path': 'artifacts/action-record.md', 'answer_quote': 'stage-one'}])
        write(review / 'judgment.json', judgment)
        result = apply_review(self.out, review / 'judgment.json', self.base / 'score.json')
        self.assertEqual(result['episode_score'], 0)
        self.assertEqual(len(read(self.base / 'score.json')['unit_scores']), 1)
        judgment['chain'][0]['status'] = 'pending'; write(review / 'judgment.json', judgment)
        self.assertIsNone(apply_review(self.out, review / 'judgment.json', self.base / 'pending.json')['episode_score'])
        (self.out / 'stages/S0/checkpoint.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'capture changed'):
            self.run_episode(execute=True, resume=True)
        with self.assertRaisesRegex(ValueError, 'capture changed'):
            export_review(self.out, self.base / 'tampered-review')

    def test_missing_artifact_is_terminal_without_retry_or_next_stage(self):
        self.script('rm artifacts/action-record.md\n')
        result = self.run_episode(execute=True)
        self.assertEqual(result['status'], 'invalid_output')
        self.assertEqual(result['completed_stages'], 0)
        self.assertFalse((self.out / 'visible/S1-prompt.md').exists())
        self.assertEqual(self.run_episode(execute=True, resume=True)['status'], 'invalid_output')
        self.assertEqual(len(read(self.out / 'native-episode.json')['attempts']), 1)

    def test_changed_contract_and_uncertain_capture_refuse_resume(self):
        self.run_episode(execute=True, max_stages=1)
        state = read(self.out / 'run.json')
        changed = copy.deepcopy(state); changed['aggregation'] = 'another-method'
        write(self.out / 'run.json', changed)
        with self.assertRaisesRegex(ValueError, 'state differs'):
            self.run_episode(execute=True, resume=True)
        write(self.out / 'run.json', state)
        registry = read(self.registry); registry['timeout_seconds'] = 6; write(self.registry, registry)
        with self.assertRaisesRegex(ValueError, 'recipe, sources, budgets or code changed'):
            self.run_episode(execute=True, resume=True)
        registry['timeout_seconds'] = 5; write(self.registry, registry)
        execution = read(self.out / 'native-episode.json'); execution['attempts'][0]['status'] = 'running'
        write(self.out / 'native-episode.json', execution)
        with self.assertRaisesRegex(ValueError, 'uncertain outcome'):
            self.run_episode(execute=True, resume=True)


if __name__ == '__main__':
    unittest.main()
