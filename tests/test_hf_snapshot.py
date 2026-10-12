"""Frozen HF publication checks; no models, credentials or remote writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_publication as pub


class HFSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.root = self.base / 'repo'
        pub.put(self.root / 'config/selections/current.json', {'task_ids': ['A', 'B']})
        self.patch = patch.object(pub, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        tasks = {'A': {'id': 'A', 'prompt': 'Public question'},
                 'B': {'id': 'B', 'stages': [{'instruction': 'First public stage'},
                                             {'instruction': 'Second public stage'}]}}
        for ident, task in tasks.items():
            pub.put(self.source / 'tasks' / ident / 'task.json', task)
            pub.put(self.source / 'tasks' / ident / 'rubric.json',
                    {'nodes': [{'id': 'N1', 'points': 100, 'full_credit': 'Public criterion'}]})
        for name in ('LICENSE', 'CITATION.cff', 'questions.md', 'questions-and-scoring.md', 'scoring.md'):
            (self.source / name).write_text('Public material\n')
        deps = {name: pub.sha((self.source / name).read_bytes())
                for name in ['scoring.md'] + [f'tasks/{ident}/{kind}.json'
                    for ident in tasks for kind in ('task', 'rubric')]}
        pub.put(self.source / 'selection-70.json', {'task_ids': ['A', 'B'], 'task_count': 2,
                                                  'stage_count': 3, 'source_files_sha256': deps})
        pub.put(self.source / 'manifest.json', {'task_count': 2, 'stage_count': 3})
        pub.jsonl(self.source / 'single-stage.jsonl', [{'task_id': 'A', 'stage_count': 1,
            'input': 'Public question', 'task_json': json.dumps(tasks['A'])}])
        pub.jsonl(self.source / 'staged.jsonl', [{'task_id': 'B', 'stage_count': 2,
            'stages_json': json.dumps(tasks['B']['stages']), 'task_json': json.dumps(tasks['B'])}])
        pub.jsonl(self.source / 'rubrics.jsonl', [{'task_id': ident,
            'rubric_json': json.dumps(pub.read(self.source / 'tasks' / ident / 'rubric.json'))} for ident in tasks])
        (self.source / 'README.md').write_text('---\nconfigs:\n' + ''.join(
            '  path: ' + n + '\n' for n in ('single-stage.jsonl', 'staged.jsonl', 'rubrics.jsonl'))
            + '---\n屿智同行 project. Thanks 保研岛.\n')
        (self.source / 'README.zh-CN.md').write_text('屿智同行项目，感谢保研岛。\n')
        for name in pub.HF_RETIRED_FILES:
            (self.source / name).write_text('Retired material\n')
        pub.checksums(self.source)

    def test_only_retired_files_are_omitted_and_material_bytes_stay_frozen(self):
        before = {p.relative_to(self.source).as_posix(): p.read_bytes()
                  for p in self.source.rglob('*') if p.is_file()}
        out = self.base / 'out'
        report = pub.dataset_snapshot(out, self.source)
        self.assertEqual((report['tasks'], report['stages']), (2, 3))
        after = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob('*') if p.is_file()}
        self.assertEqual(set(before) - set(after), pub.HF_RETIRED_FILES)
        for name in set(after) - {'checksums.json'}:
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(before, {p.relative_to(self.source).as_posix(): p.read_bytes()
                                 for p in self.source.rglob('*') if p.is_file()})

    def test_tampered_scoring_material_fails_before_output_is_created(self):
        (self.source / 'tasks/A/rubric.json').write_text('{"points": 99}')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            pub.dataset_snapshot(self.base / 'out', self.source)
        self.assertFalse((self.base / 'out').exists())

    def test_extra_task_directory_is_not_published(self):
        (self.source / 'tasks/PRIVATE').mkdir()
        with self.assertRaisesRegex(ValueError, 'extra or missing task'):
            pub.dataset_snapshot(self.base / 'out', self.source)
        self.assertFalse((self.base / 'out').exists())

    def test_a_valid_file_hash_cannot_hide_mismatched_embedded_task_text(self):
        row = json.loads((self.source / 'single-stage.jsonl').read_text())
        row['task_json'] = json.dumps({'prompt': 'Different question'})
        pub.jsonl(self.source / 'single-stage.jsonl', [row])
        pub.checksums(self.source)
        with self.assertRaisesRegex(ValueError, 'differs from canonical'):
            pub.dataset_snapshot(self.base / 'out', self.source)
        self.assertFalse((self.base / 'out').exists())

    def test_old_readme_references_cannot_restore_retired_files(self):
        (self.source / 'README.md').write_text((self.source / 'README.md').read_text()
                                             + '\nselection-75.json\n')
        pub.checksums(self.source)
        with self.assertRaisesRegex(ValueError, 'references retired files'):
            pub.dataset_snapshot(self.base / 'out', self.source)
        self.assertFalse((self.base / 'out').exists())


if __name__ == '__main__':
    unittest.main()
