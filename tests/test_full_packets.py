"""Portable full-answer packets: synthetic native CLI and Judge; no paid calls."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from benchmark import full_batch, full_packet, full_packet_io as transport, full_packet_runner as runner
from benchmark.core import read, write, file_hash
from benchmark.isolation import supported
from benchmark.judge import judge_batch
from test_full_batch import ROOT, SINGLE_TASKS, agent
import test_full_batch as fixtures


def write_selection(path):
    value = read(ROOT / 'config/selections/current.json')
    value['id'] = 'packet-test-selection'
    value['tasks'] = [t for t in value['tasks'] if t['task_id'] in ('BY-A-APPLY', 'N01-02')]
    value['task_ids'] = [t['task_id'] for t in value['tasks']]
    value.update(task_count=2, stage_count=3, case_group_count=2)
    write(path, value)


class PacketTests(unittest.TestCase):
    def test_cli_selection_packet_preview_and_conflicting_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); selection = base / 'selection.json'
            write_selection(selection)
            command = [sys.executable, '-m', 'benchmark', 'cli-package', '--suite', 'dev',
                       '--registry', str(ROOT / 'config/agents-cli.example.json'),
                       '--agent', 'gpt-6-astra', '--selection', str(selection)]
            result = subprocess.run(command + ['--out', str(base / 'package')], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(result.stdout)['packets'][0]
            self.assertEqual((packet['original_tasks'], packet['stages']), (2, 3))
            plan = read(base / 'package/organizer/gpt-6-astra/full-batch.json')
            self.assertEqual(len(plan['originals']), 2)
            self.assertEqual(plan['selection']['task_ids'], read(selection)['task_ids'])
            preview = subprocess.run([sys.executable, str(base / 'package/candidates/gpt-6-astra/run_agent.py')],
                                     cwd=base, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)['stages'], 3)
            for conflict in (['--task', 'BY-A-APPLY'], ['--include-episodes']):
                rejected = subprocess.run(command + conflict + ['--out', str(base / 'rejected')],
                                          cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse((base / 'rejected').exists())

    def test_complete_candidate_scope_and_standalone_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); package = base / 'package'
            result = full_packet.export_full(ROOT, ROOT / 'config/agents-cli.example.json', package,
                                             agent_ids=['claude-opus-5-5'])
            self.assertEqual((result['packets'][0]['units'], result['packets'][0]['stages']), (SINGLE_TASKS + 15, SINGLE_TASKS + 33))
            self.assertEqual((result['packets'][0]['original_tasks'], result['packets'][0]['derived_episodes']), (SINGLE_TASKS + 13, 2))
            with zipfile.ZipFile(result['packets'][0]['candidate_zip']) as z:
                names = z.namelist()
                self.assertFalse(any(s in n for n in names for s in ('rubric', 'reference', 'organizer', 'judge', 'scoring')))
                z.extractall(base / 'recipient')
            preview = subprocess.run(['python3', str(base / 'recipient/run_agent.py'), '--out', str(base / 'answers')],
                                     cwd=base, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(json.loads(preview.stdout)['stages'], SINGLE_TASKS + 33)
            self.assertFalse((base / 'answers').exists())
            manifest, registry = runner.load_packet(base / 'recipient')
            self.assertEqual(registry['agents'][0]['binary'], 'claude')
            staged = next(u for u in manifest['units'] if u['id'] == 'N01-02')
            first = (base / 'recipient' / runner.prompt_name(staged, staged['stages'][0])).read_text()
            second = (base / 'recipient' / runner.prompt_name(staged, staged['stages'][1])).read_text()
            self.assertNotIn('标注过工程场景图像', first); self.assertIn('标注过工程场景图像', second)

    def test_continuation_capture_uses_previous_cumulative_usage(self):
        from benchmark.native_runtime import parse_capture
        from test_antigravity import events
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); stream = events()
            stream[-1]['result'].update(num_turns=2, usage={'input_tokens': 25, 'output_tokens': 8})
            (base / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in stream))
            (base / 'final-response.md').write_text('Final answer')
            (base / 'answer.md').write_bytes(b'Delivered answer\r\n')
            evidence = {'answer_capture': {'source': 'workspace_file', 'answer_sha256': file_hash(base / 'answer.md'),
                                           'final_response_sha256': file_hash(base / 'final-response.md')}}
            result = parse_capture('antigravity', base / 'events.jsonl', base / 'answer.md', evidence,
                                   previous_antigravity={'num_turns': 1, 'usage': {'input_tokens': 10, 'output_tokens': 3}})
            self.assertEqual(result['usage']['input_tokens'], 15)
            self.assertEqual(result['usage']['output_tokens'], 5)
            self.assertEqual(result['answer'], 'Delivered answer\n')

    def test_transport_rejects_unsafe_zip_and_undeclared_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); data = base / 'data'; data.mkdir(); (data / 'answer.md').write_text('answer')
            archive = base / 'valid.zip'
            transport.export_archive(data, ['answer.md'], archive, 'packet')
            transport.extract_archive(archive, base / 'extracted')
            transport.validate_directory(base / 'extracted')
            (data / 'auth.json').write_text('must not transport')
            with self.assertRaisesRegex(ValueError, 'Undeclared'):
                transport.validate_directory(data)
            for number, name in enumerate(('../outside', '/absolute', 'a\\b', 'a/../b', 'a//b', 'C:/auth')):
                path = base / f'bad{number}.zip'
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr(name, 'bad')
                with self.assertRaises(ValueError):
                    transport.extract_archive(path, base / f'out{number}')
            linkzip = base / 'link.zip'
            with zipfile.ZipFile(linkzip, 'w') as z:
                info = zipfile.ZipInfo('link'); info.create_system = 3; info.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(info, '/tmp/outside')
            with self.assertRaises(ValueError):
                transport.extract_archive(linkzip, base / 'links')
            (data / 'answer.md').write_text('tampered')
            (data / 'auth.json').unlink()
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                transport.validate_directory(data)


@unittest.skipUnless(supported(), 'requires macOS Seatbelt isolation')
class FullReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.binary = self.base / 'fake-claude'
        self.a = agent('claude', self.binary); self.registry = self.base / 'registry.json'
        write(self.registry, {'schema': 'native-cli-batch.1', 'repeats': 1, 'timeout_seconds': 10, 'agents': [self.a]})
        self.package = self.base / 'package'
        full_packet.export_full(ROOT, self.registry, self.package, task_ids=['BY-A-APPLY', 'N01-02'], episode_ids=['LONG-C'])
        with zipfile.ZipFile(self.package / 'candidates/fixture-claude.zip') as z:
            z.extractall(self.base / 'recipient')
        write(self.base / 'recipient/.local/registry.json', read(self.registry))
        self.candidate = self.base / 'recipient'; self.answers = self.base / 'answers'; self.batch = self.base / 'imported'
        future = self.candidate / 'questions/staged/N01-02/S1.md'
        # Actual sandbox subprocess: future questions and organizer material must be unreadable.
        self.binary.write_text('''#!/bin/sh
set -eu
case "$*" in
 --version) printf 'fixture\\n'; exit 0;;
 --help) printf '%s\\n' '--model --print --output-format --permission-mode --no-session-persistence --effort --resume'; exit 0;;
esac
prompt=$(cat)
if cat "''' + str(future) + '''" >/dev/null 2>&1; then exit 91; fi
if cat "''' + str(ROOT / 'data/dev/manifest.json') + '''" >/dev/null 2>&1; then exit 92; fi
mkdir -p artifacts
persist="$HOME/.claude/projects/work/fixture.jsonl"
if [ -f "$persist" ]; then
 session=$(cat artifacts/session)
 case "$*" in *--resume*"$session"*) ;; *) exit 82;; esac
 test "$(cat artifacts/cwd)" = "$PWD"
else
 session=$(basename "$(dirname "$PWD")")
 printf '%s' "$session" > artifacts/session
 printf '%s' "$PWD" > artifacts/cwd
 mkdir -p "$(dirname "$persist")"
 printf '{}' > "$persist"
fi
printf record > artifacts/action-record.md
printf 'Fixture answer\\r\\n' > answer.md
printf '{"type":"result","subtype":"success","is_error":false,"result":"Fixture answer","session_id":"%s"}\\n' "$session"
'''); self.binary.chmod(0o700)
        for target, fn in [
            ('benchmark.native_sessions.check', lambda *a: {}),
            ('benchmark.cli_adapters.check_agent', lambda *a, **k: {'id': self.a['id'], 'issues': [], 'binary': str(self.binary)}),
            ('benchmark.cli_setup.credential_environment', lambda *a: nullcontext({self.a['id']: None}))]:
            p = patch(target, side_effect=fn); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'BENCH_FIXTURE_KEY': 'synthetic', 'BENCHMARK_JUDGE_API_KEY': 'synthetic'})
        p.start(); self.addCleanup(p.stop)

    def execute(self, **kwargs):
        return runner.run_packet(self.candidate, self.answers, execute=True, **kwargs)

    def test_selected_scope_survives_execution_import_and_scoring(self):
        selection = self.base / 'selection.json'; write_selection(selection)
        package = self.base / 'selected-package'
        full_packet.export_full(ROOT, self.registry, package, include_episodes=False,
                                selection_path=selection)
        self.candidate = package / 'candidates/fixture-claude'
        write(self.candidate / '.local/registry.json', read(self.registry))
        returned = self.execute()
        self.assertEqual((returned['status'], returned['completed']), ('completed', 2))
        full_packet.import_full(ROOT, package, Path(returned['results_zip']), self.batch)
        full_batch.verify(ROOT, self.batch)
        config = read(ROOT / 'config/judge.example.json')
        config.update(provider='fixture', model='fixture', expected_response_model='fixture',
                      endpoint='https://judge.example.org/v1/chat/completions')
        config_path = self.base / 'judge-config.json'; write(config_path, config)
        with patch('benchmark.judge.request_judgment', side_effect=fixtures.FullWorkflowTests.judgment):
            result = judge_batch(ROOT, self.batch, config_path, self.base / 'judged', execute=True)
            self.assertEqual(result['status'], 'judge_completed', result)
        board = read(self.base / 'judged/leaderboard/leaderboard.json')
        self.assertEqual((board['expected_tasks'], board['expected_episodes']), (2, 0))
        self.assertEqual(board['rows'][0]['scored_attempts'], 2)
        self.assertIsNotNone(board['rows'][0]['mean_score'])

    def test_community_submission_preserves_states_and_redacts_public_copy(self):
        from scripts.community_run import export_record
        self.binary.write_text(self.binary.read_text().replace(
            'prompt=$(cat)', 'prompt=$(cat)\necho "person@example.org" >&2'))
        returned = self.execute(max_stages=1)
        archive = Path(returned['results_zip']); original = file_hash(archive)
        records = self.base / 'public-records'
        with patch('benchmark.native_runtime.capture') as calls:
            result = export_record(ROOT, self.package, archive, records)
            calls.assert_not_called()
        dest = Path(result['record']); record = read(dest / 'record.json')
        self.assertEqual(record['execution']['completed'], 1)
        self.assertEqual(record['execution']['expected'], 3)
        self.assertEqual(record['review_status'], 'submitted_unreviewed')
        self.assertFalse(record['scored']); self.assertFalse(record['isolation_verified'])
        self.assertEqual(file_hash(archive), original)
        names = [p.name for p in dest.rglob('*') if p.is_file()]
        self.assertNotIn('checkpoint.json', names)
        self.assertNotIn('auth.json', names)
        for private_name in ('stderr.log', 'events.jsonl', 'trace.jsonl', 'execution.json'):
            self.assertNotIn(private_name, names)
        self.assertTrue(list(dest.rglob('answer.md')))
        self.assertTrue(all('person@example.org' not in p.read_text()
                            for p in dest.rglob('*') if p.is_file()))
        self.assertTrue(export_record(ROOT, self.package, archive, records)['already_prepared'])
        next(dest.rglob('answer.md')).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Existing public record changed'):
            export_record(ROOT, self.package, archive, records)

    def test_pause_resume_import_judge_and_tamper(self):
        first = self.execute(max_stages=2)
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(sum(r['completed_stages'] for r in first['records']), 2)
        final = self.execute(resume=True)
        self.assertEqual((final['status'], final['completed']), ('completed', 3))
        with patch('benchmark.native_runtime.capture') as calls:
            self.assertEqual(self.execute(resume=True)['status'], 'completed'); calls.assert_not_called()
        imported = full_packet.import_full(ROOT, self.package, Path(final['results_zip']), self.batch)
        self.assertEqual(imported['status'], 'completed'); self.assertTrue(imported['review_only'])
        self.assertFalse(imported['isolation_verified']); full_batch.verify(ROOT, self.batch)
        answers = list(self.batch.rglob('answer.md'))
        self.assertEqual(len(answers), 6)
        self.assertTrue(all(p.read_bytes() == b'Fixture answer\r\n' for p in answers))
        for state in self.batch.glob('staged/**/run.json'):
            if 'snapshot' in read(state):
                self.assertFalse(read(state)['isolation_verified'])
        config = read(ROOT / 'config/judge.example.json')
        config.update(provider='fixture', model='fixture', expected_response_model='fixture', endpoint='https://judge.example.org/v1/chat/completions')
        config_path = self.base / 'judge-config.json'; write(config_path, config)
        with patch('benchmark.judge.request_judgment', side_effect=fixtures.FullWorkflowTests.judgment) as api:
            result = judge_batch(ROOT, self.batch, config_path, self.base / 'judged', execute=True)
            self.assertEqual(result['status'], 'judge_completed', result)
            self.assertEqual(api.call_count, 5)
        board = read(self.base / 'judged/leaderboard/leaderboard.json')
        self.assertEqual((board['expected_tasks'], board['expected_episodes']), (SINGLE_TASKS + 13, 2))
        self.assertEqual(board['rows'][0]['scored_attempts'], 2)
        self.assertIsNone(board['rows'][0]['mean_score']); self.assertFalse(board['official'])
        answers[0].write_text('changed')
        with self.assertRaisesRegex(ValueError, 'capture changed'):
            full_batch.verify(ROOT, self.batch)

    def test_standalone_extracted_packet_executes_without_repository(self):
        value = subprocess.run([sys.executable, str(self.candidate / 'run_agent.py'), '--execute',
                                '--out', str(self.answers)], cwd=self.candidate,
                               env=dict(os.environ, PYTHONPATH=''), capture_output=True, text=True)
        self.assertEqual(value.returncode, 0, value.stderr + value.stdout)
        returned = json.loads(value.stdout)
        self.assertEqual(returned['completed'], 3)
        self.assertEqual(full_packet.import_full(ROOT, self.package, Path(returned['results_zip']), self.batch)['completed'], 3)

    def test_tampered_resume_and_import_rejected_before_model_or_output(self):
        self.execute(max_stages=2)
        stage = self.answers / 'runs/staged/N01-02/r1/stages/S0'
        checkpoint = read(stage / 'checkpoint.json'); checkpoint['session_id'] = 'forged'; write(stage / 'checkpoint.json', checkpoint)
        with patch('benchmark.native_runtime.capture') as calls:
            with self.assertRaises(ValueError):
                self.execute(resume=True)
            calls.assert_not_called()
        with self.assertRaises(ValueError):
            full_packet.import_full(ROOT, self.package, self.answers, self.batch)
        self.assertFalse(self.batch.exists())

    def test_resealed_invalid_returns_rejected_transactionally(self):
        self.execute()
        mutations = {
            'missing_job': lambda d: self.edit(d / 'batch.json', lambda v: v['jobs'].pop('runs/staged/N01-02/r1')),
            'stage_order': lambda d: self.edit(d / 'batch.json', lambda v: v['jobs']['runs/staged/N01-02/r1'].update(stages=['S1', 'S0'])),
            'checkpoint_chain': lambda d: self.edit(d / 'runs/staged/N01-02/r1/stages/S1/run.json', lambda v: v.update(previous_checkpoint_sha256='0' * 64)),
            'budget': lambda d: self.edit(d / 'runs/staged/N01-02/r1/stages/S1/run.json', lambda v: v.update(allocated_seconds=1)),
            'stage_identity': lambda d: self.edit(d / 'runs/staged/N01-02/r1/stages/S1/run.json', lambda v: v.update(stage_id='S0')),
            'credential': lambda d: (d / 'auth.json').write_text('unwanted'),
            'model_evidence': lambda d: self.change_evidence(d),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                returned = self.base / label; shutil.copytree(self.answers, returned)
                mutate(returned)
                names = [p.relative_to(returned).as_posix() for p in returned.rglob('*') if p.is_file() and p.name != transport.MANIFEST]
                transport.export_archive(returned, names, self.base / (label + '.zip'), read(returned / 'packet.json')['packet_id'])
                destination = self.base / ('reject-' + label)
                with self.assertRaises(ValueError):
                    full_packet.import_full(ROOT, self.package, self.base / (label + '.zip'), destination)
                self.assertFalse(destination.exists())

    @staticmethod
    def edit(path, mutate):
        value = read(path); mutate(value); write(path, value)

    def change_evidence(self, directory):
        stage = directory / 'runs/staged/N01-02/r1/stages/S1'
        self.edit(stage / 'execution.json', lambda v: v.update(requested_model='another-model'))
        self.edit(stage / 'run.json', lambda v: v['files'].update({'execution.json': file_hash(stage / 'execution.json')}))

    def test_partial_return_review_only_and_not_run_jobs_preserved(self):
        result = self.execute(max_stages=2)
        imported = full_packet.import_full(ROOT, self.package, Path(result['results_zip']), self.batch)
        self.assertEqual(imported['statuses'], {'completed': 1, 'partial': 1, 'prepared': 1})
        full_batch.verify(ROOT, self.batch)
        with patch('benchmark.cli_setup.credential_environment') as credentials:
            with self.assertRaisesRegex(ValueError, 'review-only'):
                full_batch.run(ROOT, self.batch / 'agents.frozen.json', self.batch,
                    task_ids=['BY-A-APPLY', 'N01-02'], episode_ids=['LONG-C'],
                    include_staged=True, include_episodes=True, execute=True, resume=True)
            credentials.assert_not_called()
        with patch('benchmark.native_runtime.capture') as calls:
            from benchmark.episode_runner import run_episode
            with self.assertRaisesRegex(ValueError, 'review-only'):
                run_episode(ROOT, self.batch / 'agents.frozen.json', 'staged:N01-02', self.a['id'],
                            self.batch / 'staged/fixture-claude/N01-02/r1', execute=True, resume=True)
            calls.assert_not_called()

    def test_failed_attempt_preserved_and_remaining_tasks_run(self):
        self.binary.write_text('#!/bin/sh\ncat >/dev/null\nexit 7\n'); self.binary.chmod(0o700)
        result = self.execute()
        self.assertEqual(result['completed'], 0)
        self.assertEqual([r['status'] for r in result['records']], ['failed'] * 3)
        with patch('benchmark.native_runtime.capture') as calls:
            self.execute(resume=True); calls.assert_not_called()
        imported = full_packet.import_full(ROOT, self.package, Path(result['results_zip']), self.batch)
        self.assertEqual(imported['completed'], 0); full_batch.verify(ROOT, self.batch)


if __name__ == '__main__':
    unittest.main()
