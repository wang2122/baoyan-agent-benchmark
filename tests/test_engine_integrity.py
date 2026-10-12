"""Organizer state and malformed adapter artifacts; no provider calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

from benchmark.adapters import ROOT, command, parse_output
from benchmark.core import load_tasks, read, write
from benchmark.runner import execute, prepare


class EngineIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.row = load_tasks(ROOT, 'dev')[0]
        self.protocol = read(ROOT / 'config/protocol.json')

    def attempt(self, name, artifacts, exit_code=0):
        out = prepare(*self.row, self.base / name, self.protocol)
        source = '\n'.join(['from pathlib import Path'] +
                           [f'Path({name!r}).write_bytes({data!r})' for name, data in artifacts.items()] +
                           [f'raise SystemExit({exit_code})'])
        return out, execute(out, [sys.executable, '-c', source], 'fixture', 'v1', 'no-model', 1, 10)

    def test_failed_atomic_replace_preserves_previous_record_and_permissions(self):
        path = self.base / 'run.json'
        write(path, {'status': 'prepared'})
        path.chmod(0o640)
        with patch('benchmark.core.os.replace', side_effect=OSError('injected write failure')):
            with self.assertRaises(OSError):
                write(path, {'status': 'running'})
        self.assertEqual(read(path), {'status': 'prepared'})
        self.assertEqual(list(self.base.iterdir()), [path])
        write(path, {'status': 'completed'})
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_json_write_rejects_symlinks_without_changing_the_target(self):
        target = self.base / 'target.json'; write(target, {'original': True})
        link = self.base / 'link.json'; link.symlink_to(target)
        with self.assertRaises(ValueError):
            write(link, {'original': False})
        self.assertTrue(link.is_symlink())
        self.assertEqual(read(target), {'original': True})

    def test_corrupt_metadata_finishes_the_attempt_and_preserves_answer(self):
        for name, value in [('model-observation.json', b'{'), ('failure.json', b'[]'),
                            ('model-observation.json', b'{"status":[]}')]:
            with self.subTest(name=name, value=value):
                out, meta = self.attempt(str(len(list(self.base.iterdir()))), {
                    'answer.md': b'Original fixture answer.', 'trajectory.jsonl': b'{"type":"fixture"}\n', name: value})
                self.assertEqual(meta['status'], 'failed')
                self.assertEqual(read(out / 'run.json')['status'], 'failed')
                self.assertIn(name, meta['failure']['artifacts'])
                self.assertEqual((out / 'workspace/answer.md').read_bytes(), b'Original fixture answer.')
                self.assertTrue(meta['answer_file_sha256'])

    def test_required_trace_and_invalid_utf8_cannot_claim_completion(self):
        for name, artifacts in [('missing-trace', {'answer.md': b'Answer'}),
                                ('invalid-trace', {'answer.md': b'Answer', 'trajectory.jsonl': b'not json'}),
                                ('invalid-answer', {'answer.md': b'\xff', 'trajectory.jsonl': b'{}\n'})]:
            with self.subTest(name=name):
                out, meta = self.attempt(name, artifacts)
                self.assertEqual(meta['status'], 'failed')
                self.assertTrue(meta['trajectory_required'])
                self.assertEqual(read(out / 'run.json')['status'], 'failed')
                self.assertEqual((out / 'workspace/answer.md').read_bytes(), artifacts['answer.md'])

    def test_optional_trace_and_original_process_failure_are_preserved(self):
        task, rubric = self.row
        self.row = (dict(task, execution=dict(task['execution'], trajectory_required=False)), rubric)
        _, meta = self.attempt('optional', {'answer.md': b'Answer'})
        self.assertEqual(meta['status'], 'completed')
        self.assertEqual(meta['trajectory_status'], 'missing')
        _, meta = self.attempt('failed', {'model-observation.json': b'{'}, 7)
        self.assertEqual(meta['status'], 'failed')
        self.assertEqual(meta['exit_code'], 7)

    def test_claude_stream_retains_tool_events_and_requires_a_final_success(self):
        events = [{'type': 'system', 'subtype': 'init'},
                  {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}},
                  {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'source'}]}},
                  {'type': 'result', 'subtype': 'success', 'result': 'Original answer',
                   'modelUsage': {'fixture-model': {}}, 'usage': {'input_tokens': 2, 'output_tokens': 3}}]
        stream = '\n'.join(json.dumps(e) for e in events)
        answer, captured, models, usage = parse_output('claude', stream)
        self.assertEqual(captured, events)
        self.assertEqual(answer, 'Original answer')
        self.assertEqual(models, ['fixture-model'])
        self.assertEqual(usage['output_tokens'], 3)
        argv = command({'client': 'claude', 'executable': '/fake', 'model': 'fixture-model'}, {}, self.base / 'answer.md')
        self.assertEqual(argv[argv.index('--output-format') + 1], 'stream-json')
        self.assertIn('--verbose', argv)
        for invalid in (events[:-1], events + [{'type': 'assistant'}], [None], events + [events[-1]]):
            with self.assertRaises(ValueError):
                parse_output('claude', '\n'.join(json.dumps(e) for e in invalid))
        with self.assertRaises(ValueError):
            parse_output('codex', '[]')


if __name__ == '__main__':
    unittest.main()
