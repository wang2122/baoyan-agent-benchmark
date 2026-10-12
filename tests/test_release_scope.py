"""Release approval is explicit and bound to contents, never inferred from ZIP creation."""
import json
from pathlib import Path
import tempfile
import unittest

from benchmark.archives import read_archive, write_archive
from benchmark.core import digest, encoded, write
from benchmark.release import LOCAL_TESTS, PUBLIC_COLLECTIONS, CATALOG_FILE, REVIEW_FILE, build_release, verify_release

ROOT = Path(__file__).resolve().parents[1]


class ReleaseScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_preview_omits_tests_whose_source_collections_are_not_distributed(self):
        package = self.base / 'preview.zip'
        result = build_release(ROOT, package)
        self.assertEqual(result['scope'], 'preview')
        self.assertFalse(verify_release(package)['public_release_approved'])
        manifest, files = read_archive(package)
        self.assertFalse(any('tests/' + name in files for name in LOCAL_TESTS))
        self.assertNotIn(REVIEW_FILE, files)
        self.assertIn('tests/test_new_user_workflow.py', files)
        self.assertEqual(manifest['distribution']['collections'], list(PUBLIC_COLLECTIONS) + ['data/longitudinal'])

    def test_public_requires_bound_release_authorization(self):
        package = self.base / 'public.zip'
        with self.assertRaisesRegex(ValueError, 'requires --review'):
            build_release(ROOT, package, 'public')
        review = self.base / 'review.json'
        write(review, {'schema_version': 'public-release-review.1', 'reviewer': 'synthetic-fixture',
                       'reviewed_at': '2026-10-09', 'tasks': {}})
        with self.assertRaisesRegex(ValueError, 'public catalog approval'):
            build_release(ROOT, package, 'public', review)
        self.assertFalse(package.exists())

    def test_rehashing_top_level_scope_does_not_create_public_approval(self):
        package = self.base / 'preview.zip'; build_release(ROOT, package)
        manifest, files = read_archive(package)
        manifest['scope'] = 'public'
        attack = self.base / 'forged.zip'; write_archive(attack, files, manifest)
        with self.assertRaisesRegex(ValueError, 'no bound review'):
            verify_release(attack)

    def test_synthetic_public_approval_is_bound_to_every_task_fingerprint(self):
        package = self.base / 'preview.zip'; build_release(ROOT, package)
        manifest, files = read_archive(package)
        review = {'schema_version': 'public-release-review.1', 'reviewer': 'synthetic-fixture',
                  'reviewed_at': '2026-10-09', 'tasks': {}}
        review['catalog_sha256'] = digest(json.loads(files[CATALOG_FILE]))
        # Synthetic approvals exist ONLY inside this temporary test archive.
        for folder in PUBLIC_COLLECTIONS:
            data = json.loads(files[folder + '/manifest.json'])
            for ident in data['tasks']:
                key = folder + '/' + ident
                review['tasks'][key] = {'fingerprints': dict(data['fingerprints'][ident]),
                    'privacy_approved': True, 'redistribution_approved': True, 'reason': 'Synthetic fixture only.'}
            files[folder + '/manifest.json'] = encoded(data)
        manifest['scope'] = 'public'; files[REVIEW_FILE] = encoded(review)
        approved = self.base / 'approved.zip'; write_archive(approved, files, manifest)
        self.assertTrue(verify_release(approved)['public_release_approved'])
        review['tasks'][next(iter(review['tasks']))]['fingerprints']['task'] = '0' * 64
        files[REVIEW_FILE] = encoded(review)
        stale = self.base / 'stale.zip'; write_archive(stale, files, manifest)
        with self.assertRaisesRegex(ValueError, 'stale public approval'):
            verify_release(stale)


    def test_episode_fingerprint_and_stage_order_cannot_be_rehashed_away(self):
        package = self.base / 'preview.zip'; build_release(ROOT, package)
        manifest, files = read_archive(package)
        catalog = json.loads(files[CATALOG_FILE])
        catalog['episodes'][0]['stages'][0]['task_sha256'] = '0' * 64
        files[CATALOG_FILE] = encoded(catalog)
        attack = self.base / 'changed-source.zip'; write_archive(attack, files, manifest)
        with self.assertRaisesRegex(ValueError, 'episode source changed'):
            verify_release(attack)

    def test_restored_staged_source_releases_only_current_prompt(self):
        from benchmark.longitudinal import prepare, snapshot
        from benchmark.task_sources import load_source
        snap = snapshot(ROOT, 'LONG-C')
        task, _, _ = load_source(ROOT, 'staged-dev', 'O03-04')
        self.assertEqual(len(snap['stages']), 3)
        out, candidate = self.base / 'organizer', self.base / 'candidate'
        prepare(ROOT, 'LONG-C', out, candidate, 'fixture', 'fixture', 'fixture')
        prompt = (candidate / 'S0-prompt.md').read_text()
        self.assertIn(task['stages'][0]['instruction'], prompt)
        self.assertNotIn(task['stages'][1]['instruction'], prompt)
        self.assertEqual([p.name for p in candidate.iterdir()], ['S0-prompt.md'])


if __name__ == '__main__':
    unittest.main()
