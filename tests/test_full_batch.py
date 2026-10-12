"""Synthetic executables and judges only; no provider calls or benchmark claims."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmark.core import read, write
from benchmark import full_batch, native_sessions
from benchmark.isolation import ResumedDirectory, supported
from benchmark.native_runtime import capture
from benchmark.leaderboard import export_leaderboard
from benchmark.judge import judge_batch
from benchmark.longitudinal import snapshot

ROOT = Path(__file__).resolve().parents[1]
SINGLE_TASKS = len(read(ROOT / 'data/dev/manifest.json')['tasks'])
PATHS = {'codex': '.codex/sessions/fixture.jsonl', 'claude': '.claude/projects/work/fixture.jsonl',
         'gemini': '.gemini/tmp/work/chats/session-fixture.json', 'qwen': '.qwen/projects/work/chats/fixture.jsonl',
         'kimi': '.kimi-code/sessions/work/fixture/agents/main/wire.jsonl',
         'dsh': '.dsh/sessions/work/fixture/3.jsonl'}
EVENTS = {
    'codex': [{'type': 'thread.started', 'thread_id': 'fixture-session'}, {'type': 'turn.completed', 'usage': {}}],
    'claude': [{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Fixture answer', 'session_id': 'fixture-session'}],
    'qwen': [{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Fixture answer', 'session_id': 'fixture-session'}],
    'gemini': [{'type': 'init', 'session_id': 'fixture-session'}, {'type': 'message', 'role': 'assistant', 'content': 'Fixture answer'},
               {'type': 'result', 'status': 'success'}],
    'kimi': [{'role': 'assistant', 'content': 'Fixture answer'}, {'type': 'session.resume_hint', 'session_id': 'fixture-session'}],
    'dsh': [{'type': 'session', 'sessionId': 'fixture-session'}, {'type': 'final', 'text': 'Fixture answer'},
            {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'completed'}}],
}


def agent(cli, binary):
    value = {'id': 'fixture-' + cli, 'cli': cli, 'binary': str(binary), 'cli_version': 'fixture',
             'model': 'fixture-model', 'auth': 'api', 'api_key_env': 'BENCH_FIXTURE_KEY',
             'base_url': 'https://api.example.com/v1', 'reasoning_effort': 'high' if cli in ('codex', 'claude', 'dsh') else 'native_default'}
    if cli == 'codex':
        value['web_search'] = 'disabled'
    return value


def executable(path, cli, strict=False):
    # Synthetic shell, running inside the real sandbox; it never opens a socket.
    script = '#!/bin/sh\nset -eu\n'
    script += 'prompt=$(cat)\n' if cli in ('codex', 'claude', 'dsh') else 'prompt="$*"\n'
    script += f'persist="$HOME/{PATHS[cli]}"\n'
    script += '''mkdir -p artifacts
if [ -f "$persist" ]; then
 test "$(cat artifacts/cwd.txt)" = "$PWD"
'''
    script += f' case "$*" in *"{native_sessions.RESUME_FLAGS[cli]}"*"fixture-session"*) ;; *) exit 82;; esac\n'
    if strict:
        script += ' test "$(cat artifacts/action-record.md)" = first\n case "$prompt" in *SECOND_ONLY*) ;; *) exit 83;; esac\n'
    script += ' printf second > artifacts/action-record.md\nelse\n'
    if strict:
        script += ' case "$prompt" in *SECOND_ONLY*) exit 84;; esac\n'
    script += ''' printf first > artifacts/action-record.md
 printf '%s' "$PWD" > artifacts/cwd.txt
 mkdir -p "$(dirname "$persist")"
 printf '{}' > "$persist"
fi
printf private > "$HOME/unrelated-history"
printf 'Fixture answer\\r\\n' > answer.md
while [ "$#" -gt 0 ]; do
 if [ "$1" = "--output-last-message" ]; then shift; printf 'Fixture answer\\r\\n' > "$1"; fi
 shift
done
'''
    for event in EVENTS[cli]:
        script += "printf '%s\\n' '" + json.dumps(event) + "'\n"
    path.write_text(script); path.chmod(0o700)


class PlanTests(unittest.TestCase):
    def test_selection_cli_preview_and_invalid_scope(self):
        from benchmark.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'preview'
            path = ROOT / 'config/selections/current.json'
            argv = ['--root', str(ROOT), 'cli-batch', '--registry', str(ROOT / 'config/agents-cli.example.json'),
                    '--agent', 'deepseek-v4-1-flash',
                    '--selection', str(path), '--out', str(out)]
            with patch('benchmark.native_runtime.capture') as calls:
                value = main(argv)
                calls.assert_not_called()
            self.assertEqual((value['planned'], value['original_tasks'], value['derived_episodes']), (70, 70, 0))
            self.assertEqual(sum(2 if j['kind'] == 'staged' else 1 for j in value['jobs']), 83)
            self.assertFalse(out.exists())
            for extra in (['--task', 'N02-01'], ['--include-episodes'], ['--suite', 'public-smoke']):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    main(argv + extra)
            for field, replacement in [('task_count', 19), ('stage_count', 23), ('case_group_count', 19)]:
                invalid = read(path); invalid[field] = replacement
                with self.subTest(field=field), self.assertRaises(ValueError):
                    full_batch.validate_selection(ROOT, invalid)
            invalid = read(path); invalid['tasks'][0]['fingerprints']['rubric'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'rubric changed'):
                full_batch.validate_selection(ROOT, invalid)
            invalid = read(path); invalid['source_files_sha256']['data/dev/manifest.json'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'source changed'):
                full_batch.validate_selection(ROOT, invalid)

    def test_cli_preview_and_audit_only_changes(self):
        from benchmark.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'preview'
            argv = ['--root', str(ROOT), 'cli-batch', '--registry', str(ROOT / 'config/agents-cli.example.json'),
                    '--agent', 'deepseek-v4-1-flash', '--include-staged', '--include-episodes', '--out', str(out)]
            value = main(argv)
            self.assertEqual(value['planned'], SINGLE_TASKS + 15)
            self.assertFalse(out.exists())
            first = full_batch.make_plan(ROOT, ROOT / 'config/agents-cli.example.json', 'dev',
                                          ['deepseek-v4-1-flash'], None, None, True, True, None)
            with patch('benchmark.reproduction.engine_files', return_value={'operator-guide': 'changed'}):
                second = full_batch.make_plan(ROOT, ROOT / 'config/agents-cli.example.json', 'dev',
                                              ['deepseek-v4-1-flash'], None, None, True, True, None)
            self.assertNotEqual(first['plan_sha256'], second['plan_sha256'])
            self.assertEqual(full_batch.plan_identity(first), full_batch.plan_identity(second))

    def test_all_eight_models_have_session_adapters_and_full_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'never-created'
            value = full_batch.run(ROOT, ROOT / 'config/agents-cli.example.json', out,
                                   include_staged=True, include_episodes=True)
            self.assertEqual((value['original_tasks'], value['derived_episodes'], value['planned']), (SINGLE_TASKS + 13, 2, (SINGLE_TASKS + 15) * 8))
            self.assertEqual(len(value['agents']), 8)
            self.assertFalse(out.exists())
            self.assertEqual(set(native_sessions.RESUME_FLAGS), {'codex', 'claude', 'gemini', 'antigravity', 'qwen', 'kimi', 'dsh'})

    def test_staged_has_two_turns_without_extra_long_horizon_deliverables(self):
        value = snapshot(ROOT, 'staged:N01-02')
        self.assertEqual(len(value['sources']), 1)
        self.assertEqual(len(value['stages']), 2)
        self.assertNotIn('action-record.md', value['stages'][0]['prompt'])
        self.assertNotIn('标注过工程场景图像', value['stages'][0]['prompt'])
        self.assertIn('标注过工程场景图像', value['stages'][1]['prompt'])

    def test_existing_directory_is_never_adopted_or_deleted(self):
        with tempfile.TemporaryDirectory(prefix='baoyan-candidate-') as tmp:
            path = Path(tmp); marker = path / 'user-work'; marker.write_text('keep')
            with self.assertRaises(FileExistsError):
                ResumedDirectory(path.name)
            self.assertEqual(marker.read_text(), 'keep')
        for value in ('../outside', '/tmp/baoyan-candidate-12345678', 'baoyan-candidate-x'):
            with self.assertRaises(ValueError):
                ResumedDirectory(value)

    def test_checkpoint_allows_only_native_state_and_artifacts(self):
        for cli in PATHS:
            with self.subTest(cli=cli), tempfile.TemporaryDirectory(prefix='baoyan-candidate-') as tmp:
                root = Path(tmp); box = SimpleNamespace(root=root, home=root / 'home', work=root / 'workspace')
                path = box.home / PATHS[cli]; path.parent.mkdir(parents=True); path.write_text('{}')
                auth = box.home / native_sessions.HOME_ROOTS[cli].split('/')[0] / 'auth.json'
                auth.write_text('private-credential')
                (box.work / 'artifacts').mkdir(parents=True); (box.work / 'artifacts/a.txt').write_text('answer')
                dest = root / 'checkpoint.json'; native_sessions.save(box, cli, 'fixture', dest)
                value = native_sessions.load(dest)
                self.assertNotIn('private-credential', dest.read_text())
                self.assertNotIn('auth.json', str(value['files']))
                value['files']['native/' + PATHS[cli]]['sha256'] = 'tampered'; write(dest, value)
                with self.assertRaises(ValueError):
                    native_sessions.load(dest)


class QwenCheckpointTests(unittest.TestCase):
    def test_current_chat_and_subagent_paths_preserve_bytes_without_credentials(self):
        with tempfile.TemporaryDirectory(prefix='baoyan-candidate-') as tmp:
            root = Path(tmp)
            box = SimpleNamespace(root=root, home=root / 'home', work=root / 'workspace')
            box.work.mkdir()
            files = {
                '.qwen/projects/work/chats/session.jsonl': b'{"role":"user"}\n',
                '.qwen/projects/work/subagents/session/agent-one.jsonl': b'{"role":"assistant"}\n',
                '.qwen/projects/work/subagents/session/agent-one.meta.json': b'{"state":"done"}',
                '.qwen/tmp/work/tool-results/source.txt': b'evidence\r\n',
            }
            for name, data in files.items():
                target = box.home / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (box.home / '.qwen/settings.json').write_text('private-settings')
            (box.home / '.qwen/oauth_creds.json').write_text('private-credential')
            target = root / 'checkpoint.json'
            native_sessions.save(box, 'qwen', 'session', target)
            value = native_sessions.load(target)
            self.assertEqual(set(value['files']), {'native/' + name for name in files})
            import base64
            for name, data in files.items():
                self.assertEqual(base64.b64decode(value['files']['native/' + name]['base64']), data)


@unittest.skipUnless(supported(), 'macOS sandbox lifecycle')
class NativeLifecycleTests(unittest.TestCase):
    def test_every_cli_two_turns_same_identity_directory_and_files(self):
        for cli in PATHS:
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {'BENCH_FIXTURE_KEY': 'synthetic-credential'}):
                base = Path(tmp); binary = base / ('fake-' + cli); executable(binary, cli, strict=True)
                a = agent(cli, binary); previous = None
                for i, prompt in enumerate(('FIRST_ONLY', 'SECOND_ONLY')):
                    out = base / f's{i}'; out.mkdir()
                    meta = capture(a, str(binary), prompt, 'provided_materials_only', 5, out / 'events.jsonl', out / 'stderr.log',
                                   out / 'answer.md', out / 'execution.json', out / 'trace.jsonl',
                                   previous_checkpoint=previous, checkpoint=out / 'checkpoint.json')
                    self.assertEqual(meta['status'], 'completed', (cli, meta))
                    self.assertEqual(meta['session_id'], 'fixture-session')
                    self.assertTrue((out / 'answer.md').read_text().strip())
                    checkpoint = native_sessions.load(out / 'checkpoint.json')
                    self.assertNotIn('unrelated-history', str(checkpoint))
                    self.assertFalse((Path(tempfile.gettempdir()) / checkpoint['directory']).exists())
                    previous = out / 'checkpoint.json'


@unittest.skipUnless(supported(), 'macOS sandbox lifecycle')
class FullWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.binary = self.base / 'fake-codex'; executable(self.binary, 'codex')
        self.registry = self.base / 'registry.json'; self.a = agent('codex', self.binary)
        write(self.registry, {'schema': 'native-cli-batch.1', 'repeats': 1, 'timeout_seconds': 5, 'agents': [self.a]})
        self.batch = self.base / 'batch'; self.out = self.base / 'judge'
        self.config = read(ROOT / 'config/judge.example.json')
        self.config.update(provider='fixture', model='fixture', expected_response_model='fixture',
                           endpoint='https://judge.example.org/v1/chat/completions')
        self.config_path = self.base / 'judge-config.json'; write(self.config_path, self.config)
        for target, value in [('benchmark.episode_runner.check_continuation', lambda *a: {}),
            ('benchmark.native_sessions.check', lambda *a: {}),
            ('benchmark.cli_adapters.check_agent', lambda *a, **k: {'id': self.a['id'], 'issues': [], 'binary': str(self.binary)}),
            ('benchmark.cli_setup.credential_environment', lambda *a: nullcontext({self.a['id']: None}))]:
            p = patch(target, side_effect=value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'BENCH_FIXTURE_KEY': 'synthetic-credential', 'BENCHMARK_JUDGE_API_KEY': 'synthetic-review-key'})
        p.start(); self.addCleanup(p.stop)

    def execute(self, resume=False):
        return full_batch.run(ROOT, self.registry, self.batch, task_ids=['BY-A-APPLY', 'N01-02'],
                              execute=True, resume=resume, include_staged=True, include_episodes=True,
                              episode_ids=['LONG-C'])

    @staticmethod
    def judgment(config, messages, key):
        inputs = json.loads(messages[1]['content'])
        if 'chain' in inputs:
            return {'chain': [dict(r, status='fail', reason='Synthetic missing requirement') for r in inputs['chain']]}
        value = json.loads(inputs['judgment-template.json']); value['candidates_reviewed'] = True
        for row in value['nodes']:
            row.update(grade=0, reason='Synthetic omission')
        for row in value['coverage']:
            row.update(status='miss', reason='Synthetic omission')
        return value

    def test_selected_scope_execution_review_and_resume(self):
        selected = read(ROOT / 'config/selections/current.json')
        selected.update(id='fixture-two-task-selection', task_ids=['N02-01', 'N01-02'],
                        task_count=2, case_group_count=2, stage_count=3)
        selected['tasks'] = [t for t in selected['tasks'] if t['task_id'] in selected['task_ids']]
        path = self.base / 'selection.json'; write(path, selected)
        result = full_batch.run(ROOT, self.registry, self.batch, execute=True, selection_path=path)
        self.assertEqual((result['completed'], result['original_tasks']), (2, 2))
        with patch('benchmark.native_runtime.capture') as calls:
            full_batch.run(ROOT, self.registry, self.batch, execute=True, resume=True, selection_path=path)
            calls.assert_not_called()
        export_leaderboard(ROOT, [self.batch], self.base / 'unreviewed')
        raw = read(self.base / 'unreviewed/leaderboard.json')
        self.assertEqual(raw['expected_tasks'], 2)
        self.assertIsNone(raw['rows'][0]['mean_score'])
        self.assertEqual(raw['rows'][0]['missing_reviews'], 2)
        with patch('benchmark.judge.request_judgment', side_effect=self.judgment) as api:
            judge_batch(ROOT, self.batch, self.config_path, self.out, execute=True)
            self.assertEqual(api.call_count, 2)
        board = read(self.out / 'leaderboard/leaderboard.json')
        self.assertEqual((board['expected_tasks'], board['expected_episodes']), (2, 0))
        self.assertTrue(board['rows'][0]['complete'])
        self.assertEqual(board['rows'][0]['mean_score'], 0)
        selected['label'] = 'Changed experiment'; write(path, selected)
        with self.assertRaisesRegex(ValueError, 'plan changed'):
            full_batch.run(ROOT, self.registry, self.batch, execute=True, resume=True, selection_path=path)

    def test_run_review_panel_no_double_count_and_resume_no_calls(self):
        result = self.execute(); self.assertEqual((result['status'], result['planned']), ('completed', 3))
        with patch('benchmark.native_runtime.capture') as calls:
            self.assertEqual(self.execute(resume=True)['status'], 'completed'); calls.assert_not_called()
        with patch('benchmark.judge.request_judgment', side_effect=self.judgment) as api:
            judge_batch(ROOT, self.batch, self.config_path, self.out); api.assert_not_called()
            judged = judge_batch(ROOT, self.batch, self.config_path, self.out, execute=True, resume=True)
            self.assertEqual(judged['status'], 'judge_completed',
                [(str(p.relative_to(self.out)), read(p)) for p in self.out.rglob('*status.json')])
            self.assertEqual(api.call_count, 5)  # single + staged + 2 LONG-C sources + chain
            judge_batch(ROOT, self.batch, self.config_path, self.out, execute=True, resume=True)
            self.assertEqual(api.call_count, 5)
        data = read(self.out / 'leaderboard/leaderboard.json'); row = data['rows'][0]
        self.assertEqual(data['expected_tasks'], SINGLE_TASKS + 13)
        self.assertEqual(data['expected_episodes'], 2)
        self.assertEqual(row['completed_attempts'], 2)
        self.assertEqual(row['scored_attempts'], 2)
        self.assertIsNone(row['mean_score'])
        self.assertEqual(len(row['episodes']), 2)
        self.assertEqual(row['episodes'][-1]['episode_id'], 'LONG-C')
        self.assertEqual(row['episodes'][-1]['repeats'][0]['stages']['completed'], 3)
        self.assertFalse(data['official'])
        page = (self.out / 'leaderboard/index.html').read_text()
        self.assertNotIn('synthetic-review-key', page)
        self.assertNotIn(str(self.base), page)
        if shutil.which('node'):
            # Exercise both the original-task and derived-task rendering paths without a GUI.
            script = re.search(r'<script>([\s\S]*)</script>', page).group(1)
            runner = self.base / 'render.cjs'
            runner.write_text("const vm=require('node:vm');const ids={};"
                "const node=()=>({value:'',append(){},replaceChildren(){},addEventListener(){}});"
                "const document={getElementById:id=>ids[id]??(ids[id]=node()),createElement:node};"
                "vm.runInNewContext(" + json.dumps(script) + ",{document});")
            rendered = subprocess.run(['node', str(runner)], capture_output=True, text=True)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
        scorepath = self.out / 'staged/fixture-codex/N01-02/r1/score.json'
        value = read(scorepath); value['episode_score'] = 99; write(scorepath, value)
        with self.assertRaisesRegex(ValueError, 'score changed'):
            export_leaderboard(ROOT, [self.out], self.base / 'tampered-board')

    def test_failed_judge_is_not_retried_and_raw_scope_remains_visible(self):
        self.execute()
        export_leaderboard(ROOT, [self.batch], self.base / 'raw-board')
        raw = read(self.base / 'raw-board/leaderboard.json')['rows'][0]
        self.assertEqual(raw['missing_reviews'], 2)
        with patch('benchmark.judge.request_judgment', side_effect=ValueError('synthetic failure')) as api:
            value = judge_batch(ROOT, self.batch, self.config_path, self.out, execute=True)
            self.assertEqual(value['status'], 'judge_failed'); count = api.call_count
            judge_batch(ROOT, self.batch, self.config_path, self.out, execute=True, resume=True)
            self.assertEqual(api.call_count, count)

    def test_staged_only_counts_once_and_does_not_request_chain_judgment(self):
        value = full_batch.run(ROOT, self.registry, self.batch, task_ids=['N01-02'],
                               execute=True, include_staged=True)
        self.assertEqual(value['completed'], 1)
        self.assertFalse((self.batch / 'single').exists())
        with patch('benchmark.judge.request_judgment', side_effect=self.judgment) as api:
            result = judge_batch(ROOT, self.batch, self.config_path, self.out, execute=True)
            self.assertEqual(result['status'], 'judge_completed')
            self.assertEqual(api.call_count, 1)
        row = read(self.out / 'leaderboard/leaderboard.json')['rows'][0]
        self.assertEqual(row['scored_attempts'], 1)
        self.assertEqual(row['episodes'], [])
        plan = read(self.batch / 'full-batch.json'); plan['originals'] = []
        write(self.batch / 'full-batch.json', plan)
        with self.assertRaisesRegex(ValueError, 'plan changed'):
            full_batch.verify(ROOT, self.batch)


if __name__ == '__main__':
    unittest.main()
