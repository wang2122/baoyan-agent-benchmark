"""Targeted publication boundary checks; no model calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publication', Path(__file__).resolve().parents[1] / 'scripts/prepare_publication.py')
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)


class PublicationBoundaryTests(unittest.TestCase):
    def test_export_jsonl_preserves_unicode_line_separators(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source = base / 'source'; source.mkdir()
            value = {'text': 'first\u2028second\u2029third'}
            path = source / 'events.jsonl'
            path.write_text(json.dumps(value, ensure_ascii=False) + '\n')
            pub.export_capture(source, {'events.jsonl': pub.sha(path.read_bytes())}, base / 'out')
            exported = (base / 'out/events.jsonl').read_text()
            self.assertEqual([json.loads(line) for line in exported.split('\n') if line], [value])

    def test_repository_contains_complete_test_suite_without_retired_ui(self):
        selected = pub.read(pub.ROOT / 'config/selections/current.json')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'public'
            pub.repository(out, selected, cohort_agents=set())
            manifest = pub.read(out / 'source-manifest.json')
            pub.checksums(out)
            checksums = pub.read(out / 'checksums.json')
            self.assertEqual(checksums['README.md'], pub.sha((out / 'README.md').read_bytes()))
            self.assertIn('source-manifest.json', checksums)
            for name in ('CONTRIBUTING.md', 'PUBLICATION-SCOPE.md',
                         'publication-source-manifest.json'):
                self.assertFalse((out / name).exists())
            names = {'benchmark/leaderboard.html'}
            names.update(p.relative_to(pub.ROOT).as_posix()
                         for p in (pub.ROOT / 'tests').glob('test_*.py'))
            self.assertIn('tests/test_new_user_workflow.py', names)
            for name in names:
                self.assertEqual((out / name).read_bytes(), (pub.ROOT / name).read_bytes())
                self.assertEqual(manifest['files'][name], pub.sha((out / name).read_bytes()))
            for name in ('benchmark/collector.html', 'benchmark/collector.py', 'tests/test_collector.py'):
                self.assertFalse((out / name).exists())
                self.assertNotIn(name, manifest['files'])

    def test_public_projection_omits_private_payload_and_rebinds_manifests(self):
        from scripts.publication_scope import project, rebind_selection
        from scripts.publication_reproduce import verify_source
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source = base / 'source'; folder = base / 'out'
            pub.put(source / 'data/dev/manifest.json', {'tasks':['KEEP','EXCLUDE'],
                'fingerprints':{'KEEP':{},'EXCLUDE':{}}})
            pub.put(source / 'data/staged-dev/manifest.json', {'tasks':[], 'fingerprints':{}})
            pub.put(source / 'data/dev/KEEP/task.json', {'prompt':'public question'})
            pub.put(source / 'data/dev/EXCLUDE/task.json', {'prompt':'PRIVATE_QUESTION'})
            hashes = {p.relative_to(source).as_posix():pub.sha(p.read_bytes()) for p in source.rglob('*') if p.is_file()}
            report = project(hashes, source, folder / 'engine', {'id':'public', 'task_ids':['KEEP']})
            pub.put(folder / 'source-manifest.json', report)
            verify_source(folder)
            self.assertFalse((folder / 'engine/data/dev/EXCLUDE').exists())
            self.assertEqual(pub.read(folder / 'engine/data/dev/manifest.json')['tasks'], ['KEEP'])
            self.assertIn('data/dev/manifest.json', report['changed'])
            rebound = rebind_selection({'source_files_sha256':{'data/dev/manifest.json':hashes['data/dev/manifest.json']}}, folder/'engine')
            self.assertNotEqual(rebound['source_files_sha256'], rebound['historical_source_files_sha256'])
            self.assertTrue((source / 'data/dev/EXCLUDE/task.json').exists())
            (folder / 'engine/data/dev/KEEP/task.json').write_text('tampered')
            with self.assertRaises(ValueError):
                verify_source(folder)

    def test_legacy_public_export_refuses_historical_extra_tasks(self):
        from benchmark.release import check_active_public_scope
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pub.put(root/'config/selections/current.json', {'task_ids':['PUBLIC']})
            pub.put(root/'data/dev/manifest.json', {'tasks':['PUBLIC','PRIVATE']})
            with self.assertRaises(ValueError):
                check_active_public_scope(root)
            pub.put(root/'data/dev/manifest.json', {'tasks':['PUBLIC']})
            check_active_public_scope(root)

    def test_hashes_are_not_mistaken_for_phone_numbers(self):
        value = 'a13812345678b' + 'f' * 51
        self.assertEqual(pub.clean_text(value), value)
        self.assertEqual(pub.clean_text('电话：13812345678'), '电话：[REDACTED_PHONE]')

    def test_redacted_url_is_not_reported_as_a_secret(self):
        from scripts.publication_evidence import PATTERNS
        cleaned = pub.clean_text('https://example.org/?access_token=secret-value&n=1')
        self.assertNotIn('secret-value', cleaned)
        self.assertIsNone(PATTERNS['signed_url'].search(cleaned))

    def test_public_home_urls_survive_while_local_paths_and_url_secrets_are_redacted(self):
        from scripts.publication_evidence import pattern_matches
        urls = ['https://www.law.ruc.edu.cn/home/t/?id=61546',
                'http://example.org/Users/public/page',
                'https://[2001:db8::1]/home/news/']
        value = ' '.join(urls) + ' cwd=/home/alice/work file:///Users/bob/work'
        cleaned = pub.clean_text(value)
        for url in urls:
            self.assertIn(url, cleaned)
        self.assertIn('cwd=/home/REDACTED/work', cleaned)
        self.assertIn('file:///Users/REDACTED/work', cleaned)
        self.assertEqual(len(pattern_matches(value)['user_home']), 2)
        self.assertFalse(pattern_matches(cleaned)['user_home'])
        signed = pub.clean_text(urls[0] + '&access_token=private-value')
        self.assertIn('/home/t/?id=61546', signed)
        self.assertNotIn('private-value', signed)

    def test_url_only_answer_export_keeps_original_hash_and_passes_privacy_scan(self):
        from scripts.publication_evidence import scan
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'; source.mkdir()
            body = b'[source](https://www.law.ruc.edu.cn/home/t/?id=61546)\n'
            (source / 'answer.md').write_bytes(body)
            out = Path(tmp) / 'public'
            artifacts = pub.export_capture(source, {'answer.md': pub.sha(body)}, out)
            self.assertEqual((out / 'answer.md').read_bytes(), body)
            self.assertFalse(artifacts[0]['transformed'])
            self.assertEqual(artifacts[0]['source_pattern_counts'], {})
            self.assertEqual(scan(out)['status'], 'no_pattern_findings')

    def test_cumulative_stage_usage_is_a_delta_not_double_counted(self):
        from scripts.publication_evidence import usage_events
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            path.write_text(json.dumps({'event':'result','result':{'num_turns':2,
                'usage':{'input_tokens':120,'output_tokens':30}}}) + '\n')
            value, basis, _ = usage_events(path, 'antigravity', {'input_tokens':80,'output_tokens':20})
            self.assertEqual(value['input_tokens'], 40)
            self.assertEqual(value['output_tokens'], 10)
            self.assertEqual(basis, 'antigravity_cumulative_delta')
            unknown, _, _ = usage_events(path, 'antigravity')
            self.assertIsNone(unknown['input_tokens'])

    def test_copied_attempts_have_one_cost_and_two_origins(self):
        from scripts import publication_evidence as ev
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            previous = root / 'runs/previous'
            current = root / 'runs/current'
            meta = {'adapter':'agent','task_id':'TASK','cli':'codex','status':'completed',
                'started_at':'2026-10-09T00:00:00Z','finished_at':'2026-10-09T00:00:01Z',
                'elapsed_seconds':1,'session_id':'same-invocation',
                'usage':{'input_tokens':10,'output_tokens':5,'cost_usd':None}}
            for directory in [previous,current]:
                prefix = 'capture/agent/TASK/r1/'
                run = directory / 'attempts/0/a1' / prefix
                pub.put(run / 'run.json', meta)
                (run / 'answer.md').write_text('synthetic answer')
                hashes={prefix + p.name:pub.sha(p.read_bytes()) for p in run.iterdir()}
                manifest={'schema_version':'test','jobs':[{'agent':'agent','id':'TASK','index':0,
                    'kind':'single','repeat':1}],'prior_batches':[]}
                if directory == current:
                    manifest['previous_recovery']={'path':str(previous)}
                pub.put(directory / 'parallel-batch.json',manifest)
                pub.put(directory / 'parallel-state.json',{'jobs':[{'attempts':[{'number':1,
                    'finished_at':meta['finished_at'],'files':hashes}]}]})
            with patch.object(ev,'ROOT',root):
                ev.ledger({'campaign_by_agent':{'agent':str(current)}},{'task_ids':['TASK']},root/'out')
            report=pub.read(root/'out/cost-summary.json')
            self.assertEqual(report['unique_invocations'],1)
            self.assertEqual(report['duplicate_references_removed'],1)
            self.assertEqual(report['per_agent'][0]['input_tokens']['reported_sum'],10)
            self.assertIsNone(report['per_agent'][0]['billed_cost_usd'])

    def test_full_source_verification_rejects_missing_or_added_files(self):
        from scripts.publication_reproduce import verify_source
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'engine').mkdir()
            (root/'engine/code.py').write_text('pass\n')
            hashes={'code.py':pub.sha((root/'engine/code.py').read_bytes())}
            pub.put(root/'source-manifest.json',{'original_files':hashes,'exported_files':hashes})
            verify_source(root)
            (root/'engine/extra.py').write_text('pass\n')
            with self.assertRaises(ValueError):
                verify_source(root)

    def test_secret_fields_and_headers_are_redacted_without_losing_usage(self):
        row = pub.clean({'authorization': 'Bearer credential', 'usage': {'input_tokens': 42},
                         'nested': [{'refresh_token': 'secret'}],
                         'text': '/Users/alice/run Bearer abcDEF123.api.key'})
        self.assertEqual(row['usage']['input_tokens'], 42)
        self.assertEqual(row['authorization'], '[REDACTED]')
        self.assertEqual(row['nested'][0]['refresh_token'], '[REDACTED]')
        self.assertNotIn('alice', row['text'])
        self.assertNotIn('abcDEF123', row['text'])

    def test_escape_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(ValueError):
                pub.inside(base, '../outside')
            with self.assertRaises(ValueError):
                pub.inside(base, '/outside')
            (base / 'link').symlink_to('/tmp')
            with self.assertRaises(ValueError):
                pub.inside(base, 'link/file')

    def test_changed_answer_fails_and_checkpoint_is_never_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source = base / 'source'; source.mkdir()
            (source / 'answer.md').write_text('real answer')
            (source / 'checkpoint.json').write_text('{"refresh_token":"private"}')
            hashes = {p.name: pub.sha(p.read_bytes()) for p in source.iterdir()}
            entries = pub.export_capture(source, hashes, base / 'out')
            self.assertEqual([x['path'] for x in entries], ['answer.md'])
            self.assertFalse((base / 'out/checkpoint.json').exists())
            (source / 'answer.md').write_text('changed')
            with self.assertRaises(ValueError):
                pub.export_capture(source, hashes, base / 'out2')

    def test_run_projection_cannot_publish_embedded_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); source = base / 'source'; source.mkdir()
            raw = json.dumps({'model': 'requested', 'records': [{'encoded_answer': 'private'}],
                              'session_id': 'native-session', 'usage': {'cost_usd': None}}).encode()
            (source / 'run.json').write_bytes(raw)
            pub.export_capture(source, {'run.json': pub.sha(raw)}, base / 'out')
            public = json.loads((base / 'out/run.json').read_text())
            self.assertNotIn('records', public)
            self.assertNotIn('session_id', public)
            self.assertIsNone(public['usage']['cost_usd'])


if __name__ == '__main__':
    unittest.main()
