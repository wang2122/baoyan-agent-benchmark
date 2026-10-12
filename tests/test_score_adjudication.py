"""Offline safeguards for the explicit maintenance layer; no model calls."""
import copy
from fractions import Fraction
import unittest
from benchmark.core import digest
from benchmark.scoring import rounded
from scripts.score_adjudication import replay


class NodeAdjudicationTests(unittest.TestCase):
    def fixture(self):
        rubric = {'id': 'T', 'coverage_points': 70, 'reference_denominator': 3,
                  'reference_candidates': [{'id': x} for x in ('C1', 'C2', 'C3')],
                  'nodes': [{'id': 'Q', 'points': 30, 'grading': 'three_level',
                             'full_credit': 'Complete', 'half_credit': 'Partly complete'}]}
        score = {'task_id': 'T', 'answer_sha256': 'answer', 'rubric_sha256': 'original-rubric',
                 'status': 'pending_evidence', 'pending_nodes': ['C2', 'Q'],
                 'score_interval': [14.0, 67.33],
                 'nodes': [{'id': 'C1', 'kind': 'coverage', 'status': 'hit', 'points': 14.0,
                            'weight': {'numerator': 70, 'denominator': 3}, 'match_coefficient': .6},
                           {'id': 'C2', 'kind': 'coverage', 'status': 'pending', 'points': None,
                            'weight': {'numerator': 70, 'denominator': 3}},
                           {'id': 'C3', 'kind': 'coverage', 'status': 'miss', 'points': 0,
                            'weight': {'numerator': 70, 'denominator': 3}},
                           {'id': 'Q', 'kind': 'quality', 'grade': 'pending', 'points': None}]}
        ruling = {'node_id': 'Q', 'status': 'resolved_by_official_evidence', 'grade': .5,
                  'points': 15, 'rule': copy.deepcopy(rubric['nodes'][0]), 'decision': 'Relevant core partly complete',
                  'sources': [{'url': 'https://example.edu/official'}]}
        ledger = {'schema': 'node-adjudication-ledger.1', 'task_id': 'T', 'answer_sha256': 'answer',
                  'original_rubric_sha256': 'original-rubric', 'original_score_content_sha256': digest(score),
                  'original_rubric_content_sha256': digest(rubric), 'rulings': [ruling]}
        return score, rubric, ledger

    def test_exact_fraction_preserves_pending_and_original_objects(self):
        score, rubric, ledger = self.fixture(); before = copy.deepcopy((score, rubric, ledger))
        result = replay(score, rubric, ledger)
        self.assertEqual(result['score_interval'], [29.0, 52.33])
        self.assertEqual(result['pending_nodes'], ['C2'])
        self.assertEqual((score, rubric, ledger), before)
        self.assertEqual(result['nodes'][0]['original_node'], score['nodes'][0])

    def test_policy_zero_retains_unresolved_fact_and_is_separate(self):
        score, rubric, ledger = self.fixture()
        ledger['authorized_policy_nodes'] = ['C2']
        ledger['rulings'].append({'node_id': 'C2', 'status': 'owner_policy_no_verified_credit', 'grade': 0,
                                 'points': 0, 'decision': 'No verified credit', 'factual_status': 'unresolved',
                                 'policy_authorization': 'Explicit instruction for C2'})
        result = replay(score, rubric, ledger)
        self.assertEqual(result['score_interval'], [29.0, 29.0])
        self.assertEqual(score['nodes'][1]['status'], 'pending')
        self.assertEqual(result['nodes'][1]['disposition'], 'owner_policy_no_verified_credit')

    def test_rejects_settlement_of_confirmed_node(self):
        score, rubric, ledger = self.fixture(); ledger['rulings'][0]['node_id'] = 'C1'
        with self.assertRaisesRegex(ValueError, 'Only pending'): replay(score, rubric, ledger)

    def test_rejects_off_grid_grade_and_false_weight(self):
        score, rubric, ledger = self.fixture(); ledger['rulings'][0]['grade'] = .75
        with self.assertRaisesRegex(ValueError, 'grading levels'): replay(score, rubric, ledger)
        score, rubric, ledger = self.fixture(); ledger['rulings'][0]['points'] = 16
        with self.assertRaisesRegex(ValueError, 'original weight'): replay(score, rubric, ledger)

    def test_rejects_missing_policy_permission_and_resolved_fact_claim(self):
        score, rubric, ledger = self.fixture()
        ledger['rulings'] = [{'node_id': 'C2', 'status': 'owner_policy_no_verified_credit', 'grade': 0, 'points': 0,
                              'decision': 'No verified credit', 'factual_status': 'unresolved'}]
        with self.assertRaisesRegex(ValueError, 'authorization'): replay(score, rubric, ledger)
        ledger['authorized_policy_nodes'] = ['C2'];ledger['rulings'][0]['policy_authorization'] = 'Explicit'
        ledger['rulings'][0]['factual_status'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'resolved fact'): replay(score, rubric, ledger)

    def test_rejects_wrong_answer_or_mutated_original(self):
        score, rubric, ledger = self.fixture();ledger['answer_sha256'] = 'another-answer'
        with self.assertRaisesRegex(ValueError, 'Answer mismatch'): replay(score, rubric, ledger)
        score, rubric, ledger = self.fixture();score['score_interval'][0] = 15
        with self.assertRaisesRegex(ValueError, 'content changed'): replay(score, rubric, ledger)

    def test_rejects_duplicate_ruling_and_wrong_rubric_text(self):
        score, rubric, ledger = self.fixture();ledger['rulings'] *= 2
        with self.assertRaisesRegex(ValueError, 'Duplicate ruling'): replay(score, rubric, ledger)
        score, rubric, ledger = self.fixture();ledger['rulings'][0]['rule']['half_credit'] = 'Different rule'
        with self.assertRaisesRegex(ValueError, 'original rubric rule'): replay(score, rubric, ledger)

    def test_quantization_is_once_after_sum(self):
        score, rubric, ledger = self.fixture()
        score['nodes'][0].update(match_coefficient=1, points=23.33)
        score['nodes'][2].update(status='hit', match_coefficient=1, points=23.33)
        score['score_interval'] = [46.67, 100.0];ledger['original_score_content_sha256'] = digest(score)
        result = replay(score, rubric, ledger)
        self.assertEqual(result['score_interval'], [61.67, 85.0])
        self.assertNotEqual(result['score_interval'][0], sum(n['effective_points'] or 0 for n in result['nodes']))


if __name__ == '__main__': unittest.main()
