"""Layered coverage, global assignment and historical weights; synthetic evidence only."""
import copy
import itertools
import random
import unittest
from fractions import Fraction
from pathlib import Path

from benchmark.core import digest, load_tasks
from benchmark.project_matching import POLICY, maximum_assignment, reference_weights
from benchmark.scoring import judgment_template, score

ROOT = Path(__file__).resolve().parents[1]
ANSWER = 'Synthetic project A. Synthetic project B. Synthetic project C.'
EVIDENCE = [{'source': 'Synthetic provided material', 'applicable_cycle': 'fixture',
             'scope': 'Synthetic identity, direction and feasible application path'}]


class MatchingTests(unittest.TestCase):
    def setUp(self):
        task, rubric = next((t, r) for t, r in load_tasks(ROOT, 'dev') if r['reference_candidates'])
        self.task, self.rubric = copy.deepcopy(task), copy.deepcopy(rubric)
        self.rubric.update(coverage_policy=POLICY, forced_pending_reference_ids=[])
        self.refs(2)

    def refs(self, count):
        self.rubric['reference_candidates'] = [{'id': f'R{i}', 'school': 'Synthetic school',
                                                'project': f'Project {i}'} for i in range(count)]
        self.rubric.update(reference_denominator=count, candidate_limit=count)
        self.task['rubric_sha256'] = digest(self.rubric)

    def judgment(self, candidates=2):
        self.task['rubric_sha256'] = digest(self.rubric)
        j = judgment_template(self.task, self.rubric, ANSWER)
        j.update(reviewer='synthetic', judge_version='synthetic', candidates_reviewed=True)
        for node in j['nodes']:
            node.update(grade=0, reason='Synthetic quality omitted')
        for row in j['coverage']:
            row.update(status='miss', reason='No valid relation')
        j['recommendation_review'] = [dict(answer_item_id=f'A{i}', canonical_candidate_key=f'project-{i}',
            school_key='synthetic-school', label=f'Project {i}', answer_quote=ANSWER,
            treatment='direct', validity='valid', reason='Synthetic valid candidate', error_ids=[])
            for i in range(candidates)]
        return j

    def match(self, j, ref, candidate, level):
        row = j['coverage'][ref]
        row.update(status='hit', reason='All feasible synthetic alternatives reviewed')
        row['matches'].append(dict(answer_item_id=f'A{candidate}', match_level=level, answer_quote=ANSWER,
                                   reason='Synthetic relation', relation_evidence=copy.deepcopy(EVIDENCE)))

    def result(self, j):
        return score(self.task, self.rubric, ANSWER, j)

    def test_each_coefficient_and_school_only_miss(self):
        self.refs(4); j = self.judgment(3)
        for ref, level in enumerate(('exact', 'same_department', 'related_department')):
            self.match(j, ref, ref, level)
        result = self.result(j)
        self.assertEqual(result['score'], 33.25)
        self.assertEqual([n['points'] for n in result['nodes'][:4]], [17.5, 10.5, 5.25, 0])
        self.assertEqual([n['match_coefficient'] for n in result['nodes'][:4]], [1, .6, .3, 0])

    def test_global_optimum_beats_first_hit_greedy(self):
        j = self.judgment()
        self.match(j, 0, 0, 'exact')
        self.match(j, 0, 1, 'same_department')
        self.match(j, 1, 0, 'same_department')
        result = self.result(j)
        self.assertEqual(result['score'], 42)
        self.assertEqual([n['matched_answer_item_id'] for n in result['nodes'][:2]], ['A1', 'A0'])
        self.assertIsNone(j['coverage'][0]['matched_answer_item_id'])  # Review is never rewritten.

    def test_aliases_and_duplicate_mentions_cannot_be_counted_twice(self):
        j = self.judgment()
        j['recommendation_review'][1]['canonical_candidate_key'] = 'project-0'
        self.match(j, 0, 0, 'exact'); self.match(j, 1, 1, 'exact')
        result = self.result(j)
        self.assertEqual(result['score'], 35)
        self.assertEqual(result['actual_candidate_count'], 1)
        self.assertEqual(sum(n['status'] == 'hit' for n in result['nodes'][:2]), 1)

    def test_highest_verified_project_outcome_is_normalized_and_used_in_assignment(self):
        self.rubric['reference_candidates'][1]['historical_outcomes'] = [
            dict(verified=True, reference_id='R1', status=status, source='fixture',
                 applicable_cycle='fixture', scope='Verified synthetic project result')
            for status in ('camp', 'admission', 'excellent_camp')]
        self.assertEqual(reference_weights(self.rubric), {'R0': Fraction(35, 2), 'R1': Fraction(105, 2)})
        j = self.judgment(1)
        self.match(j, 0, 0, 'exact'); self.match(j, 1, 0, 'same_department')
        self.assertEqual(self.result(j)['score'], 31.5)
        j = self.judgment()
        self.match(j, 0, 0, 'exact'); self.match(j, 1, 1, 'exact')
        self.assertEqual(self.result(j)['score'], 70)

    def test_unverified_outcomes_do_not_change_weights_and_verified_need_project_evidence(self):
        ref = self.rubric['reference_candidates'][1]
        ref['historical_outcomes'] = [dict(verified=False, status='admission')]
        self.assertEqual(reference_weights(self.rubric), {'R0': 35, 'R1': 35})
        ref['historical_outcomes'][0]['verified'] = True
        with self.assertRaisesRegex(ValueError, '绑定具体参考项目'):
            reference_weights(self.rubric)
        ref['historical_outcomes'][0]['reference_id'] = 'R1'
        with self.assertRaisesRegex(ValueError, '缺少证据'):
            reference_weights(self.rubric)

    def test_forced_pending_survives_and_preserves_conservative_interval(self):
        self.rubric['forced_pending_reference_ids'] = ['R1']
        j = self.judgment(1); j['coverage'][1]['status'] = 'pending'
        self.match(j, 0, 0, 'same_department')
        result = self.result(j)
        self.assertIsNone(result['score'])
        self.assertEqual(result['score_interval'], [21, 56])
        self.assertEqual(result['pending_nodes'], ['R1'])
        j['coverage'][1]['status'] = 'miss'
        with self.assertRaisesRegex(ValueError, '强制待核'):
            self.result(j)

    def test_positive_matches_require_valid_candidates_quotes_and_relation_evidence(self):
        j = self.judgment(); self.match(j, 0, 0, 'exact')
        for change, error in [('level', '匹配等级'), ('evidence', '可核验依据'), ('quote', '引句不在'),
                              ('candidate', '有效直接推荐'), ('preselected', '不能预填')]:
            bad = copy.deepcopy(j)
            match = bad['coverage'][0]['matches'][0]
            if change == 'level': match['match_level'] = 'school_only'
            elif change == 'evidence': match['relation_evidence'] = []
            elif change == 'quote': match['answer_quote'] = 'fabricated quote'
            elif change == 'candidate': bad['recommendation_review'][0]['treatment'] = 'mentioned'
            else: bad['coverage'][0]['matched_answer_item_id'] = 'A0'
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                self.result(bad)

    def test_legacy_hit_without_layered_review_is_rejected(self):
        j = self.judgment(); j['coverage'][0].update(status='hit', answer_quote=ANSWER)
        with self.assertRaisesRegex(ValueError, '命中须有匹配方案'):
            self.result(j)

    def test_missing_relation_review_cannot_be_silently_treated_as_zero(self):
        j = self.judgment(); j['coverage'][0].pop('matches')
        with self.assertRaisesRegex(ValueError, 'matches数组'):
            self.result(j)

    def test_empty_answer_gets_zero_except_for_forced_pending(self):
        j = judgment_template(self.task, self.rubric, '')
        j.update(reviewer='synthetic', judge_version='synthetic', candidates_reviewed=True)
        for row in j['nodes']: row.update(grade=0, reason='Empty answer')
        for row in j['coverage']: row.update(status='miss', reason='Empty answer')
        self.assertEqual(score(self.task, self.rubric, '', j)['score'], 0)


class AssignmentOracleTests(unittest.TestCase):
    def test_small_graphs_agree_with_exhaustive_one_to_one_search(self):
        rng = random.Random(7)
        for size in range(1, 5):
            refs, keys = [f'R{i}' for i in range(size)], [f'K{i}' for i in range(size)]
            for trial in range(24):
                edges = {(r, k): Fraction(rng.choice((3, 6, 10, 18, 30)), 7)
                         for r in refs for k in keys if rng.random() < .65}
                expected = Fraction(0)
                for assignment in itertools.product([None, *keys], repeat=size):
                    actual = [k for k in assignment if k is not None]
                    if len(set(actual)) != len(actual): continue
                    if any(k is not None and (r, k) not in edges for r, k in zip(refs, assignment)): continue
                    value = sum((edges[(r, k)] for r, k in zip(refs, assignment) if k is not None), Fraction(0))
                    expected = max(expected, value)
                result = maximum_assignment(refs, edges)
                with self.subTest(size=size, trial=trial):
                    self.assertEqual(sum((edges[item] for item in result.items()), Fraction(0)), expected)
                    self.assertEqual(len(set(result.values())), len(result))


if __name__ == '__main__':
    unittest.main()
