"""Candidate-visible facts, delivery parity, and frozen-input compatibility."""
import copy
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from benchmark.core import (CANDIDATE_INPUT_VERSION, candidate, candidate_prompt,
                            input_hash, load_tasks, query_prompt, read, write)
from benchmark.cli_packet import export_packets
from benchmark.manual import prepare_app_session
from benchmark.packaging import task_package
from benchmark.runner import prepare
from benchmark.longitudinal import staged_snapshot, staged_prompt

ROOT = Path(__file__).resolve().parents[1]


class CandidateInputTests(unittest.TestCase):
    def test_all_current_inputs_retain_facts_without_serializing_metadata(self):
        rows = load_tasks(ROOT, 'dev')
        for ident in read(ROOT / 'examples/public-smoke/manifest.json')['tasks']:
            folder = ROOT / 'examples/public-smoke' / ident
            rows.append((read(folder / 'task.json'), read(folder / 'rubric.json')))
        for task, _ in rows:
            with self.subTest(task=task['id']):
                self.assertEqual(task['execution']['prompt_version'], CANDIDATE_INPUT_VERSION)
                prompt = candidate_prompt(task)
                self.assertEqual(prompt.count(task['prompt'].strip()), 1)
                for fields in task['context'].values():
                    for value in fields.values():
                        self.assertIn(value, prompt)
                for field in ('"context"', '"execution"', '"deliverables"', '"review"', '"task_version"'):
                    self.assertNotIn(field, prompt)
                self.assertIn('answer.md', prompt)
                # Adapters also accept projected task.json. It must render identically.
                self.assertEqual(candidate_prompt(candidate(task)), prompt)
                self.assertEqual(query_prompt(task), prompt)
                self.assertEqual(candidate(task)['context'], {'persona': {}, 'observation': {}})

    def test_revision_keeps_question_once_and_legacy_remains_reproducible(self):
        task = read(ROOT / 'data/dev/TRY-E-03/task.json')
        prompt = candidate_prompt(task)
        self.assertEqual(prompt.count(task['prompt']), 1)
        self.assertEqual(prompt.count('专业排名前约45%'), 1)
        self.assertNotIn('id', candidate(task))
        # Synthetic legacy facts exercise the previous duplicate rendering contract.
        legacy = copy.deepcopy(task)
        legacy['task_version'] = '1.0.1'
        legacy['prompt'] = '请依据示例背景作答。'
        legacy['context'] = {'persona': {'背景': '示例背景'}, 'observation': {}}
        modern = copy.deepcopy(legacy)
        legacy['execution'].pop('prompt_version')
        self.assertEqual(candidate_prompt(legacy).count('示例背景'), 3)
        self.assertEqual(candidate_prompt(modern).count('示例背景'), 1)
        self.assertEqual(query_prompt(legacy), legacy['prompt'])
        self.assertNotEqual(input_hash(modern), input_hash(legacy))

    def test_unembedded_time_and_additional_context_are_not_lost(self):
        task = read(ROOT / 'data/dev/N01-01/task.json')
        task['context']['observation']['本轮新增条件'] = '每周只能投入六小时，暂不申请直博。'
        prompt = candidate_prompt(task)
        self.assertEqual(prompt.count('2026年推免申请季'), 1)
        self.assertNotIn('评测时间：', prompt)
        self.assertEqual(prompt.count('每周只能投入六小时，暂不申请直博。'), 1)

    def test_unknown_input_version_fails_closed(self):
        task = read(ROOT / 'data/dev/TRY-E-03/task.json')
        task['execution']['prompt_version'] = 'unsupported'
        with self.assertRaisesRegex(ValueError, 'Unsupported candidate input version'):
            candidate_prompt(task)

    def test_manual_and_workspace_exports_have_exactly_the_sent_input(self):
        task = read(ROOT / 'data/dev/TRY-E-03/task.json')
        rubric = read(ROOT / 'data/dev/TRY-E-03/rubric.json')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run = prepare(task, rubric, base / 'run', read(ROOT / 'config/protocol.json'))
            prepare_app_session(run, task, {'agent_product': 'fixture'})
            task_package(task, rubric, base / 'package')
            for path in (run / 'workspace/query.md', run / 'workspace/app-prompt.md',
                         base / 'package/workspace/query.md'):
                self.assertEqual(path.read_text(), candidate_prompt(task))
            self.assertEqual(read(run / 'task.json')['context'], task['context'])

    def test_second_stage_facts_remain_out_of_the_first_input(self):
        snap = staged_snapshot(ROOT, 'O01-04')
        self.assertNotIn('递补资格', snap['stages'][0]['prompt'])
        self.assertIn('本校推免资格', snap['stages'][1]['prompt'])
        self.assertNotIn('本轮时点：', snap['stages'][1]['prompt'])
        self.assertEqual(snap['stages'][1]['prompt'].count('2026年9月'), 1)

    def test_staged_header_is_versioned(self):
        task = {'task_version': '1.0.1', 'constraints': ['只使用当前阶段信息。']}
        stage = {'visible_at': '2026年3月', 'instruction': '现在是2026年3月，请给出建议。'}
        expected = '本轮时点：2026年3月\n\n现在是2026年3月，请给出建议。\n\n约束：\n只使用当前阶段信息。'
        self.assertEqual(staged_prompt(task, stage), expected)
        task['task_version'] = '1.0.2'
        self.assertEqual(staged_prompt(task, stage), stage['instruction'] + '\n\n只使用当前阶段信息。')

    def test_portable_cli_packet_uses_the_same_plain_text_message(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            registry = read(ROOT / 'config/agents-cli.example.json')
            agent = registry['agents'][0]
            agent['cli_version'] = 'offline-fixture'
            write(base / 'registry.json', registry)
            result = export_packets(ROOT, base / 'packets', base / 'registry.json',
                                    [agent['id']], ['TRY-E-03'])
            with ZipFile(result['packets'][0]['candidate_zip']) as archive:
                text = archive.read('questions/TRY-E-03.md').decode()
                task = read(ROOT / 'data/dev/TRY-E-03/task.json')
                self.assertEqual(text, candidate_prompt(task))
                self.assertFalse(any('rubric' in name for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
