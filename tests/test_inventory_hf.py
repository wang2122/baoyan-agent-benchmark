"""Non-additive accounting and public export approvals; synthetic approvals stay temporary."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from benchmark.core import candidate_prompt, digest, read, write
from benchmark.dataset import export_hf
from benchmark.inventory import inventory

ROOT = Path(__file__).resolve().parents[1]
SINGLE_TASKS = len(read(ROOT / 'data/dev/manifest.json')['tasks'])


class InventoryExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_inventory_counts_canonical_tasks_without_adding_derived_episodes(self):
        result = inventory(ROOT)
        self.assertEqual(result['collections']['dev']['canonical_tasks'], SINGLE_TASKS)
        self.assertEqual(result['canonical_dev_tasks'], SINGLE_TASKS + result['staged_dev']['canonical_tasks'])
        self.assertEqual(result['longitudinal']['additional_independent_samples'], 0)
        self.assertFalse(result['private_test_checked'])
        self.assertFalse(result['formal_results_enabled'])
        self.assertEqual(result['domain'], 'baoyan')

    def test_pending_public_export_fails_before_creating_output(self):
        dest = self.base / 'hf'
        if SINGLE_TASKS == 65:
            with self.assertRaisesRegex(ValueError, 'Historical local task pool differs'):
                export_hf(ROOT, dest, 'public')
            self.assertFalse(dest.exists())
            return
        with self.assertRaisesRegex(ValueError, 'requires --review'):
            export_hf(ROOT, dest, 'public')
        self.assertFalse(dest.exists())
        review = self.base / 'review.json'
        write(review, {'schema_version': 'public-release-review.1', 'reviewer': 'fixture',
                       'reviewed_at': '2026-10-09', 'tasks': {}})
        with self.assertRaisesRegex(ValueError, 'public catalog approval'):
            export_hf(ROOT, dest, 'public', review)
        self.assertFalse(dest.exists())

    def test_hf_review_is_bound_to_exact_export_and_preview_is_labelled(self):
        root = self.base / 'fixture'
        for folder in ('data/dev', 'docs', 'config'):
            shutil.copytree(ROOT / folder, root / folder)
        manifest = read(root / 'data/dev/manifest.json')
        # This synthetic approval fixture contains single-stage tasks only.
        write(root / 'config/selections/current.json', {'task_ids': manifest['tasks']})
        review = {'schema_version': 'public-release-review.1', 'reviewer': 'synthetic-fixture',
                  'reviewed_at': '2026-10-09', 'tasks': {}}
        for ident in manifest['tasks']:
            path = root / 'data/dev' / ident / 'task.json'
            task = read(path); task['review']['content_status'] = 'approved'
            task['difficulty'] = task['difficulty'] or 'hard'; write(path, task)
            manifest['fingerprints'][ident]['task'] = digest(task)
            review['tasks']['data/dev/' + ident] = {'fingerprints': copy.deepcopy(manifest['fingerprints'][ident]),
                'privacy_approved': True, 'redistribution_approved': True, 'reason': 'Synthetic fixture only'}
        write(root / 'data/dev/manifest.json', manifest)
        rp = self.base / 'review.json'; write(rp, review)
        export_hf(root, self.base / 'approved', 'public', rp)
        self.assertTrue(read(self.base / 'approved/manifest.json')['public_release_approved'])
        review['tasks'][next(iter(review['tasks']))]['fingerprints']['task'] = '0' * 64; write(rp, review)
        with self.assertRaisesRegex(ValueError, 'stale public approval'):
            export_hf(root, self.base / 'stale', 'public', rp)
        self.assertFalse((self.base / 'stale').exists())
        export_hf(ROOT, self.base / 'preview')
        meta = read(self.base / 'preview/manifest.json')
        self.assertEqual(meta['sample_count'], SINGLE_TASKS + 13)
        self.assertEqual(meta['staged_count'], 13)
        self.assertEqual(meta['derived_episode_count'], 2)
        self.assertEqual(len((self.base / 'preview/staged-dev.jsonl').read_text().splitlines()), 13)
        self.assertTrue((self.base / 'preview/episodes.json').is_file())
        self.assertFalse(meta['public_release_approved'])
        self.assertIn(f'实际导出 **{SINGLE_TASKS}', (self.base / 'preview/README.md').read_text())
        for line in (self.base / 'preview/dev.jsonl').read_text().splitlines():
            sample = json.loads(line)
            task = read(ROOT / 'data/dev' / sample['id'] / 'task.json')
            self.assertEqual(sample['input'], candidate_prompt(task))
            self.assertEqual(sample['context'], {'persona': {}, 'observation': {}})


if __name__ == '__main__':
    unittest.main()
