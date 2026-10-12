"""Native JSONL must preserve Unicode separators in fetched text."""
import json
from pathlib import Path
import tempfile
import unittest

from benchmark.cli_adapters import parse_result
from benchmark.native_runtime import index_events


class JsonlUnicodeTests(unittest.TestCase):
    def test_dsh_web_result_keeps_unicode_separators(self):
        events = [
            {'type': 'session', 'sessionId': 'session'},
            {'type': 'tool_result', 'result': 'first\u2028second\u2029third\x85last'},
            {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}},
            {'type': 'final', 'text': 'Answer\u2028continued'},
        ]
        with tempfile.TemporaryDirectory() as temp:
            source, trace = Path(temp) / 'events.jsonl', Path(temp) / 'trace.jsonl'
            source.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events) + '\n')
            self.assertEqual(parse_result('dsh', source)['answer'], events[-1]['text'])
            self.assertEqual(index_events(source, trace, 'dsh'), 4)
            indexed = [json.loads(line) for line in trace.read_text().split('\n') if line]
            self.assertEqual([e['event'] for e in indexed], events)
            self.assertEqual([e['line'] for e in indexed], [1, 2, 3, 4])

    def test_malformed_json_still_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            source, trace = Path(temp) / 'events.jsonl', Path(temp) / 'trace.jsonl'
            source.write_text('{"type":"final","text":"broken\ntext"}\n')
            with self.assertRaises(json.JSONDecodeError):
                parse_result('dsh', source)
            self.assertEqual(index_events(source, trace, 'dsh'), 0)


if __name__ == '__main__':
    unittest.main()
