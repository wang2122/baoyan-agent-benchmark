"""Analytical expectations and complete synthetic review/statistics workflows; no model calls."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from benchmark.adapters import select_baselines
from benchmark.batch import batch_run
from benchmark.calibration import compare_reviews, krippendorff_alpha, record_adjudication
from benchmark.core import digest, file_hash, load_tasks, read, write
from benchmark.manual import record_app_answer
from benchmark.reproduction import batch_runs, make_context, suite_rows
from benchmark.scoring import judgment_template
from benchmark.statistics import cluster_interval, report_statistics
from benchmark.study import GATES, RUNTIME_CHECKS, check_study, freeze_study, prepare_study
from benchmark.submissions import apply_reviews, check_review_settings, review_batch
from benchmark.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
ANSWER = 'Synthetic fixture answer. No real model evaluation.\r\n'.encode()


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.rows = suite_rows(ROOT, 'public-smoke')
        self.protocol = dict(read(ROOT / 'config/reproduction/materials.protocol.json'), repeats=3)
        self.registry = select_baselines(read(ROOT / 'config/baselines.example.json'), ['gemini-app', 'kimi-k3'])
        self.registry.update(repeats=3, timeout_seconds=900)
        self.batch = self.base / 'answers'
        batch_run(self.rows, self.protocol, self.registry, self.batch)
        write(self.batch / 'reproduction.json', make_context(ROOT, self.rows, self.protocol, 'public-smoke', source='legacy_batch'))
        for run in batch_runs(self.batch)[1]:
            meta = read(run / 'run.json')
            session = read(run / 'app-session.template.json')
            session.update(surface='desktop_app', app_version='fixture-app',
                selected_model_label='K3' if meta['adapter'] == 'kimi-k3' else 'unverified', mode='fixture',
                reasoning_setting='unknown', web_search='disabled', memory='unknown',
                new_conversation=True, extra_user_messages=0, model_switch_observed=False,
                started_at='2026-10-08T00:00:00Z', finished_at='2026-10-08T00:01:00Z', outcome='completed')
            write(self.base / 'session.json', session)
            (self.base / 'answer.md').write_bytes(ANSWER)
            record_app_answer(run, self.base / 'answer.md', self.base / 'session.json')

    def reviewed(self, name, modifier=None):
        packets = self.base / (name + '-packets')
        review_batch(ROOT, self.batch, packets)
        mapping = {item['path']: item['packet_path'] for item in read(packets / 'review-manifest.json')['packets']}
        for run in batch_runs(self.batch)[1]:
            meta = read(run / 'run.json')
            path = f"runs/{meta['adapter']}/{meta['task_id']}/r{meta['repeat']}"
            folder = packets / mapping[path]
            answer = (run / 'workspace/answer.md').read_text()
            judgment = judgment_template(read(run / 'task.json'), read(run / 'rubric.json'), answer)
            grade = .5 if meta['adapter'] == 'kimi-k3' else (0, .5, 1)[meta['repeat'] - 1]
            if meta['adapter'] == 'gemini-app' and meta['task_id'] == self.rows[1][0]['id']:
                grade = (.5, 1, 1)[meta['repeat'] - 1]
            judgment.update(reviewer=name, judge_version='evidence-review-v1', candidates_reviewed=True)
            for node in judgment['nodes']:
                node.update(grade=grade, answer_quote='Synthetic fixture answer.', reason='Synthetic reviewed credit')
            if modifier:
                modifier(meta, judgment)
            write(folder / 'judgment.json', judgment)
            write(folder / 'review-settings.json', {'reviewer': name, 'reviewer_type': 'human',
                'judge_version': 'evidence-review-v1', 'provider': None, 'model': None,
                'model_version_evidence': None, 'reasoning_setting': None})
        scored = self.base / name
        apply_reviews(ROOT, self.batch, packets, scored)
        return scored

    def test_repeat_means_and_paired_difference_use_all_answers(self):
        scored = self.reviewed('reviewer-a')
        out = self.base / 'statistics'
        result = main(['statistics', '--batch-dir', str(scored), '--out', str(out), '--bootstrap-samples', '256', '--seed', '7'])
        self.assertEqual(result['status'], 'statistics_complete')
        report = read(out / 'statistics.json')
        rows = {row['adapter']: row for row in report['rows']}
        self.assertAlmostEqual(rows['gemini-app']['mean_score'], 200 / 3, places=5)
        self.assertEqual(rows['kimi-k3']['mean_score'], 50)
        self.assertEqual(rows['kimi-k3']['repeat_mean_stddev'], 0)
        self.assertAlmostEqual(rows['gemini-app']['repeat_mean_stddev'], 38.1881308, places=5)
        self.assertEqual([row['mean_score'] for row in rows['gemini-app']['repeat_means']], [25, 75, 100])
        self.assertEqual(rows['gemini-app']['expected_attempts'], 6)
        self.assertEqual(rows['gemini-app']['runtime_evidence']['required_trajectory_recorded'], 0)
        self.assertEqual(rows['kimi-k3']['confidence_interval']['lower'], 50)
        difference = report['paired_comparisons'][0]['mean_difference']
        self.assertAlmostEqual(abs(difference), 50 / 3, places=5)
        replay = self.base / 'statistics-replay'
        report_statistics(ROOT, [scored], replay, samples=256, seed=7)
        self.assertEqual(read(replay / 'statistics.json'), report)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            report_statistics(ROOT, [scored], out, samples=256, seed=7)

    def test_case_group_bootstrap_preserves_clusters_and_task_weights(self):
        tasks = {'a': {'case_group_id': 'one'}, 'b': {'case_group_id': 'one'}, 'c': {'case_group_id': 'two'}}
        interval = cluster_interval({'a': 0, 'b': 100, 'c': 100}, tasks, 1000, 'known-fixture')
        self.assertEqual(interval['case_groups'], 2)
        self.assertEqual((interval['lower'], interval['upper']), (50, 100))
        self.assertIsNone(cluster_interval({'a': 0, 'b': 100}, tasks, 128, 'single-group'))

    def test_missing_score_or_repeat_never_becomes_a_full_mean(self):
        def pending(meta, judgment):
            if meta['adapter'] == 'gemini-app' and meta['repeat'] == 3:
                judgment['nodes'][0]['grade'] = 'pending'
        scored = self.reviewed('pending-reviewer', pending)
        out = self.base / 'incomplete'
        self.assertEqual(report_statistics(ROOT, [scored], out, samples=128)['exit_code'], 1)
        row = next(row for row in read(out / 'statistics.json')['rows'] if row['adapter'] == 'gemini-app')
        self.assertIsNone(row['mean_score'])
        self.assertIsNone(row['confidence_interval'])
        self.assertIsNone(read(out / 'statistics.json')['paired_comparisons'][0]['mean_difference'])
        with self.assertRaisesRegex(ValueError, 'omit planned'):
            report_statistics(ROOT, [scored], self.base / 'omit', expected_repeats=2)

    def test_duplicate_attempts_or_mixed_reviewers_are_rejected(self):
        first = self.reviewed('reviewer-a')
        second = self.reviewed('reviewer-b')
        with self.assertRaisesRegex(ValueError, 'Mixed reviewer'):
            report_statistics(ROOT, [first, second], self.base / 'mixed')
        with self.assertRaisesRegex(ValueError, 'Duplicate or empty'):
            report_statistics(ROOT, [first, first], self.base / 'duplicate')
        copy = self.base / 'same-attempts'
        shutil.copytree(first, copy)
        with self.assertRaisesRegex(ValueError, 'Duplicate task/repeat'):
            report_statistics(ROOT, [first, copy], self.base / 'duplicate-attempts')

    def test_edited_score_cannot_enter_statistics(self):
        scored = self.reviewed('reviewer-a')
        run = batch_runs(scored)[1][0]
        value = read(run / 'score.json'); value['score'] = 99
        write(run / 'score.json', value)
        with self.assertRaisesRegex(ValueError, 'not reproducible'):
            report_statistics(ROOT, [scored], self.base / 'edited')
        self.assertFalse((self.base / 'edited').exists())

    def test_alpha_has_analytical_values_and_handles_missing_or_constant_ratings(self):
        self.assertEqual(krippendorff_alpha([[0, 0], [1, 1]], 'interval'), 1)
        self.assertEqual(krippendorff_alpha([[0, 1], [1, 0]], 'nominal'), -.5)
        self.assertIsNone(krippendorff_alpha([[0, 0], [0, 0]], 'interval'))
        self.assertIsNone(krippendorff_alpha([[0, None], [None, 1]], 'interval'))
        self.assertEqual(krippendorff_alpha([[0, 0, None], [1, 1, 1]], 'nominal'), 1)

    def test_multi_reviewer_comparison_and_adjudication_do_not_change_scores(self):
        first = self.reviewed('reviewer-a')
        def changed(meta, judgment):
            if meta['adapter'] == 'gemini-app' and meta['repeat'] == 1:
                judgment['nodes'][0]['grade'] = 1
        second = self.reviewed('reviewer-b', changed)
        before = {str(run): file_hash(run / 'score.json') for batch in (first, second) for run in batch_runs(batch)[1]}
        out = self.base / 'calibration'
        result = compare_reviews(ROOT, [first, second], out)
        self.assertGreater(result['disagreements'], 0)
        report = read(out / 'calibration.json')
        self.assertEqual(report['completed_answers'], 12)
        self.assertEqual(report['pairwise'][0]['known_total_pairs'], 12)
        self.assertGreater(report['pairwise'][0]['total_mean_absolute_difference'], 0)
        decisions = read(out / 'adjudication-template.json')
        for item in decisions['decisions']:
            item.update(status='resolved', resolution='Use the documented rubric boundary.',
                reason='Synthetic human decision fixture.', reviewer='fixture-expert', reviewer_type='human',
                evidence_references=['rubric.json: relevant node and saved original answer'])
        write(self.base / 'decisions.json', decisions)
        applied = self.base / 'adjudicated'
        self.assertEqual(record_adjudication(ROOT, out, self.base / 'decisions.json', applied)['exit_code'], 0)
        self.assertEqual(read(applied / 'adjudication.json')['effect'], 'record_only_original_scores_unchanged')
        self.assertTrue(all(file_hash(Path(run) / 'score.json') == fingerprint for run, fingerprint in before.items()))
        decisions['decisions'][0]['observed'] = {}
        write(self.base / 'bad-decisions.json', decisions)
        with self.assertRaisesRegex(ValueError, 'modified'):
            record_adjudication(ROOT, out, self.base / 'bad-decisions.json', self.base / 'bad-adjudication')

    def test_pending_is_reported_instead_of_counted_as_agreement(self):
        def pending(meta, judgment):
            judgment['nodes'][0]['grade'] = 'pending'
        first, second = self.reviewed('reviewer-a', pending), self.reviewed('reviewer-b', pending)
        out = self.base / 'pending-calibration'
        compare_reviews(ROOT, [first, second], out)
        report = read(out / 'calibration.json')
        self.assertGreater(report['rating_coverage']['quality_pending_ratings'], 0)
        self.assertGreater(report['pairwise'][0]['item_agreement']['quality_pending_pairs'], 0)
        self.assertEqual(report['pairwise'][0]['known_total_pairs'], 0)
        self.assertTrue(any(item['contains_pending'] for item in report['disagreements']))
        self.assertEqual(record_adjudication(ROOT, out, out / 'adjudication-template.json', self.base / 'pending-record')['exit_code'], 1)

    def test_calibration_rejects_duplicate_profile_or_changed_original_answer(self):
        first = self.reviewed('reviewer-a')
        with self.assertRaisesRegex(ValueError, 'distinct reviewed'):
            compare_reviews(ROOT, [first, first], self.base / 'duplicate-reviewer')
        second = self.reviewed('reviewer-b')
        run = batch_runs(second)[1][0]
        (run / 'workspace/answer.md').write_text('Changed answer')
        with self.assertRaisesRegex(ValueError, 'Original answer changed'):
            compare_reviews(ROOT, [first, second], self.base / 'changed-answer')

    def test_prepare_study_is_draft_and_incomplete_cannot_freeze(self):
        out = self.base / 'study'
        with patch('benchmark.adapters.preflight') as preflight:
            result = prepare_study(ROOT, out)
            preflight.assert_not_called()
        self.assertEqual(result['model_calls'], 0)
        protocol = read(out / 'protocol.draft.json')
        self.assertEqual((protocol['status'], protocol['repeats'], protocol['time_limit_seconds']), ('draft', 3, 900))
        self.assertFalse(check_study(ROOT, out / 'protocol.draft.json')['ready_to_freeze'])
        with self.assertRaisesRegex(ValueError, 'cannot freeze'):
            freeze_study(ROOT, out / 'protocol.draft.json', out / 'readiness.template.json', self.base / 'frozen')
        self.assertFalse((self.base / 'frozen').exists())
        registry = read(out / 'baselines.draft.json')
        self.assertEqual((registry['repeats'], registry['timeout_seconds']), (3, 900))

    def test_explicit_study_batch_enforces_repeat_and_web_policy(self):
        protocol = read(ROOT / 'config/formal-protocol.example.json')
        protocol.update(evaluation_split='dev', required_web_modes=['provided_materials_only'])
        with self.assertRaisesRegex(ValueError, 'repeat/time'):
            batch_run(self.rows, protocol, dict(self.registry, repeats=1), self.base / 'wrong-repeats')
        with self.assertRaisesRegex(ValueError, 'web mode'):
            batch_run(self.rows, dict(protocol, required_web_modes=['historical_only']), self.registry, self.base / 'wrong-web')
        out = self.base / 'study-batch'
        result = batch_run(self.rows, protocol, self.registry, out)
        self.assertEqual(result['attempts'], 12)
        self.assertFalse(result['executed'])

    def test_cli_study_batch_pins_preparation_and_rejects_changed_registry(self):
        task, _ = load_tasks(ROOT, 'dev')[0]
        protocol = read(ROOT / 'config/formal-protocol.example.json')
        protocol.update(evaluation_split='dev', required_web_modes=[task['execution']['web_mode']])
        write(self.base / 'protocol.json', protocol)
        write(self.base / 'registry.json', self.registry)
        out = self.base / 'pinned-study'
        argv = ['batch', '--split', 'dev', '--protocol', str(self.base / 'protocol.json'),
                '--registry', str(self.base / 'registry.json'), '--task', task['id'], '--out', str(out)]
        with patch('benchmark.batch.execute') as execute:
            self.assertEqual(main(argv)['attempts'], 6)
            execute.assert_not_called()
        context = read(out / 'reproduction.json')
        self.assertEqual(context['source'], 'study_protocol')
        self.assertEqual(context['engine_capture'], 'before_execution')
        self.assertEqual(context['registry_file_sha256'], file_hash(self.base / 'registry.json'))
        registry = read(self.base / 'registry.json')
        registry['notes'] = 'Changed configuration file after preparation.'
        write(self.base / 'registry.json', registry)
        with self.assertRaisesRegex(ValueError, 'registry or selection changed'):
            main(argv + ['--resume'])

    def complete_readiness_fixture(self):
        """Synthetic operator declarations, not evidence of the actual benchmark being ready."""
        folder = self.base / 'study'
        prepare_study(ROOT, folder)
        protocol = read(folder / 'protocol.draft.json')
        first, second = self.reviewed('reviewer-a'), self.reviewed('reviewer-b')
        calibration_out, statistics_out = self.base / 'calibration', self.base / 'statistics'
        compare_reviews(ROOT, [first, second], calibration_out)
        report_statistics(ROOT, [first], statistics_out)
        reviewer = read(batch_runs(first)[1][0] / 'review-settings.json')
        write(folder / 'reviewer.json', reviewer)
        write(folder / 'registry.json', self.registry)
        write(folder / 'dataset.json', {'schema_version': '1.0', 'split': 'test', 'tasks': ['SYNTHETIC'],
                                      'fingerprints': {'SYNTHETIC': {'task': '0' * 64, 'rubric': '1' * 64}}})
        protocol.update(primary_reviewer_settings_sha256=digest(reviewer),
            baseline_registry_sha256=file_hash(folder / 'registry.json'), dataset_manifest_sha256=file_hash(folder / 'dataset.json'),
            required_web_modes=['provided_materials_only'], evidence_cutoff='Supplied fixture material only.',
            calibration_acceptance_criteria='Synthetic operator approval for testing checker mechanics only.')
        write(folder / 'protocol.draft.json', protocol)
        runtime = read(folder / 'runtime-acceptance.template.json')
        runtime.update(operator='synthetic-operator', baselines=[row for row in runtime['baselines'] if row['id'] in ('gemini-app', 'kimi-k3')])
        for row in runtime['baselines']:
            row.update(status='passed', model_observed='fixture-ui', client_version='fixture', evidence_references=['synthetic local fixture'])
            row['checks'] = {name: True for name in RUNTIME_CHECKS}
        write(folder / 'runtime.json', runtime)
        write(folder / 'decision.json', {'schema_version': 'benchmark-protocol-decision.1', 'protocol_sha256': digest(protocol),
              'decision': 'approve', 'reviewer': 'synthetic-operator', 'rationale': 'Checker test fixture, not a real approval.'})
        paths = {'dataset_manifest': folder / 'dataset.json', 'calibration': calibration_out / 'calibration.json',
                 'statistics': statistics_out / 'statistics.json', 'runtime_acceptance': folder / 'runtime.json',
                 'baseline_registry': folder / 'registry.json', 'primary_reviewer_settings': folder / 'reviewer.json',
                 'protocol_decision': folder / 'decision.json'}
        assignments = {'dataset': ['dataset_manifest'], 'calibration': ['calibration'], 'stability': ['statistics'],
                       'runtime': ['runtime_acceptance'], 'protocol_decisions': ['baseline_registry', 'primary_reviewer_settings', 'protocol_decision']}
        readiness = {'schema_version': 'benchmark-study-readiness.1', 'protocol_sha256': digest(protocol),
            'gates': [{'id': gate, 'status': 'approved', 'approved_by': 'synthetic-operator', 'reason': 'Fixture only.',
                'artifacts': [{'kind': kind, 'path': str(paths[kind]), 'sha256': file_hash(paths[kind])}
                              for kind in assignments[gate]]} for gate in GATES]}
        write(folder / 'readiness.json', readiness)
        return folder, paths

    def test_evidence_check_and_freeze_preserve_source_and_never_enable_official_results(self):
        folder, paths = self.complete_readiness_fixture()
        before = file_hash(folder / 'protocol.draft.json')
        result = check_study(ROOT, folder / 'protocol.draft.json', folder / 'readiness.json')
        self.assertEqual(result['blocked'], [])
        out = self.base / 'frozen'
        freeze_study(ROOT, folder / 'protocol.draft.json', folder / 'readiness.json', out)
        value = read(out / 'protocol.frozen.json')
        self.assertEqual(value['status'], 'frozen')
        self.assertFalse(value['formal_results_enabled'])
        self.assertEqual(file_hash(folder / 'protocol.draft.json'), before)
        frozen_check = check_study(ROOT, out / 'protocol.frozen.json', out / 'readiness.json')
        self.assertEqual(frozen_check['status'], 'frozen_protocol_checked')
        self.assertFalse(frozen_check['ready_to_freeze'])
        paths['statistics'].write_text('{}')
        with self.assertRaisesRegex(ValueError, 'fingerprint changed'):
            check_study(ROOT, folder / 'protocol.draft.json', folder / 'readiness.json')

    def test_fingerprint_valid_but_incomplete_runtime_is_still_blocked(self):
        folder, paths = self.complete_readiness_fixture()
        value = read(paths['runtime_acceptance'])
        value['baselines'][0]['checks']['trajectory_requirement_met'] = False
        write(paths['runtime_acceptance'], value)
        readiness = read(folder / 'readiness.json')
        for gate in readiness['gates']:
            for artifact in gate['artifacts']:
                if artifact['kind'] == 'runtime_acceptance':
                    artifact['sha256'] = file_hash(paths['runtime_acceptance'])
        write(folder / 'readiness.json', readiness)
        self.assertIn('runtime_acceptance_incomplete', check_study(ROOT, folder / 'protocol.draft.json', folder / 'readiness.json')['blocked'])

    def test_invalid_json_shapes_are_rejected_without_outputs(self):
        for name, value in (('array', []), ('null', None)):
            path = self.base / (name + '.json')
            write(path, value)
            with self.assertRaisesRegex(ValueError, 'must be an object'):
                check_study(ROOT, path)

    def test_ready_gate_cannot_omit_evidence_or_resolve_disputes_without_reasons(self):
        folder, paths = self.complete_readiness_fixture()
        readiness = read(folder / 'readiness.json')
        readiness['gates'][0]['artifacts'] = []
        write(folder / 'readiness-empty.json', readiness)
        blocked = check_study(ROOT, folder / 'protocol.draft.json', folder / 'readiness-empty.json')['blocked']
        self.assertIn('dataset_missing_artifacts', blocked)
        self.assertIn('dataset_missing_required_artifact_kinds', blocked)

        first = self.reviewed('reviewer-c')
        def changed(meta, judgment):
            judgment['nodes'][0]['grade'] = 1
        second = self.reviewed('reviewer-d', changed)
        out = self.base / 'disputed-calibration'
        compare_reviews(ROOT, [first, second], out)
        submitted = read(out / 'adjudication-template.json')
        submitted['decisions'][0].update(status='resolved', reviewer='fixture', reviewer_type='human', resolution='A decision.')
        write(self.base / 'unexplained.json', submitted)
        with self.assertRaisesRegex(ValueError, 'decision and reason'):
            record_adjudication(ROOT, out, self.base / 'unexplained.json', self.base / 'unexplained-record')
        self.assertFalse((self.base / 'unexplained-record').exists())


if __name__ == '__main__':
    unittest.main()
