"""New contexts distinguish execution/evaluation compatibility from release provenance."""
import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from benchmark.core import digest, read, write
from benchmark.native import selection
from benchmark.reproduction import engine_files, make_context, reproduce, suite_rows, verify_context
from benchmark.workflow_versions import same_context

ROOT = Path(__file__).resolve().parents[1]


class WorkflowVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'release'; self.root.mkdir()
        for directory in ('benchmark', 'config', 'docs', 'schemas', 'scripts', 'examples'):
            shutil.copytree(ROOT / directory, self.root / directory, ignore=shutil.ignore_patterns('__pycache__'))
        self.rows = suite_rows(self.root, 'public-smoke')
        self.protocol = read(self.root / 'config/reproduction/materials.protocol.json')
        self.context = make_context(self.root, self.rows, self.protocol, 'public-smoke', source='fixed_profile')

    def test_operator_doc_and_unrelated_workflow_change_do_not_invalidate_answers(self):
        for name in ('docs/native-cli.md', 'docs/study.md', 'benchmark/longitudinal.py'):
            path = self.root / name; path.write_text(path.read_text() + '\n# Unrelated change\n')
        self.assertNotEqual(digest(engine_files(self.root)), self.context['engine_sha256'])
        verify_context(self.root, self.context)
        current = make_context(self.root, self.rows, self.protocol, 'public-smoke', source='fixed_profile')
        self.assertTrue(same_context(self.context, current))

    def test_runtime_or_evaluation_edits_still_require_matching_release(self):
        for name in ('benchmark/adapters.py', 'benchmark/scoring.py', 'benchmark/project_matching.py', 'docs/review-protocol.md'):
            path = self.root / name; original = path.read_bytes()
            path.write_bytes(original + b'\n# Changed behavior/policy\n')
            with self.assertRaisesRegex(ValueError, 'component changed'):
                verify_context(self.root, self.context)
            path.write_bytes(original)
        changed = copy.deepcopy(self.context); changed.pop('components')
        with self.assertRaisesRegex(ValueError, 'component identity'):
            verify_context(self.root, changed)

    def test_old_contexts_keep_full_fingerprint_policy(self):
        legacy = dict(self.context, schema_version='benchmark-reproduction.1'); legacy.pop('components')
        verify_context(self.root, legacy)
        path = self.root / 'docs/native-cli.md'; path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'Workflow code/config differs'):
            verify_context(self.root, legacy)

    def test_app_batch_can_resume_after_informational_doc_change(self):
        out = self.base / 'batch'
        args = (self.root, 'config/reproduction/public-smoke-apps.json', out, ['gemini-app'])
        reproduce(*args)
        frozen = (out / 'reproduction.json').read_bytes()
        path = self.root / 'docs/native-cli.md'; path.write_text(path.read_text() + '\nGuide clarification.\n')
        result = reproduce(*args, resume=True)
        self.assertEqual(result['manual_pending'], 2)
        self.assertEqual((out / 'reproduction.json').read_bytes(), frozen)

    def test_unselected_local_agents_are_not_selected_recipe_identity(self):
        registry = read(self.root / 'config/agents-cli.example.json')
        path = self.base / 'local.json'; write(path, registry)
        agent = 'deepseek-v4-1-flash'
        first = selection(self.root, path, 'public-smoke', [agent])[-1]
        registry['agents'][0]['binary'] = '/another/local/codex'
        registry['agents'][0]['model'] = 'another-model'
        write(path, registry)
        second = selection(self.root, path, 'public-smoke', [agent])[-1]
        self.assertNotEqual(first['registry_file_sha256'], second['registry_file_sha256'])
        self.assertTrue(same_context(first, second))
        next(a for a in registry['agents'] if a['id'] == agent)['model'] = 'different-selected-model'
        write(path, registry)
        third = selection(self.root, path, 'public-smoke', [agent])[-1]
        self.assertFalse(same_context(first, third))


if __name__ == '__main__':
    unittest.main()
