"""Exercise the shipped CLI from a clean download; all answers/reviews are fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class DownloadWorkflowTests(unittest.TestCase):
    def test_download_capture_transfer_review_and_recheck(self):
        with tempfile.TemporaryDirectory(prefix='benchmark-download-') as temporary:
            base = Path(temporary)
            home = base / 'home'; home.mkdir()
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home),
                   'PYTHONNOUSERSITE': '1', 'LANG': 'en_US.UTF-8'}
            def run(cwd, *args, code=0, module='benchmark'):
                result = subprocess.run([sys.executable, '-m', module, *map(str, args)],
                                        cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return result
            package = base / 'release.zip'
            run(ROOT, 'release-package', '--out', package)
            repo = base / 'download'; repo.mkdir()
            with zipfile.ZipFile(package) as archive:
                archive.extractall(repo)
            run(repo, 'release-verify', '--package', package)
            listed = run(repo, 'list', module='benchmark.longitudinal')
            self.assertEqual(len(json.loads(listed.stdout)['episodes']), 2)
            preview = run(repo, 'episode-run', '--episode', 'LONG-C', '--agent', 'gpt-6-astra', '--out', base / 'episode-preview')
            self.assertFalse((base / 'episode-preview').exists())
            batch = base / 'app-demo'
            run(repo, 'reproduce', '--profile', 'config/reproduction/public-smoke-apps.json',
                '--baseline', 'gemini-app', '--out', batch)
            raw = b'Synthetic workflow fixture; not a model response.\r\n'
            answer = base / 'fixture.md'; answer.write_bytes(raw)
            for item in json.loads((batch / 'batch.json').read_text())['runs']:
                attempt = batch / item['path']
                session = json.loads((attempt / 'app-session.template.json').read_text())
                session.update(surface='desktop_app', app_version='synthetic-fixture',
                    selected_model_label='unverified', mode='fixture', reasoning_setting='unknown',
                    web_search='disabled', memory='unknown', new_conversation=True,
                    extra_user_messages=0, model_switch_observed=False,
                    started_at='2026-10-09T00:00:00Z', finished_at='2026-10-09T00:01:00Z', outcome='completed')
                receipt = base / 'fixture-session.json'; receipt.write_text(json.dumps(session))
                run(repo, 'record-answer', '--run-dir', attempt, '--answer', answer, '--session', receipt)
            answers = base / 'answers.zip'
            run(repo, 'submission-export', '--batch-dir', batch, '--out', answers)
            incoming = base / 'incoming'
            run(repo, 'submission-import', '--package', answers, '--out', incoming)
            for item in json.loads((incoming / 'batch.json').read_text())['runs']:
                self.assertEqual((incoming / item['path'] / 'workspace/answer.md').read_bytes(), raw)
            packets = base / 'packets'
            run(repo, 'review-batch', '--batch-dir', incoming, '--out', packets)
            for item in json.loads((packets / 'review-manifest.json').read_text())['packets']:
                packet = packets / item['packet_path']
                self.assertEqual((packet / 'answer.md').read_bytes(), raw.replace(b'\r\n', b'\n'))
                judgment_path = packet / 'judgment.json'
                judgment = json.loads((packet / 'judgment-template.json').read_text())
                judgment.update(reviewer='synthetic-workflow-fixture', judge_version='evidence-review-v1',
                                candidates_reviewed=True)
                for node in judgment['nodes']:
                    node.update(grade=0, reason='Synthetic fixture omits every requested requirement.')
                judgment_path.write_text(json.dumps(judgment))
                (packet / 'review-settings.json').write_text(json.dumps({
                    'reviewer': judgment['reviewer'], 'reviewer_type': 'human',
                    'judge_version': judgment['judge_version'], 'provider': None, 'model': None,
                    'model_version_evidence': None, 'reasoning_setting': None}))
            scored = base / 'scored'
            run(repo, 'review-apply', '--batch-dir', incoming, '--reviews', packets, '--out', scored)
            results = base / 'results.zip'
            run(repo, 'results-export', '--batch-dir', scored, '--out', results)
            replay = base / 'replay'
            run(repo, 'submission-import', '--package', results, '--out', replay)
            report = json.loads(run(repo, 'results-verify', '--batch-dir', replay).stdout)
            self.assertEqual(report['exit_code'], 0)
            self.assertEqual(report['summary']['rows'][0]['mean_score'], 0)
            self.assertFalse(report['summary']['rows'][0]['trajectory_requirements']['complete'])
            self.assertFalse(list(repo.rglob('*.credentials.json')))
            self.assertFalse(list(home.iterdir()))


if __name__ == '__main__':
    unittest.main()
