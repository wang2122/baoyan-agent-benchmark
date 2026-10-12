"""Checks for the answer-only publication boundary; no model calls."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_publication import export_public_answers, put, sha
from scripts.export_public_answers import export


class PublicAnswerExportTests(unittest.TestCase):
    def test_public_capture_excludes_logs_prompts_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            source.mkdir()
            bodies = {'answer.md': '原始答案\n', 'final-response.md': 'Final answer\n',
                      'trace.jsonl': '{"private":"tool transcript"}\n',
                      'events.jsonl': '{"private":"conversation"}\n',
                      'query.md': 'prompt', 'run.json': '{"session_id":"private"}',
                      'stderr.log': 'private', 'checkpoint.json': '{"token":"private"}'}
            for name, body in bodies.items():
                (source / name).write_text(body, encoding='utf-8')
            hashes = {name: sha((source / name).read_bytes()) for name in bodies}
            out = base / 'public'
            entries = export_public_answers(source, hashes, out)
            self.assertEqual({x['path'] for x in entries}, {'answer.md', 'final-response.md'})
            self.assertEqual({p.name for p in out.iterdir()},
                             {'answer.md', 'final-response.md', 'artifacts.json'})
            self.assertEqual((out / 'answer.md').read_bytes(), (source / 'answer.md').read_bytes())
            (source / 'answer.md').write_text('changed')
            with self.assertRaises(ValueError):
                export_public_answers(source, hashes, base / 'changed')

    def test_dev70_uses_selected_stage_answers_and_excludes_failed_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            record = source / 'materials/records/agent/archive'
            put(record / 'record.json', {'agent': 'agent', 'configuration': {'model': 'model'},
                'records': [
                    {'task_id': 'TASK', 'status': 'completed',
                     'selected_answers': ['captures/TASK/selected-answer/S0/answer.md',
                                          'captures/TASK/selected-answer/S1/answer.md']},
                    {'task_id': 'WITHDRAWN', 'status': 'completed',
                     'selected_answers': ['missing-withdrawn-answer.md']},
                    {'task_id': 'FAIL', 'status': 'failed',
                     'selected_answers': ['failures/answer.md']} ]})
            for stage in ('S0', 'S1'):
                p = record / f'captures/TASK/selected-answer/{stage}/answer.md'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f'answer {stage}\n')
            (record / 'failures').mkdir()
            (record / 'failures/answer.md').write_text('partial answer')
            put(record / 'captures/TASK/trace.jsonl', {'secret': 'not public'})
            out = base / 'public'
            result = export(source, out, 'selected-historical-answers', allowed_tasks={'TASK', 'FAIL'})
            self.assertEqual(result['answer_records'], 1)
            manifest = json.loads((out / 'sources/selected-historical-answers/manifest.json').read_text())
            self.assertEqual(len(manifest['records']), 2)
            complete, failed = manifest['records']
            self.assertEqual(len(complete['answers']), 2)
            self.assertTrue(all('/runs/native-batch-archive/' in a for a in complete['answers']))
            self.assertTrue(all((out / a).is_file() for a in complete['answers']))
            self.assertEqual([Path(a).parent.name for a in complete['answers']], ['S0', 'S1'])
            self.assertFalse((out / 'dev70').exists())
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['answers'], [])
            self.assertIsNone(failed['score'])
            self.assertFalse(list(out.rglob('trace.jsonl')))
            self.assertFalse(list(out.rglob('record.json')))
            with self.assertRaises(ValueError):
                export(source, out, 'selected-historical-answers', allowed_tasks={'TASK', 'FAIL'})

    def test_dev20_keeps_final_adjudicated_score_and_deduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'source'
            materials = source / 'materials'
            packets = materials / 'review-materials'
            put(packets / 'index.json', {'records': [{'agent': 'agent', 'task_id': 'TASK',
                'packet': 'packet', 'packet_path': 'review/packets/packet'}]})
            p = packets / 'review/packets/packet/answer.md'
            p.parent.mkdir(parents=True)
            p.write_text('original answer\n')
            put(materials / 'review-final/results.json', {
                'selection_id': 'historical', 'generated_at': '2026-10-10', 'reviewer': {},
                'summary': [],
                'unresolved_facts': [{'task_id': 'WITHDRAWN', 'claim': 'excluded'},
                                     {'task_id': 'TASK', 'claim': 'retained'}],
                'adjudication': {'task_id': 'WITHDRAWN', 'reason': 'excluded'},
                'rows': [{'agent': 'agent', 'task_id': 'TASK', 'packet': 'packet',
                    'score': 77.5, 'deductions': [{'node': 'LANG', 'reason': 'Final adjudication'}]}]})
            out = base / 'public'
            export(source, out, 'retained-early-scores', allowed_tasks={'TASK'})
            score = json.loads((out / 'models/agent/runs/historical-reviewed/TASK/score.json').read_text())
            meta = json.loads((out / 'models/agent/runs/historical-reviewed/manifest.json').read_text())
            self.assertFalse((out / 'sources/retained-early-scores').exists())
            scores = {'rows': [score], 'unresolved_facts': meta['unresolved_facts'], 'adjudication': meta['adjudication']}
            self.assertEqual(scores['rows'][0]['score'], 77.5)
            self.assertEqual(json.loads((out / 'models/agent/runs/historical-reviewed/TASK/score.json').read_text()), scores['rows'][0])
            self.assertEqual((out / 'models/agent/runs/historical-reviewed/TASK/answer.md').read_text(), 'original answer\n')
            self.assertEqual(scores['rows'][0]['deductions'][0]['reason'], 'Final adjudication')
            self.assertNotIn('packet', scores['rows'][0])
            self.assertEqual(scores['unresolved_facts'], [{'task_id': 'TASK', 'claim': 'retained'}])
            self.assertEqual(scores['adjudication'], {})


if __name__ == '__main__':
    unittest.main()
