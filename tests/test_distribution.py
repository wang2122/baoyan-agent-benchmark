"""Public 1.0 distribution boundaries and versioned candidate compatibility."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from benchmark.archives import read_archive, write_archive
from benchmark.core import candidate, candidate_prompt, load_tasks, read
from benchmark.release import HF_FILES, PUBLIC_DOCS, SELECTION_FILES, build_release, public_name, verify_release
from benchmark.reproduction import VERSION

ROOT = Path(__file__).resolve().parents[1]
SINGLE_TASKS = len(read(ROOT / 'data/dev/manifest.json')['tasks'])
PRIVATE_KEYS = {'source_document_section', 'source_document_lines',
                'source_evidence', 'source_type', 'source_verification'}


class DistributionTests(unittest.TestCase):
    def test_extracted_release_runs_readme_selection_previews(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            package = base / 'release.zip'
            build_release(ROOT, package)
            manifest, files = read_archive(package)
            extracted = base / 'extracted'
            for name, data in files.items():
                target = extracted / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            for name, count in zip(SELECTION_FILES, (70,)):
                with self.subTest(selection=name):
                    relative = 'config/selections/' + name
                    self.assertEqual(files[relative], (ROOT / relative).read_bytes())
                    out = base / ('preview-' + str(count))
                    result = subprocess.run([sys.executable, '-B', '-m', 'benchmark',
                        'cli-batch', '--selection', relative, '--agent', 'gpt-6-1-sol',
                        '--out', str(out)], cwd=extracted, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    preview = json.loads(result.stdout)
                    self.assertEqual(preview['status'], 'preview_no_model_called')
                    self.assertEqual(preview['planned'], count)
                    self.assertFalse(out.exists())
                    incomplete = base / ('missing-' + str(count) + '.zip')
                    write_archive(incomplete, {k: v for k, v in files.items() if k != relative}, manifest)
                    with self.assertRaisesRegex(ValueError, 'Incomplete release workflow'):
                        verify_release(incomplete)
            self.assertEqual({n for n in files if n.startswith('config/selections/')},
                             {'config/selections/' + n for n in SELECTION_FILES})
            self.assertFalse(public_name('config/selections/credentials.local.json'))
            self.assertFalse(public_name('config/selections/retired-cohort.json'))

    def test_public_package_has_only_user_docs_and_no_source_records(self):
        with tempfile.TemporaryDirectory() as d:
            package = Path(d) / 'release.zip'
            build_release(ROOT, package)
            self.assertEqual(verify_release(package)['release_version'], '1.0.1')
            manifest, files = read_archive(package)
            self.assertEqual({n.removeprefix('docs/') for n in files if n.startswith('docs/')}, set(PUBLIC_DOCS) | {'huggingface/' + name for name in HF_FILES})
            self.assertFalse(any(n.startswith(('runs/', 'data/source-review/')) for n in files))
            def keys(value):
                if isinstance(value, dict):
                    for k, item in value.items():
                        yield k
                        yield from keys(item)
                elif isinstance(value, list):
                    for item in value: yield from keys(item)
            for name, data in files.items():
                if name.startswith('data/') and name.endswith('.json'):
                    self.assertFalse(PRIVATE_KEYS & set(keys(json.loads(data))), name)
            self.assertFalse(public_name('docs/internal-notes.md'))
            self.assertFalse(public_name('docs/engineering-fixes.md'))
            self.assertFalse(public_name('leaderboard/results.json'))
            self.assertIn('data/staged-dev/manifest.json', files)
            self.assertIn('data/longitudinal/episodes.json', files)
            self.assertEqual(manifest['public_dev_tasks'], SINGLE_TASKS + 13)
            self.assertEqual(manifest['derived_episodes'], 2)
            self.assertNotIn('benchmark/codex_runner.py', files)
            self.assertNotIn('config/reproduction/public-smoke-codex.json', files)
            self.assertNotIn('config/reproduction/public-smoke-codex-pair.json', files)
            self.assertNotIn('config/reproduction/codex.example.json', files)

    def test_versions_match_and_content_reviews_are_approved(self):
        config = read(ROOT / 'config/benchmark.json')
        self.assertEqual(VERSION, '1.0.1')
        self.assertEqual(config['version'], '1.0.2')
        self.assertEqual(config['reproduction_version'], VERSION)
        self.assertIn('version: "1.0.2"', (ROOT / 'CITATION.cff').read_text())
        self.assertEqual(read(ROOT / 'data/dev/manifest.json')['version'], config['version'])
        self.assertEqual(read(ROOT / 'schemas/reproduction.schema.json')['properties']['release_version']['const'], VERSION)
        rows = load_tasks(ROOT, 'dev')
        self.assertTrue(all(t['review']['content_status'] == 'approved' for t, _ in rows))
        for task, rubric in rows:
            self.assertEqual(task['task_version'], config['version'])
            self.assertEqual(rubric['scoring_revision'], config['version'])
            self.assertFalse(rubric['official'])

    def test_legacy_versions_preserve_natural_and_structured_input_formats(self):
        for task, _ in load_tasks(ROOT, 'dev'):
            legacy = copy.deepcopy(task)
            legacy['execution'].pop('prompt_version', None)
            prior = copy.deepcopy(task)
            prior['execution'].pop('prompt_version', None)
            prior['task_version'] = 'dev-v0.6' if not task['context']['persona'] else 'dev-v0.2'
            expected = candidate_prompt(prior).replace(prior['task_version'], task['task_version'])
            self.assertEqual(candidate_prompt(legacy), expected)
            previous_fields = set(candidate(prior))
            self.assertEqual(set(candidate(legacy)), previous_fields)


if __name__ == '__main__':
    unittest.main()
