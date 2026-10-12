"""Dataset boundaries and reproducibility; all answers here are synthetic."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from benchmark.core import candidate, candidate_prompt, collection, digest, file_hash, load_tasks, read, write
from benchmark.dataset import export_hf
from benchmark.packaging import judge_package, task_package
from benchmark.runner import aggregate, prepare
from benchmark.scoring import judgment_template, score

ROOT = Path(__file__).resolve().parents[1]
SINGLE_TASKS = len(read(ROOT / 'data/dev/manifest.json')['tasks'])
ANSWER = 'Synthetic answer for scoring tests only.'


def zero_judgment(task, rubric):
    result = judgment_template(task, rubric, ANSWER)
    result.update(reviewer='synthetic-reviewer', judge_version='synthetic-v1', candidates_reviewed=True)
    for row in result['nodes']:
        row.update(grade=0, reason='Synthetic answer omits the requirement')
    for row in result['coverage']:
        row.update(status='pending' if row['id'] in rubric['forced_pending_reference_ids'] else 'miss',
                   reason='Reference identity requires review' if row['id'] in rubric['forced_pending_reference_ids']
                   else 'Synthetic answer omits the candidate')
    return result


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_tasks(ROOT, 'dev')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_public_inventory_and_candidate_projection(self):
        self.assertEqual(len(self.rows), SINGLE_TASKS)
        self.assertEqual(len({t['case_group_id'] for t, _ in self.rows}), 23)
        for task, rubric in self.rows:
            self.assertNotIn('source', task)
            public = candidate(task)
            self.assertFalse({'review', 'rubric_sha256', 'difficulty', 'case_group_id'} & public.keys())
            self.assertEqual(sum(n['points'] for n in rubric['nodes']) + rubric['coverage_points'], 100)

    def test_candidate_package_excludes_evaluator_materials(self):
        task, rubric = self.rows[0]
        task_package(task, rubric, self.base / 'package')
        workspace = self.base / 'package/workspace'
        self.assertEqual(set(p.name for p in workspace.iterdir()), {'task.json', 'query.md'})
        self.assertEqual(read(workspace / 'task.json'), candidate(task))
        self.assertTrue((self.base / 'package/eval/scoring.md').exists())

    def test_complete_run_keeps_forced_pending_out_of_numeric_mean(self):
        protocol = read(ROOT / 'config/protocol.json')
        directories = []
        for task, rubric in self.rows:
            out = prepare(task, rubric, self.base / task['id'], protocol)
            answer_path = out / 'workspace/answer.md'
            answer_path.write_text(ANSWER)
            meta = read(out / 'run.json')
            meta.update(status='completed', adapter='synthetic', adapter_version='v1', model='no-model',
                        repeat=1, timeout_seconds=60, answer_file_sha256=file_hash(answer_path))
            write(out / 'run.json', meta)
            judgment = zero_judgment(task, rubric)
            result = score(task, rubric, ANSWER, judgment)
            self.assertEqual(result['score'], None if rubric['forced_pending_reference_ids'] else 0)
            write(out / 'judgment.json', judgment)
            write(out / 'score.json', result)
            directories.append(out)
        report = aggregate(self.rows, directories, protocol)
        self.assertEqual(report['rows'][0]['attempted_tasks'], SINGLE_TASKS)
        self.assertEqual(report['rows'][0]['scored_tasks'], SINGLE_TASKS - 1)
        self.assertEqual(report['rows'][0]['pending_or_missing_tasks'], 1)
        self.assertIsNone(report['rows'][0]['mean_score'])
        self.assertFalse(report['official'])
        incomplete = aggregate(self.rows, directories[:-1], protocol)
        self.assertIsNone(incomplete['rows'][0]['mean_score'])
        with self.assertRaisesRegex(ValueError, 'Duplicate task attempt'):
            aggregate(self.rows, directories + directories[:1], protocol)

    def test_full_credit_pending_and_stale_answer(self):
        task, rubric = next((t, r) for t, r in self.rows if t['task_kind'] == 'planning')
        judgment = zero_judgment(task, rubric)
        for node in judgment['nodes']:
            node.update(grade=1, answer_quote=ANSWER, reason='Synthetic full-credit judgment')
        self.assertEqual(score(task, rubric, ANSWER, judgment)['score'], 100)
        judgment['nodes'][0]['grade'] = 'pending'
        self.assertIsNone(score(task, rubric, ANSWER, judgment)['score'])
        with self.assertRaises(ValueError):
            score(task, rubric, ANSWER + ' edited', judgment)

    def test_legacy_one_candidate_cannot_cover_two_references(self):
        task, rubric = copy.deepcopy(next((t, r) for t, r in self.rows if t['task_kind'] == 'recommendation'))
        rubric.pop('coverage_policy', None)
        task['rubric_sha256'] = digest(rubric)
        judgment = zero_judgment(task, rubric)
        judgment['recommendation_review'] = [dict(answer_item_id='item-1', canonical_candidate_key='school|project',
            school_key='school', label='synthetic', answer_quote=ANSWER, treatment='direct', validity='valid',
            reason='Synthetic candidate', error_ids=[])]
        for row in judgment['coverage'][:2]:
            row.update(status='hit', matched_answer_item_id='item-1', answer_quote=ANSWER)
        with self.assertRaisesRegex(ValueError, '不能重复覆盖'):
            score(task, rubric, ANSWER, judgment)

    def test_huggingface_export_is_generated_from_canonical_data(self):
        out = self.base / 'hf'
        export_hf(ROOT, out)
        samples = [json.loads(line) for line in (out / 'dev.jsonl').read_text().splitlines()]
        self.assertEqual(len(samples), SINGLE_TASKS)
        self.assertFalse((out / 'test.jsonl').exists())
        self.assertEqual(read(out / 'manifest.json')['split'], 'dev')
        for sample, (task, rubric) in zip(samples, self.rows):
            self.assertEqual(sample['metadata']['split'], 'dev')
            self.assertEqual(sample['input'], candidate_prompt(task))
            self.assertEqual(sample['rubric'], rubric)
            self.assertEqual(sample['reference'], rubric['reference_candidates'])
        self.assertEqual(file_hash(out / 'scoring.md'), file_hash(ROOT / 'docs/scoring.md'))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            export_hf(ROOT, out)

    def test_private_storage_and_case_group_boundary(self):
        with self.assertRaisesRegex(ValueError, 'outside the repository'):
            load_tasks(ROOT, 'test', ROOT / 'data/test')
        task, rubric = copy.deepcopy(next((t, r) for t, r in self.rows if t['task_kind'] == 'planning'))
        task.update(id='FIXTURE-PRIVATE', split='test')
        rubric['id'] = task['id']
        task['rubric_sha256'] = digest(rubric)
        private = self.base / 'private'
        write(private / task['id'] / 'task.json', task)
        write(private / task['id'] / 'rubric.json', rubric)
        def manifest():
            write(private / 'manifest.json', {'schema_version': '1.0', 'split': 'test',
                  'tasks': [task['id']], 'fingerprints': {task['id']: {'task': digest(task), 'rubric': digest(rubric)}}})
        manifest()
        with self.assertRaisesRegex(ValueError, 'Related cases cross splits'):
            collection(ROOT, private)
        task['case_group_id'] = 'fixture-private-group'
        write(private / task['id'] / 'task.json', task)
        manifest()
        self.assertEqual(len(collection(ROOT, private)['test']), 1)
        self.assertNotIn('test', collection(ROOT))

    def test_private_snapshots_cannot_be_written_inside_public_checkout(self):
        task, rubric = copy.deepcopy(self.rows[0])
        task['split'] = 'test'
        out = ROOT / 'runs/forbidden-private-fixture'
        calls = [lambda: prepare(task, rubric, out, {}),
                 lambda: task_package(task, rubric, out),
                 lambda: judge_package(task, rubric, ANSWER, out)]
        for call in calls:
            with self.assertRaisesRegex(ValueError, 'outside the public repository'):
                call()
        self.assertFalse(out.exists())

    def test_tampered_task_is_rejected(self):
        shutil.copytree(ROOT / 'data', self.base / 'data')
        shutil.copytree(ROOT / 'docs', self.base / 'docs')
        task = copy.deepcopy(self.rows[0][0])
        task['prompt'] += ' edited'
        write(self.base / 'data/dev' / task['id'] / 'task.json', task)
        with self.assertRaisesRegex(ValueError, 'Dataset changed'):
            load_tasks(self.base, 'dev')


if __name__ == '__main__':
    unittest.main()
