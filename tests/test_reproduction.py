"""Reproduction/transport acceptance. Answers and judgments here are synthetic fixtures."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from benchmark.archives import hash_bytes, read_archive, write_archive
from benchmark.core import digest, read, write
from benchmark.manual import record_app_answer
from benchmark.release import build_release, verify_release
from benchmark.reproduction import batch_runs, reproduce, suite_rows
from benchmark.runner import aggregate
from benchmark.scoring import judgment_template
from benchmark.submissions import (apply_reviews, export_submission, import_submission,
                                   review_batch, verify_results)

ROOT = Path(__file__).resolve().parents[1]
SINGLE_TASKS = len(read(ROOT / 'data/dev/manifest.json')['tasks'])
PROFILE = 'config/reproduction/public-smoke-apps.json'
RAW = 'Synthetic answer only.\r\nA fixture, not a model evaluation.\r\n'.encode()


class ReproductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.batch = self.base / 'batch'
        reproduce(ROOT, PROFILE, self.batch, ['gemini-app'])
        _, self.runs = batch_runs(self.batch)

    def capture(self, run, outcome='completed'):
        session = read(run / 'app-session.template.json')
        session.update(surface='desktop_app', app_version='synthetic-app', selected_model_label='unverified',
                       mode='fixture', reasoning_setting='unknown', web_search='disabled', memory='unknown',
                       new_conversation=True, extra_user_messages=0, model_switch_observed=False,
                       started_at='2026-10-08T00:00:00Z', finished_at='2026-10-08T00:01:00Z', outcome=outcome)
        session_file = self.base / 'session.json'; write(session_file, session)
        answer = self.base / 'answer.md'; answer.write_bytes(RAW)
        record_app_answer(run, answer if outcome == 'completed' else None, session_file)

    def fill_reviews(self, incoming, pending=False):
        packets = self.base / 'packets'
        review_batch(ROOT, incoming, packets)
        mapping = {p['path']: p.get('packet_path', p['path']) for p in read(packets / 'review-manifest.json')['packets']}
        _, runs = batch_runs(incoming)
        for run in runs:
            if read(run / 'run.json')['status'] != 'completed':
                continue
            meta = read(run / 'run.json')
            folder = packets / mapping[f"runs/{meta['adapter']}/{meta['task_id']}/r1"]
            answer = (run / 'workspace/answer.md').read_text()
            judgment = judgment_template(read(run / 'task.json'), read(run / 'rubric.json'), answer)
            judgment.update(reviewer='synthetic-human-fixture', judge_version='evidence-review-v1', candidates_reviewed=True)
            for node in judgment['nodes']:
                node.update(grade='pending' if pending else 0, reason='Synthetic missing requirement fixture')
            write(folder / 'judgment.json', judgment)
            write(folder / 'review-settings.json', {'reviewer': judgment['reviewer'], 'reviewer_type': 'human',
                  'judge_version': judgment['judge_version'], 'provider': None, 'model': None,
                  'model_version_evidence': None, 'reasoning_setting': None})
        return packets

    def test_prepare_resume_uses_fixed_configuration_without_model_calls(self):
        with patch('benchmark.batch.execute') as execute:
            result = reproduce(ROOT, PROFILE, self.batch, ['gemini-app'], execute=True, resume=True)
            self.assertEqual(result['manual_pending'], 2)
            execute.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'changed'):
            reproduce(ROOT, PROFILE, self.batch, ['kimi-k3'], resume=True)
        self.assertEqual(verify_results(ROOT, self.batch)['exit_code'], 1)
        registry = read(self.batch / 'resolved-registry.json')
        registry['baselines'][0]['command'] = ['untrusted-command']
        write(self.batch / 'resolved-registry.json', registry)
        with self.assertRaisesRegex(ValueError, 'Resolved registry changed'):
            reproduce(ROOT, PROFILE, self.batch, ['gemini-app'], execute=True, resume=True)

    def test_original_bytes_review_and_results_roundtrip(self):
        for run in self.runs:
            self.capture(run)
        package = self.base / 'answers.zip'
        export_submission(ROOT, self.batch, package)
        manifest, files = read_archive(package)
        self.assertEqual(len(manifest['attempts']), 2)
        self.assertTrue(all(not any(term in n for term in ('rubric.json', 'reference', 'stdout', 'session.json')) for n in files))
        incoming = self.base / 'incoming'; import_submission(ROOT, package, incoming)
        for run in batch_runs(incoming)[1]:
            self.assertEqual((run / 'workspace/answer.md').read_bytes(), RAW)
            self.assertFalse(read(run / 'run.json')['isolation_verified'])
        packets = self.fill_reviews(incoming)
        scored = self.base / 'scored'; value = apply_reviews(ROOT, incoming, packets, scored)
        self.assertEqual(value['exit_code'], 0)
        self.assertEqual(value['summary']['rows'][0]['mean_score'], 0)
        evidence = value['summary']['rows'][0]['trajectory_requirements']
        self.assertEqual((evidence['required_tasks'], evidence['recorded_tasks'], evidence['complete']), (2, 0, False))
        self.assertFalse(any((run / 'score.json').exists() for run in self.runs))
        results = self.base / 'results.zip'; export_submission(ROOT, scored, results, include_reviews=True)
        replay = self.base / 'replay'; import_submission(ROOT, results, replay)
        self.assertEqual(verify_results(ROOT, replay)['summary'], value['summary'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            import_submission(ROOT, results, replay)
        with self.assertRaisesRegex(ValueError, 'review-only'):
            reproduce(ROOT, PROFILE, incoming, ['gemini-app'], resume=True)

    def test_failure_is_preserved_and_prevents_full_mean(self):
        self.capture(self.runs[0]); self.capture(self.runs[1], 'failed')
        packets = self.fill_reviews(self.batch)
        scored = self.base / 'scored'; result = apply_reviews(ROOT, self.batch, packets, scored)
        self.assertEqual(result['exit_code'], 0)
        self.assertIsNone(result['summary']['rows'][0]['mean_score'])
        self.assertEqual(len(batch_runs(scored)[1]), 2)
        export_submission(ROOT, scored, self.base / 'results.zip', include_reviews=True)

    def test_pending_judgments_are_incomplete_and_cannot_be_results_exported(self):
        for run in self.runs:
            self.capture(run)
        result = apply_reviews(ROOT, self.batch, self.fill_reviews(self.batch, True), self.base / 'scored')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(len(result['missing_reviews']), 2)
        with self.assertRaisesRegex(ValueError, 'Finish and review'):
            export_submission(ROOT, self.base / 'scored', self.base / 'results.zip', include_reviews=True)

    def test_omitted_attempt_or_changed_model_is_rejected(self):
        manifest = read(self.batch / 'batch.json')
        original = copy.deepcopy(manifest)
        manifest['runs'].pop(); write(self.batch / 'batch.json', manifest)
        with self.assertRaisesRegex(ValueError, 'missing'):
            verify_results(ROOT, self.batch)
        write(self.batch / 'batch.json', original)
        meta = read(self.runs[0] / 'run.json'); meta['model'] = 'different-model'; write(self.runs[0] / 'run.json', meta)
        with self.assertRaisesRegex(ValueError, 'baseline/budget'):
            export_submission(ROOT, self.batch, self.base / 'invalid.zip')

    def test_launch_failure_and_missing_answer_remain_transportable(self):
        for run, status in zip(self.runs, ('launch_failed', 'missing_answer')):
            meta = read(run / 'run.json'); meta['status'] = status; write(run / 'run.json', meta)
        package = self.base / 'failure.zip'; export_submission(ROOT, self.batch, package)
        imported = self.base / 'imported'; import_submission(ROOT, package, imported)
        self.assertEqual(verify_results(ROOT, imported)['exit_code'], 0)
        self.assertIsNone(verify_results(ROOT, imported)['summary']['rows'][0]['mean_score'])

    def test_archive_rejects_corrupt_bytes_and_unsafe_paths(self):
        package = self.base / 'answers.zip'; export_submission(ROOT, self.batch, package)
        manifest, files = read_archive(package)
        corrupt = self.base / 'corrupt.zip'
        with zipfile.ZipFile(corrupt, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            for name, data in files.items():
                archive.writestr(name, data + b'changed')
        with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
            import_submission(ROOT, corrupt, self.base / 'bad')
        for name in ('../escape', '/absolute', 'x\\evil', 'x/./evil'):
            with self.subTest(name=name):
                attack = self.base / 'attack.zip'
                with zipfile.ZipFile(attack, 'w') as archive:
                    archive.writestr(name, b'bad')
                    archive.writestr('manifest.json', '{}')
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    read_archive(attack)
        self.assertFalse((self.base / 'bad').exists())

    def test_rehashed_unexpected_files_and_truncated_suite_are_rejected(self):
        package = self.base / 'answers.zip'; export_submission(ROOT, self.batch, package)
        manifest, files = read_archive(package)
        files['unrequested.txt'] = b'do not import'
        write_archive(self.base / 'extra.zip', files, manifest)
        with self.assertRaisesRegex(ValueError, 'Unexpected files'):
            import_submission(ROOT, self.base / 'extra.zip', self.base / 'extra')
        manifest['context']['task_ids'].pop()
        write_archive(self.base / 'truncated.zip', {k: v for k, v in files.items() if k != 'unrequested.txt'}, manifest)
        with self.assertRaisesRegex(ValueError, 'truncated'):
            import_submission(ROOT, self.base / 'truncated.zip', self.base / 'truncated')

    def test_fabricated_quote_and_changed_score_are_rejected(self):
        for run in self.runs:
            self.capture(run)
        packets = self.fill_reviews(self.batch)
        folder = next(packets.glob('packets/*'))
        judgment = read(folder / 'judgment.json')
        judgment['nodes'][0].update(grade=1, answer_quote='This quote does not occur.', reason='Invalid synthetic claim')
        write(folder / 'judgment.json', judgment)
        with self.assertRaises(ValueError):
            apply_reviews(ROOT, self.batch, packets, self.base / 'bad-score')
        self.assertFalse((self.base / 'bad-score').exists())
        judgment['nodes'][0].update(grade=0, answer_quote=''); write(folder / 'judgment.json', judgment)
        scored = self.base / 'scored'; apply_reviews(ROOT, self.batch, packets, scored)
        run = batch_runs(scored)[1][0]
        saved = read(run / 'score.json'); saved['score'] = 100; write(run / 'score.json', saved)
        with self.assertRaisesRegex(ValueError, 'not reproducible'):
            verify_results(ROOT, scored)

    def test_release_is_public_only_and_checks_engine_fingerprint(self):
        package = self.base / 'release.zip'; build_release(ROOT, package)
        result = verify_release(package)
        # Count the selected public collection; demos and derived episodes are separate.
        self.assertEqual((result['public_dev_tasks'], result['single_stage_tasks'],
                          result['staged_dev_tasks'], result['public_demo_tasks']), (SINGLE_TASKS + 13, SINGLE_TASKS, 13, 2))
        manifest, files = read_archive(package)
        self.assertFalse(any('benchmark-private' in n or '__pycache__' in n or n.startswith('runs/') for n in files))
        files['benchmark/scoring.py'] += b'\n# changed\n'
        write_archive(self.base / 'changed-release.zip', files, manifest)
        with self.assertRaisesRegex(ValueError, 'workflow fingerprint'):
            verify_release(self.base / 'changed-release.zip')

    def test_blind_packet_mapping_preserves_answer_and_legacy_layout(self):
        for run in self.runs:
            self.capture(run)
        packets = self.fill_reviews(self.batch)
        manifest = read(packets / 'review-manifest.json')
        self.assertTrue(manifest['blind_packet_names'])
        for item in manifest['packets']:
            packet = packets / item['packet_path']
            self.assertRegex(item['packet_path'], r'^packets/[a-f0-9]{32}$')
            self.assertNotIn('gemini-app', item['packet_path'])
            self.assertNotIn('adapter', read(packet / 'task.json'))
            self.assertEqual((packet / 'answer.md').read_text(), RAW.decode().replace('\r\n', '\n'))
            self.assertFalse((packet / 'review-manifest.json').exists())
            legacy = packets / item['path']
            legacy.parent.mkdir(parents=True, exist_ok=True)
            packet.rename(legacy)
            item.pop('packet_path')
        write(packets / 'review-manifest.json', manifest)
        self.assertEqual(apply_reviews(ROOT, self.batch, packets, self.base / 'legacy-scored')['exit_code'], 0)

    def test_packet_escape_is_rejected_without_partial_output(self):
        for run in self.runs:
            self.capture(run)
        packets = self.fill_reviews(self.batch)
        manifest = read(packets / 'review-manifest.json')
        manifest['packets'][0]['packet_path'] = '../outside'
        write(packets / 'review-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            apply_reviews(ROOT, self.batch, packets, self.base / 'escaped')
        self.assertFalse((self.base / 'escaped').exists())

    def test_aggregate_rejects_changed_or_unknown_reviewer_profiles(self):
        for run in self.runs:
            self.capture(run)
        scored = self.base / 'scored'
        apply_reviews(ROOT, self.batch, self.fill_reviews(self.batch), scored)
        _, runs = batch_runs(scored)
        settings_path = runs[0] / 'review-settings.json'
        original = read(settings_path)
        settings = dict(original, reasoning_setting='different')
        write(settings_path, settings)
        meta = read(runs[0] / 'run.json')
        meta['review_settings_sha256'] = digest(settings)
        write(runs[0] / 'run.json', meta)
        rows = suite_rows(ROOT, 'public-smoke')
        protocol = read(runs[0] / 'protocol.json')
        with self.assertRaisesRegex(ValueError, 'Mixed reviewer'):
            aggregate(rows, runs, protocol)
        write(settings_path, original)
        meta['review_settings_sha256'] = digest(original)
        write(runs[0] / 'run.json', meta)
        settings_path.unlink()
        with self.assertRaisesRegex(ValueError, 'unknown legacy'):
            aggregate(rows, runs, protocol)

    def test_failed_non_utf8_bytes_roundtrip_without_becoming_a_judge_answer(self):
        for run in self.runs:
            self.capture(run)
        raw = b'Invalid UTF-8 failure artifact: \xff\xfe'
        (self.runs[0] / 'workspace/answer.md').write_bytes(raw)
        meta = read(self.runs[0] / 'run.json')
        meta.update(status='failed', answer_file_sha256=hash_bytes(raw))
        write(self.runs[0] / 'run.json', meta)
        package = self.base / 'failed-bytes.zip'
        export_submission(ROOT, self.batch, package)
        incoming = self.base / 'incoming'
        import_submission(ROOT, package, incoming)
        imported = batch_runs(incoming)[1]
        self.assertEqual((imported[0] / 'workspace/answer.md').read_bytes(), raw)
        packets = self.base / 'packets'
        self.assertEqual(review_batch(ROOT, incoming, packets)['answers'], 1)
        self.assertEqual(len(read(packets / 'review-manifest.json')['packets']), 1)

    def test_release_rejects_rehashed_private_file_count_or_removed_engine(self):
        package = self.base / 'release.zip'
        build_release(ROOT, package)
        manifest, files = read_archive(package)
        for index, name in enumerate(('config/credentials.local.json', 'data/dev/UNLISTED/task.json')):
            with self.subTest(name=name):
                extra = dict(files, **{name: b'{}'})
                attack = self.base / f'extra-release-{index}.zip'
                write_archive(attack, extra, manifest)
                with self.assertRaises(ValueError):
                    verify_release(attack)
        changed = copy.deepcopy(manifest)
        changed['public_dev_tasks'] = 999
        write_archive(self.base / 'wrong-count.zip', files, changed)
        with self.assertRaisesRegex(ValueError, 'task counts'):
            verify_release(self.base / 'wrong-count.zip')
        changed = copy.deepcopy(manifest)
        changed['engine_files'].pop('benchmark/runner.py')
        changed['engine_sha256'] = digest(changed['engine_files'])
        missing = {k: v for k, v in files.items() if k != 'benchmark/runner.py'}
        write_archive(self.base / 'missing-engine.zip', missing, changed)
        with self.assertRaisesRegex(ValueError, 'Incomplete release workflow'):
            verify_release(self.base / 'missing-engine.zip')

    def test_release_excludes_unlisted_tasks_and_rejects_linked_parent_before_loading(self):
        linked = self.base / 'linked'
        linked.mkdir()
        (linked / 'data').symlink_to(ROOT / 'data', target_is_directory=True)
        with patch('benchmark.release.collection') as load:
            with self.assertRaisesRegex(ValueError, 'linked public'):
                build_release(linked, self.base / 'linked.zip')
            load.assert_not_called()
        copy_root = self.base / 'source'
        shutil.copytree(ROOT, copy_root, ignore=shutil.ignore_patterns('__pycache__', '.DS_Store'))
        unlisted = copy_root / 'data/dev/UNLISTED'
        unlisted.mkdir()
        for name in ('task.json', 'rubric.json'):
            (unlisted / name).write_text('{}')
        package = self.base / 'clean.zip'
        build_release(copy_root, package)
        self.assertFalse(any('UNLISTED' in name for name in read_archive(package)[1]))
        self.assertEqual(verify_release(package)['public_dev_tasks'], SINGLE_TASKS + 13)


if __name__ == '__main__':
    unittest.main()
