"""Offline native CLI capture, portable round trips and real review/statistics contracts."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from benchmark import cli_adapters, cli_batch, cli_packet_runner, native
from benchmark.isolation import supported
from benchmark.__main__ import main
from benchmark.calibration import compare_reviews
from benchmark.core import candidate_prompt, digest, file_hash, read, write
from benchmark.reproduction import batch_runs, suite_rows
from benchmark.scoring import judgment_template
from benchmark.statistics import report_statistics
from benchmark.study import check_study, prepare_study
from benchmark.submissions import apply_reviews, export_submission, review_batch, verify_results

ROOT = Path(__file__).resolve().parents[1]
ANSWER = 'Synthetic fixture answer.'


def fake_events(cli, model):
    if cli == 'codex':
        return [{'type': 'thread.started', 'thread_id': 'SESSION', 'model': model},
                {'type': 'turn.completed', 'usage': {'input_tokens': 8, 'output_tokens': 3}}]
    if cli in ('claude', 'qwen'):
        return [{'type': 'assistant', 'message': {'model': model}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'SESSION',
                 'result': ANSWER, 'usage': {'input_tokens': 8, 'output_tokens': 3,
                 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}]
    if cli == 'gemini':
        return [{'type': 'init', 'session_id': 'SESSION', 'model': model},
                {'type': 'message', 'role': 'assistant', 'content': ANSWER},
                {'type': 'result', 'status': 'success', 'stats': {'input_tokens': 8,
                 'output_tokens': 3, 'models': {model: {}}}}]
    if cli == 'antigravity':
        return [{'event': 'init', 'conversation_id': 'SESSION', 'init': {'model': model}},
                {'event': 'result', 'result': {'conversation_id': 'SESSION', 'status': 'SUCCESS',
                 'response': ANSWER, 'num_turns': 1, 'usage': {'input_tokens': 8, 'output_tokens': 3}}}]
    if cli == 'kimi':
        return [{'role': 'assistant', 'content': ANSWER},
                {'role': 'meta', 'type': 'session.resume_hint', 'session_id': 'SESSION'}]
    return [{'type': 'session', 'sessionId': 'SESSION'},
            {'type': 'status', 'phase': 'step_end', 'usage': {'inputTokens': 8, 'outputTokens': 3}},
            {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}},
            {'type': 'final', 'text': ANSWER}]


@unittest.skipUnless(supported(), 'Native execution requires macOS Seatbelt')
class NativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.registry = read(ROOT / 'config/agents-cli.example.json')
        self.registry.update(repeats=2, timeout_seconds=3)
        for a in self.registry['agents']:
            a['cli_version'] = 'fixture-1'
            if a['cli'] == 'antigravity':
                # Synthetic API-mode adapter coverage; never claim keyring subscription acceptance.
                a.update(auth='api', api_key_env='NATIVE_FIXTURE_KEY',
                         base_url='https://generativelanguage.googleapis.com')
            if 'api_key_env' in a:
                a['api_key_env'] = 'NATIVE_FIXTURE_KEY'
            a['binary'] = str(self.fake(a))
        self.registry_path = self.base / 'registry.json'
        write(self.registry_path, self.registry)
        self.addCleanup(patch.stopall)
        host = self.base / 'host'
        for name in ('.codex', '.gemini', '.kimi-code/credentials'):
            (host / name).mkdir(parents=True, exist_ok=True)
        write(host / '.codex/auth.json', {'auth_mode': 'chatgpt', 'tokens': {'access_token': 'fixture-only'}})
        write(host / '.gemini/oauth_creds.json', {'access_token': 'fixture-only'})
        write(host / '.kimi-code/credentials/kimi-code.json', {'access_token': 'fixture-only'})
        kimi_model = next(a['model'] for a in self.registry['agents'] if a['cli'] == 'kimi')
        (host / '.kimi-code/config.toml').write_text(f'''
[providers."managed:kimi-code"]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
[providers."managed:kimi-code".oauth]
storage = "file"
key = "oauth/kimi-code"
[models."fixture-kimi"]
provider = "managed:kimi-code"
model = {json.dumps(kimi_model)}
max_context_size = 1048576
''')
        patch.dict(os.environ, {'NATIVE_FIXTURE_KEY': 'fixture-not-a-real-secret',
            'CLAUDE_CODE_OAUTH_TOKEN': 'fixture-subscription', 'CODEX_HOME': str(host / '.codex'),
            'KIMI_CODE_HOME': str(host / '.kimi-code'), 'BENCH_GEMINI_AUTH_DIR': str(host / '.gemini')}).start()
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.rows = suite_rows(ROOT, 'public-smoke')

    def fake(self, a, mode='success', final_answer=ANSWER):
        path = self.base / (a['id'] + '-fake')
        events = fake_events(a['cli'], 'unexpected-model' if mode == 'mismatch' else a['model'])
        flags = ' '.join(set(cli_adapters.REQUIRED_FLAGS[a['cli']]) | {'--output-last-message'})
        payload = '\n'.join(json.dumps(e) for e in events).replace(ANSWER, final_answer)
        path.write_text('#!/bin/sh\n' +
            'if [ "$1" = "--version" ]; then echo fixture-1; exit 0; fi\n' +
            'for arg in "$@"; do if [ "$arg" = "--help" ]; then echo ' + shlex.quote(flags) + '; exit 0; fi; done\n' +
            ('sleep 5\n' if mode == 'timeout' else '') +
            ('echo not-json; exit 0\n' if mode == 'invalid' else '') +
            'printf %s ' + shlex.quote(ANSWER) + ' > answer.md\n' +
            'while [ "$#" -gt 0 ]; do if [ "$1" = "--output-last-message" ]; then shift; printf %s ' +
            shlex.quote(final_answer) + ' > "$1"; fi; shift; done\n' +
            'session=$(/usr/bin/uuidgen)\n' +
            'printf "%s\\n" ' + shlex.quote(payload) + ' | /usr/bin/sed "s/SESSION/$session/g"\n', encoding='utf-8')
        path.chmod(0o700)
        return path

    def run_direct(self, name, ids=None, tasks=None, resume=False):
        return native.run_native(ROOT, self.registry_path, self.base / name, 'public-smoke', ids, tasks,
                                 execute=True, resume=resume)

    def export(self, ids=None):
        return native.export_native(ROOT, self.registry_path, self.base / 'packets', 'public-smoke', ids)

    def answer_packet(self, agent):
        packet = self.base / 'packets/candidates' / agent['id']
        frozen = read(packet / 'registry.json')
        frozen['agents'][0]['binary'] = agent['binary']
        write(packet / '.local/registry.json', frozen)
        output = self.base / ('returned-' + agent['id'])
        result = cli_packet_runner.run_packet(packet, output, execute=True)
        return output, result

    def reviewed(self, batch, name):
        reviews = self.base / (name + '-packets')
        review_batch(ROOT, batch, reviews)
        mapping = {p['path']: p['packet_path'] for p in read(reviews / 'review-manifest.json')['packets']}
        for run in batch_runs(batch)[1]:
            meta = read(run / 'run.json')
            if meta['status'] != 'completed':
                continue
            folder = reviews / mapping[f"runs/{meta['adapter']}/{meta['task_id']}/r{meta['repeat']}"]
            judgment = judgment_template(read(run / 'task.json'), read(run / 'rubric.json'),
                                         (run / 'workspace/answer.md').read_text())
            judgment.update(reviewer=name, judge_version='evidence-review-v1', candidates_reviewed=True)
            for node in judgment['nodes']:
                node.update(grade=.5 if meta['repeat'] == 1 else 1, answer_quote=ANSWER,
                            reason='Synthetic fixture credit, not real model evaluation')
            write(folder / 'judgment.json', judgment)
            write(folder / 'review-settings.json', {'reviewer': name, 'reviewer_type': 'human',
                'judge_version': 'evidence-review-v1', 'provider': None, 'model': None,
                'model_version_evidence': None, 'reasoning_setting': None})
        scored = self.base / (name + '-scored')
        apply_reviews(ROOT, batch, reviews, scored)
        return scored

    def test_preview_never_inspects_cli_or_credentials_or_writes_output(self):
        with patch.object(cli_batch, 'inspect_agents', side_effect=AssertionError('CLI called')), \
                patch.object(native.cli_setup, 'credential_environment', side_effect=AssertionError('credential read')):
            result = native.run_native(ROOT, self.registry_path, self.base / 'preview', 'public-smoke')
        self.assertEqual(result['attempts'], 32)
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse((self.base / 'preview').exists())

    def test_all_native_clients_feed_review_statistics_and_calibration(self):
        result = self.run_direct('direct')
        self.assertEqual((result['completed'], result['planned'], result['exit_code']), (32, 32, 0))
        runs = batch_runs(self.base / 'direct')[1]
        for run in runs:
            meta = read(run / 'run.json')
            self.assertEqual(meta['execution_mode'], 'native_cli')
            self.assertEqual(meta['prepared_experiment_sha256'], meta['experiment_sha256'])
            self.assertTrue(meta['isolation_verified'])
            self.assertEqual(meta['isolation']['cleanup'], 'complete')
            self.assertTrue((run / 'execution.json').is_file())
            self.assertFalse(meta['experiment']['tool_policy']['tools_verified'])
            if meta['cli'] in ('kimi', 'dsh'):
                self.assertEqual(meta['model_observation']['status'], 'not_reported')
        first = self.reviewed(self.base / 'direct', 'synthetic-reviewer-a')
        second = self.reviewed(self.base / 'direct', 'synthetic-reviewer-b')
        self.assertEqual(verify_results(ROOT, first)['exit_code'], 0)
        report_statistics(ROOT, [first], self.base / 'statistics', samples=32)
        rows = read(self.base / 'statistics/statistics.json')['rows']
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r['complete'] and r['expected_repeats'] == 2 and r['mean_score'] == 75 for r in rows))
        comparison = compare_reviews(ROOT, [first, second], self.base / 'calibration')
        self.assertEqual(comparison['completed_answers'], 32)

    def test_portable_packets_roundtrip_to_the_same_standard_plan(self):
        exported = self.export()
        scored_batches = []
        for i, a in enumerate(self.registry['agents']):
            archive = Path(exported['packets'][i]['candidate_zip'])
            with ZipFile(archive) as z:
                # The answer-only credential loader is code, not saved account state.
                self.assertEqual(z.read('native_cli/credentials.py'), (ROOT / 'benchmark/credentials.py').read_bytes())
                self.assertFalse(any('rubric' in n or 'organizer' in n or
                    ('credentials' in n and n != 'native_cli/credentials.py') for n in z.namelist()))
                query = z.read('questions/' + self.rows[0][0]['id'] + '.md').decode()
                self.assertEqual(query, candidate_prompt(self.rows[0][0]))
                self.assertNotIn('"deliverables"', query)
                self.assertNotIn('"execution"', query)
                self.assertIn('不要读取评分标准', query)
            returned, report = self.answer_packet(a)
            with ZipFile(report['results_zip']) as z:
                self.assertFalse(any('.local' in n or 'credentials' in n for n in z.namelist()))
            destination = self.base / ('imported-' + a['id'])
            imported = native.import_native(ROOT, self.base / 'packets', returned, destination)
            self.assertEqual((imported['completed'], imported['planned'], imported['exit_code']), (4, 4, 0))
            for run in batch_runs(destination)[1]:
                self.assertEqual((run / 'workspace/answer.md').read_bytes(), ANSWER.encode())
                self.assertTrue((run / 'cli_evidence/events.jsonl').is_file())
            scored_batches.append(self.reviewed(destination, 'synthetic-same-reviewer'))
            # Unique destination names while using one recorded reviewer profile.
            shutil.move(self.base / 'synthetic-same-reviewer-scored', self.base / ('scored-' + a['id']))
            shutil.rmtree(self.base / 'synthetic-same-reviewer-packets')
            scored_batches[-1] = self.base / ('scored-' + a['id'])
        self.assertEqual(report_statistics(ROOT, scored_batches, self.base / 'combined', samples=32)['exit_code'], 0)
        self.assertEqual(len(read(self.base / 'combined/statistics.json')['rows']), 8)

    def test_file_answer_survives_closing_reply_in_resume_and_portable_import(self):
        selected = [next(a for a in self.registry['agents'] if a['cli'] == cli) for cli in ('codex', 'qwen')]
        closing = 'Completed; see answer.md.'
        for a in selected:
            self.fake(a, final_answer=closing)
            name = 'separate-' + a['id']
            result = self.run_direct(name, [a['id']])
            self.assertEqual(result['completed'], 4)
            for run in batch_runs(self.base / name)[1]:
                self.assertEqual((run / 'workspace/answer.md').read_text(), ANSWER)
                self.assertEqual((run / 'workspace/final-response.md').read_text(), closing)
            with patch('benchmark.native_runtime.capture', side_effect=AssertionError('must not rerun')):
                self.assertEqual(self.run_direct(name, [a['id']], resume=True)['completed'], 4)
        self.export([a['id'] for a in selected])
        for a in selected:
            returned, result = self.answer_packet(a)
            self.assertEqual(result['completed'], 4)
            dest = self.base / ('separate-import-' + a['id'])
            native.import_native(ROOT, self.base / 'packets', returned, dest)
            for run in batch_runs(dest)[1]:
                self.assertEqual((run / 'workspace/answer.md').read_text(), ANSWER)
                self.assertEqual((run / 'cli_evidence/final-response.md').read_text(), closing)

    def test_invalid_output_stops_and_resume_preserves_failed_attempt(self):
        a = self.registry['agents'][0]
        self.fake(a, 'invalid')
        first = self.run_direct('failed', [a['id']])
        self.assertEqual((first['completed'], first['exit_code']), (0, 1))
        runs = batch_runs(self.base / 'failed')[1]
        captured = (runs[0] / 'run.json').read_bytes()
        self.fake(a)
        second = self.run_direct('failed', [a['id']], resume=True)
        self.assertEqual((second['completed'], second['exit_code']), (3, 1))
        self.assertEqual((runs[0] / 'run.json').read_bytes(), captured)
        export_submission(ROOT, self.base / 'failed', self.base / 'failed.zip')

    def test_changed_answer_is_rejected_before_any_cli_check(self):
        a = self.registry['agents'][0]
        self.run_direct('edited', [a['id']])
        answer = batch_runs(self.base / 'edited')[1][0] / 'workspace/answer.md'
        answer.write_text('edited answer')
        with patch.object(cli_batch, 'inspect_agents', side_effect=AssertionError('CLI called')):
            with self.assertRaisesRegex(ValueError, 'Original answer changed'):
                self.run_direct('edited', [a['id']], resume=True)

    def test_partial_portable_capture_keeps_unattempted_jobs_and_no_full_mean(self):
        a = self.registry['agents'][0]
        self.export([a['id']])
        self.fake(a, 'invalid')
        returned, _ = self.answer_packet(a)
        result = native.import_native(ROOT, self.base / 'packets', returned, self.base / 'partial')
        self.assertEqual((result['planned'], result['completed'], result['exit_code']), (4, 0, 1))
        self.assertEqual(result['statuses'], {'invalid_output': 1, 'prepared': 3})
        report_statistics(ROOT, [self.base / 'partial'], self.base / 'partial-stats', samples=32)
        self.assertIsNone(read(self.base / 'partial-stats/statistics.json')['rows'][0]['mean_score'])

    def test_tampered_return_is_rejected_without_creating_output(self):
        a = self.registry['agents'][0]
        self.export([a['id']])
        returned, _ = self.answer_packet(a)
        answer = next((returned / 'runs').glob('*/r1/answer.md'))
        answer.write_text('edited')
        with self.assertRaisesRegex(ValueError, 'file changed'):
            native.import_native(ROOT, self.base / 'packets', returned, self.base / 'forbidden')
        self.assertFalse((self.base / 'forbidden').exists())

    def test_native_study_templates_and_protocol_pin_are_checked(self):
        out = self.base / 'study'
        prepare_study(ROOT, out, self.registry_path)
        draft = read(out / 'baselines.draft.json')
        self.assertEqual(draft['schema'], 'native-cli-batch.1')
        self.assertEqual(len(read(out / 'runtime-acceptance.template.json')['baselines']), 8)
        self.assertEqual(check_study(ROOT, out / 'protocol.draft.json', out / 'readiness.template.json')['exit_code'], 1)
        protocol = read(out / 'protocol.draft.json')
        protocol.update(evaluation_split='dev', required_web_modes=['historical_source_rules_in_prompt'],
                        repeats=2, time_limit_seconds=3, baseline_registry_sha256='0' * 64)
        path = self.base / 'study-protocol.json'; write(path, protocol)
        with self.assertRaisesRegex(ValueError, 'Pinned native study registry/dataset differs'):
            native.export_native(ROOT, self.registry_path, self.base / 'forbidden', 'dev', protocol_path=path)
        self.assertFalse((self.base / 'forbidden').exists())

    def test_claude_helpers_do_not_mask_a_real_primary_model_mismatch(self):
        agent = next(a for a in self.registry['agents'] if a['cli'] == 'claude')
        original = fake_events
        def with_helper(cli, model):
            events = original(cli, model)
            if cli == 'claude':
                events[-1]['modelUsage'] = {model: {}, 'helper-fixture': {}}
            return events
        for mode, expected in [('success', 'completed'), ('mismatch', 'model_mismatch')]:
            with patch(__name__ + '.fake_events', side_effect=with_helper):
                self.fake(agent, mode)
            out = 'claude-helper-' + mode
            result = self.run_direct(out, [agent['id']], [self.rows[0][0]['id']])
            self.assertEqual(result['exit_code'], 0 if mode == 'success' else 1)
            run = batch_runs(self.base / out)[1][0]
            meta = read(run / 'run.json')
            self.assertEqual(meta['status'], expected)
            self.assertIn('helper-fixture', meta['reported_models'])
            self.assertEqual(meta['other_reported_models'], ['helper-fixture'])
            self.assertEqual(meta['model_observation']['status'],
                             'cli_reported_match' if mode == 'success' else 'cli_reported_mismatch')

    def test_timeout_model_mismatch_and_interruption_are_nonzero(self):
        a = self.registry['agents'][0]
        for mode, status in (('timeout', 'timeout'), ('mismatch', 'model_mismatch')):
            with self.subTest(mode=mode):
                self.fake(a, mode)
                result = self.run_direct(mode, [a['id']])
                self.assertEqual(result['exit_code'], 1)
                first = batch_runs(self.base / mode)[1][0]
                self.assertEqual(read(first / 'run.json')['status'], status)
                self.assertTrue((first / 'workspace/trajectory.jsonl').is_file())
        self.fake(a)
        original_popen = subprocess.Popen
        def interrupted_capture(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            if '--json' in args[0] and '--help' not in args[0]:
                process.communicate = unittest.mock.Mock(side_effect=KeyboardInterrupt)
            return process
        with patch.object(cli_batch.subprocess, 'Popen', side_effect=interrupted_capture):
            result = self.run_direct('interrupted', [a['id']])
        self.assertEqual((result['status'], result['exit_code']), ('interrupted', 130))

    def test_native_readiness_requires_runtime_versions_to_match_pinned_registry(self):
        out = self.base / 'native-evidence'
        prepare_study(ROOT, out, self.registry_path)
        protocol_path = out / 'protocol.draft.json'
        protocol = read(protocol_path)
        protocol['baseline_registry_sha256'] = file_hash(out / 'baselines.draft.json')
        write(protocol_path, protocol)
        runtime_path = out / 'runtime-acceptance.template.json'
        runtime = read(runtime_path)
        runtime['operator'] = 'synthetic fixture operator'
        for row in runtime['baselines']:
            row.update(status='passed', model_observed='synthetic fixture model', client_version='wrong-version',
                       evidence_references=['synthetic fixture only'])
            row['checks'] = {key: True for key in row['checks']}
        write(runtime_path, runtime)
        readiness = read(out / 'readiness.template.json')
        readiness['protocol_sha256'] = digest(protocol)
        for gate in readiness['gates']:
            if gate['id'] in ('protocol_decisions', 'runtime'):
                path = out / 'baselines.draft.json' if gate['id'] == 'protocol_decisions' else runtime_path
                gate.update(status='approved', approved_by='synthetic fixture operator', reason='synthetic gate check',
                            artifacts=[{'kind': 'baseline_registry' if gate['id'] == 'protocol_decisions' else 'runtime_acceptance',
                                        'path': str(path), 'sha256': file_hash(path)}])
        readiness_path = out / 'readiness.template.json'; write(readiness_path, readiness)
        self.assertIn('native_runtime_cli_version_mismatch',
                      check_study(ROOT, protocol_path, readiness_path)['blocked'])
        for row in runtime['baselines']:
            row['client_version'] = 'fixture-1'
        write(runtime_path, runtime)
        next(g for g in readiness['gates'] if g['id'] == 'runtime')['artifacts'][0]['sha256'] = file_hash(runtime_path)
        write(readiness_path, readiness)
        checked = check_study(ROOT, protocol_path, readiness_path)
        self.assertNotIn('native_runtime_cli_version_mismatch', checked['blocked'])
        self.assertNotIn('baseline_registry_empty_or_policy_mismatch', checked['blocked'])
        self.assertEqual(checked['exit_code'], 1)  # Other real-evidence gates remain pending.

    def test_portable_entrypoint_preserves_interrupt_and_partial_status(self):
        with patch.object(cli_packet_runner, 'main', return_value={'expected': 4, 'completed': 0,
                'records': [{'status': 'interrupted'}, {'status': 'not_run'}]}):
            with self.assertRaises(SystemExit) as result:
                cli_packet_runner.entrypoint(self.base)
        self.assertEqual(result.exception.code, 130)
        from benchmark.cli_common import result_exit_code
        self.assertEqual(result_exit_code({'completed': 0, 'imported': 1,
                                          'records': [{'status': 'invalid_output'}, {'status': 'not_run'}]}), 1)

    def test_protocol_budget_conflict_and_import_resume_are_rejected(self):
        protocol = read(ROOT / 'config/native-cli.protocol.json')
        path = self.base / 'protocol.json'; write(path, protocol)
        with self.assertRaisesRegex(ValueError, 'repeat/time policy'):
            native.run_native(ROOT, self.registry_path, self.base / 'bad', 'public-smoke', protocol_path=path)
        a = self.registry['agents'][0]
        self.export([a['id']]); returned, _ = self.answer_packet(a)
        native.import_native(ROOT, self.base / 'packets', returned, self.base / 'imported')
        with self.assertRaisesRegex(ValueError, 'review-only'):
            self.run_direct('imported', [a['id']], resume=True)

    def test_copied_packet_executes_without_organizer_imports(self):
        a = self.registry['agents'][0]
        self.export([a['id']])
        packet = self.base / 'packets/candidates' / a['id']
        local = read(packet / 'registry.json')
        local['agents'][0]['binary'] = a['binary']
        write(packet / '.local/registry.json', local)
        env = dict(os.environ, PYTHONPATH='')
        run = subprocess.run([sys.executable, str(packet / 'run_agent.py'), '--execute',
            '--out', str(self.base / 'standalone')], cwd=packet, env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        report = read(self.base / 'standalone/summary.json')
        self.assertEqual(report['completed'], 4)
        for row in report['records']:
            meta = read(self.base / 'standalone' / row['path'] / 'run.json')
            self.assertTrue(meta['isolation_verified'])
        imported = native.import_native(ROOT, self.base / 'packets', self.base / 'standalone', self.base / 'standalone-import')
        self.assertEqual(imported['completed'], 4)
        for folder in batch_runs(self.base / 'standalone-import')[1]:
            meta = read(folder / 'run.json')
            self.assertFalse(meta['isolation_verified'])
            self.assertEqual(meta['isolation_evidence_source'], 'submitter_report_unverified')
            self.assertTrue((folder / 'cli_evidence/execution.json').is_file())
            self.assertTrue((folder / 'cli_evidence/trace.jsonl').is_file())

    def test_duplicate_session_failure_does_not_poison_resume(self):
        a = self.registry['agents'][0]
        script = Path(a['binary'])
        script.write_text(script.read_text().replace('session=$(/usr/bin/uuidgen)', 'session=fixed-fixture'))
        first = self.run_direct('duplicate', [a['id']])
        self.assertEqual(first['statuses'], {'completed': 1, 'invalid_output': 1, 'prepared': 2})
        run = batch_runs(self.base / 'duplicate')[1][1]
        original = (run / 'run.json').read_bytes()
        self.assertIsNone(read(run / 'run.json')['session_id'])
        self.assertEqual(read(run / 'run.json')['duplicate_session_id'], 'fixed-fixture')
        self.fake(a)
        resumed = self.run_direct('duplicate', [a['id']], resume=True)
        self.assertEqual(resumed['statuses'], {'completed': 3, 'invalid_output': 1})
        self.assertEqual((run / 'run.json').read_bytes(), original)

    def test_module_check_failure_and_preview_have_correct_shell_exit_codes(self):
        registry = copy.deepcopy(self.registry)
        registry['agents'] = [dict(registry['agents'][0], binary='missing-native-fixture-binary')]
        path = self.base / 'missing.json'; write(path, registry)
        check = subprocess.run([sys.executable, '-m', 'benchmark', 'cli-check', '--registry', str(path)],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(check.returncode, 1, check.stderr)
        self.assertEqual(json.loads(check.stdout)['status'], 'setup_required')
        preview = subprocess.run([sys.executable, '-m', 'benchmark', 'cli-batch', '--registry', str(path),
            '--suite', 'public-smoke', '--out', str(self.base / 'preview')], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse((self.base / 'preview').exists())


if __name__ == '__main__':
    unittest.main()
