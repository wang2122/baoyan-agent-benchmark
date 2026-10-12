"""Deliverable/final-reply regressions, with no provider calls or credentials."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark import cli_adapters, native_runtime
from tests.test_native import ANSWER, fake_events
from tests.test_full_batch import agent


BODY = '完整答卷\r\n\r\n具体院校、条件、证据与申请安排。\r\n'.encode('utf-8')
REPLY = '已完成，完整正文见 answer.md。'


class AnswerCaptureTests(unittest.TestCase):
    def capture(self, base, cli='codex', body=BODY, failure=None, required=True, linked=False):
        class Sandbox:
            def __init__(self, *args, **kwargs):
                self.work, self.home, self.scratch = (base / n for n in ('work', 'home', 'scratch'))
                for p in (self.work, self.home, self.scratch):
                    p.mkdir()
                (self.home / '.kimi-code').mkdir()
                self.report = {'status': 'not_checked', 'cleanup': 'pending'}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.report['cleanup'] = 'complete'

            def verify(self):
                self.report['status'] = 'passed'

            def run(self, args, **kwargs):
                if body is not None:
                    if linked:
                        target = base / 'outside'; target.write_bytes(body)
                        (self.work / 'answer.md').symlink_to(target)
                    else:
                        (self.work / 'answer.md').write_bytes(body)
                rows = fake_events(cli, 'fixture-model')
                # The final response is deliberately shorter and different from the file.
                text = '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows).replace(ANSWER, REPLY)
                events, stderr = kwargs['log_paths']
                events.write_text(text + '\n'); stderr.write_text('')
                if cli == 'codex':
                    output = Path(args[args.index('--output-last-message') + 1])
                    self_outer.assertNotEqual(output, self.work / 'answer.md')
                    output.write_text(REPLY)
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(args, 1)
                if failure == 'interrupt':
                    raise KeyboardInterrupt
                return subprocess.CompletedProcess(args, 1 if failure == 'exit' else 0)

        self_outer = self
        with patch.object(native_runtime, 'CandidateSandbox', Sandbox), \
             patch.object(native_runtime.native_auth, 'stage_auth', return_value={'BENCH_FIXTURE_KEY': 'fixture'}), \
             patch.object(native_runtime, 'stage_tls', return_value={}):
            return native_runtime.capture(agent(cli, '/fixture/client'), '/fixture/client', 'write answer.md',
                'provided_materials_only', 1, *(base / n for n in
                ('events.jsonl', 'stderr.log', 'answer.md', 'execution.json', 'trace.jsonl')),
                require_answer_file=required)

    def test_all_clients_preserve_file_bytes_separately_from_closing_reply(self):
        for cli in cli_adapters.CLIS:
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp); result = self.capture(base, cli)
                self.assertEqual(result['status'], 'completed', result)
                self.assertEqual((base / 'answer.md').read_bytes(), BODY)
                self.assertEqual((base / 'final-response.md').read_text(), REPLY)
                evidence = json.loads((base / 'execution.json').read_text())
                self.assertEqual(result['answer_capture'], evidence['answer_capture'])
                self.assertEqual(result['answer_capture']['source'], 'workspace_file')
                self.assertEqual(result['answer_capture']['answer_sha256'], hashlib.sha256(BODY).hexdigest())
                parsed = native_runtime.parse_capture(cli, base / 'events.jsonl', base / 'answer.md', evidence)
                self.assertEqual(parsed['answer'], BODY.decode().replace('\r\n', '\n'))
                (base / 'final-response.md').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    native_runtime.parse_capture(cli, base / 'events.jsonl', base / 'answer.md', evidence)

    def test_missing_empty_or_invalid_utf8_file_is_not_a_successful_answer(self):
        for body in (None, b' \r\n', b'\xff'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp); result = self.capture(base, body=body)
                self.assertEqual(result['status'], 'invalid_output', result)
                self.assertEqual((base / 'final-response.md').read_text(), REPLY)
                if body is None:
                    self.assertFalse((base / 'answer.md').exists())
                else:
                    self.assertEqual((base / 'answer.md').read_bytes(), body)

    def test_timeout_interruption_and_nonzero_exit_retain_partial_without_success(self):
        for failure, status in [('timeout', 'timeout'), ('interrupt', 'interrupted'), ('exit', 'failed')]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp); result = self.capture(base, failure=failure)
                self.assertEqual(result['status'], status, result)
                self.assertEqual((base / 'answer.md').read_bytes(), BODY)
                self.assertEqual(result['isolation']['cleanup'], 'complete')

    def test_linked_file_is_rejected_without_copying_outside_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); result = self.capture(base, linked=True)
            self.assertNotEqual(result['status'], 'completed')
            self.assertFalse((base / 'answer.md').exists())

    def test_explicit_chat_only_mode_can_use_final_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); result = self.capture(base, body=None, required=False)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['answer_capture']['source'], 'final_response')
            self.assertEqual((base / 'answer.md').read_text(), REPLY)


if __name__ == '__main__':
    unittest.main()
