"""Recovery scheduling, attempt retention and capture extraction without model calls."""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark import parallel_batch as parallel
from benchmark.core import digest, file_hash, read, write

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def campaign(self, entries):
        agents = [{'id': name, 'cli': group} for name, group in dict((e[0], e[1]) for e in entries).items()]
        jobs = [dict(index=i, agent=e[0], id='task-' + str(i), kind='single', status=e[2]) for i, e in enumerate(entries)]
        m = {'jobs': jobs, 'registry': {'agents': agents}, 'concurrency_by_product': parallel.LIMITS,
             'policy': {'retry_delay_seconds': 0}}
        write(self.base / parallel.MARKER, m)
        state = {'jobs': [{'index': j['index'], 'status': 'queued', 'attempts': [],
                          'retry_at': 0, 'selected': None} for j in jobs], 'blocked_groups': {}, 'running': False}
        write(self.base / 'parallel-state.json', state)
        return m

    def execute(self, m, outcomes, stop_on_start=False):
        running = Counter(); peak = Counter(); seen = []; outputs = {}
        by_agent = {a['id']: a['cli'] for a in m['registry']['agents']}
        base = self.base
        class Process:
            def __init__(self, args, **kwargs):
                index, number = int(args[-3]), int(args[-2])
                self.index, self.number = index, number
                self.agent = m['jobs'][index]['agent']; self.group = by_agent[self.agent]
                self.pid = 10000 + len(seen); self.returncode = None; self.polls = 0; self.stopped = False
                running[self.group] += 1; running[self.agent] += 1
                peak[self.group] = max(peak[self.group], running[self.group])
                if self.group == 'codex':
                    assert running[self.agent] <= 1
                seen.append((index, number))
                if stop_on_start: write(base / 'stop-request.json', {'reason': 'test'})

            def poll(self):
                if self.returncode is not None: return self.returncode
                self.polls += 1
                if self.polls < 3 and not self.stopped: return None
                category = 'interrupted' if self.stopped else outcomes.get((self.index, self.number), 'completed')
                result = {'category': category, 'status': {'transient': 'timeout', 'setup_blocked': 'failed',
                    'account_blocked': 'failed'}.get(category, category), 'exit_code': 0 if category == 'completed' else 1}
                target = parallel.attempt_path(base, m['jobs'][self.index], self.number)
                write(target / 'result.json', result)
                outputs[(self.index, self.number)] = (target / 'result.json').read_bytes()
                running[self.group] -= 1; running[self.agent] -= 1
                self.returncode = result['exit_code']; return self.returncode

            def send_signal(self, sig): self.stopped = True
            def wait(self):
                while self.poll() is None: pass
                return self.returncode

        with patch.object(parallel, 'verify', return_value=m), \
             patch.object(parallel.subprocess, 'Popen', Process), patch.object(parallel.time, 'sleep'):
            result = parallel.run(base, base / 'credentials.json')
        return result, seen, peak, outputs

    def test_caps_retry_limit_old_failure_and_resume_do_not_overwrite(self):
        entries = [('astra', 'codex', 'prepared'), ('astra', 'codex', 'prepared'),
                   ('sol', 'codex', 'prepared'), ('sol', 'codex', 'prepared'),
                   ('deepseek', 'dsh', 'prepared'), ('deepseek', 'dsh', 'prepared'),
                   ('deepseek', 'dsh', 'failed')]
        m = self.campaign(entries)
        result, seen, peak, outputs = self.execute(m, {(4, 1): 'transient', (4, 2): 'transient', (6, 1): 'transient'})
        self.assertEqual(peak['codex'], 2); self.assertEqual(peak['dsh'], 2)
        self.assertEqual(seen.count((4, 2)), 1)
        self.assertNotIn((6, 2), seen)  # Prior failed attempt already consumed the first try.
        self.assertEqual(result['statuses'], {'completed': 5, 'failed': 2})
        for key, body in outputs.items():
            self.assertEqual((parallel.attempt_path(self.base, m['jobs'][key[0]], key[1]) / 'result.json').read_bytes(), body)
        with patch.object(parallel, 'verify', return_value=m), \
             patch.object(parallel.subprocess, 'Popen', side_effect=AssertionError('No retry on resume')):
            self.assertEqual(parallel.run(self.base, self.base / 'credentials.json')['new_attempts'], 8)
        (parallel.attempt_path(self.base, m['jobs'][0], 1) / 'result.json').write_text('{}')
        with patch.object(parallel, 'verify', return_value=m), self.assertRaisesRegex(ValueError, 'attempt changed'):
            parallel.run(self.base, self.base / 'credentials.json')

    def test_account_block_pauses_only_its_group(self):
        m = self.campaign([('qwen', 'qwen', 'prepared')] * 4 + [('deepseek', 'dsh', 'prepared')] * 3)
        result, seen, _, _ = self.execute(m, {(0, 1): 'account_blocked', (1, 1): 'account_blocked'})
        self.assertNotIn((2, 1), seen); self.assertNotIn((3, 1), seen)
        self.assertIn('qwen', result['blocked_groups'])
        self.assertEqual(result['statuses']['completed'], 3)

    def test_fresh_subset_freezes_exact_four_model_scope_and_rejects_drift(self):
        agents = ['gpt-6-astra', 'gpt-6-1-sol', 'deepseek-v4-1-flash', 'qwen3-8-max-0902']
        selection = ROOT / 'config/selections/current.json'
        registry = read(ROOT / 'config/agents-cli.example.json')
        for agent in registry['agents']:
            agent['cli_version'] = agent.get('cli_version') or 'fixture-pinned-version'
        registry_path = self.base / 'registry.json'; write(registry_path, registry)
        out = self.base / 'subset'
        with patch.object(parallel.subprocess, 'Popen', side_effect=AssertionError('Preparation must not run clients')):
            result = parallel.prepare_subset(ROOT, registry_path, selection, agents, out)
        self.assertEqual(result['planned'], 280)
        self.assertEqual(result['new_attempts'], 0)
        m = parallel.verify(out, prior_files=True)
        parallel.validate_state(out, m, read(out / 'parallel-state.json'))
        self.assertEqual(m['prior_batches'], [])
        for aid in agents:
            jobs = [j for j in m['jobs'] if j['agent'] == aid]
            self.assertEqual([j['id'] for j in jobs], read(selection)['task_ids'])
            self.assertEqual(Counter(j['kind'] for j in jobs), {'single': 57, 'staged': 13})
            self.assertEqual(len({j['case_group_id'] for j in jobs}), 23)
        changed = read(out / 'selection.json'); changed['task_ids'].pop()
        write(out / 'selection.json', changed)
        with self.assertRaisesRegex(ValueError, 'Subset selection changed'):
            parallel.verify(out)
        bad = read(selection); bad['tasks'][0]['fingerprints']['task'] = '0' * 64
        write(self.base / 'bad-selection.json', bad)
        with self.assertRaisesRegex(ValueError, 'Selected task or rubric changed'):
            parallel.prepare_subset(ROOT, registry_path, self.base / 'bad-selection.json',
                                    agents, self.base / 'bad-output')
        self.assertFalse((self.base / 'bad-output').exists())

    def test_task_failure_is_preserved_without_retry_and_other_tasks_continue(self):
        m = self.campaign([('kimi', 'kimi', 'prepared')] * 5)
        result, seen, _, outputs = self.execute(m, {(0, 1): 'failed'})
        self.assertEqual(len(seen), 5)
        self.assertNotIn((0, 2), seen)
        self.assertEqual(result['blocked_groups'], {})
        self.assertEqual(result['statuses'], {'failed': 1, 'completed': 4})
        state = read(self.base / 'parallel-state.json')
        self.assertIsNone(state['jobs'][0]['selected'])
        self.assertEqual((self.base / 'attempts/0/a1/result.json').read_bytes(), outputs[(0, 1)])

    def test_setup_failure_still_pauses_dispatch_for_its_product(self):
        m = self.campaign([('kimi', 'kimi', 'prepared')] * 5)
        result, seen, _, _ = self.execute(m, {(0, 1): 'setup_blocked'})
        self.assertEqual(len(seen), 2)
        self.assertEqual(result['blocked_groups']['kimi']['reason'], 'setup_blocked')

    def test_subset_reuse_skips_verified_capture_and_preserves_interruption_budget(self):
        from benchmark import native
        from benchmark.formal_public import tree_hashes
        registry = read(ROOT / 'config/agents-cli.example.json')
        agent = next(a for a in registry['agents'] if a['id'] == 'gpt-6-astra')
        registry['agents'] = [agent]
        path = self.base / 'registry.json'; write(path, registry)
        selection = ROOT / 'config/selections/current.json'
        previous = self.base / 'previous'
        parallel.prepare_subset(ROOT, path, selection, [agent['id']], previous)
        state = read(previous / 'parallel-state.json')
        target = previous / 'attempts/1/a1'; target.mkdir(parents=True)
        result = {'status': 'interrupted', 'category': 'interrupted'}
        write(target / 'result.json', result)
        state['jobs'][1].update(status='interrupted', attempts=[{
            'number': 1, 'result': result, 'files': tree_hashes(target)}])
        write(previous / 'parallel-state.json', state)
        run = self.base / 'old-capture'
        meta = {'adapter': agent['id'], 'task_id': read(selection)['task_ids'][0], 'status': 'completed',
                'answer_capture': {'source': 'workspace_file'}, 'execution_config': agent}
        write(run / 'run.json', meta)
        (run / 'workspace').mkdir(); (run / 'workspace/answer.md').write_text('original answer')
        out = self.base / 'continued'
        with patch.object(native, 'inspect_run', return_value=meta) as inspect:
            parallel.prepare_subset(ROOT, path, selection, [agent['id']], out,
                                    reuse_runs=[run], superseded_campaign=previous)
        inspect.assert_called_once()
        m = parallel.verify(out, prior_files=True)
        state = read(out / 'parallel-state.json'); parallel.validate_state(out, m, state)
        self.assertEqual(state['jobs'][0]['status'], 'completed')
        self.assertEqual(state['jobs'][0]['attempts'], [])
        self.assertEqual(parallel.remaining_attempts(m['jobs'][1], state['jobs'][1]), 1)
        self.assertEqual(parallel.remaining_attempts(m['jobs'][2], state['jobs'][2]), 2)
        result, seen, _, _ = self.execute_at(out, m)
        self.assertNotIn((0, 1), seen)
        self.assertEqual(result['statuses'], {'completed': 70})
        (run / 'workspace/answer.md').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Reused capture changed'):
            parallel.verify(out, prior_files=True)

    def execute_at(self, directory, manifest):
        previous = self.base
        try:
            self.base = directory
            return self.execute(manifest, {})
        finally:
            self.base = previous

    def test_account_recovery_keeps_failure_and_does_not_reset_retry_budget(self):
        from benchmark import native
        registry = read(ROOT / 'config/agents-cli.example.json')
        agent = next(a for a in registry['agents'] if a['id'] == 'gpt-6-astra')
        registry['agents'] = [agent]
        registry_path = self.base / 'registry.json'; write(registry_path, registry)
        attempt = self.base / 'old-attempt'
        write(attempt / 'result.json', {'status': 'failed', 'category': 'account_blocked'})
        task_id = read(ROOT / 'config/selections/current.json')['task_ids'][0]
        run = attempt / 'capture' / agent['id'] / task_id / 'r1'
        meta = {'adapter': agent['id'], 'task_id': task_id, 'status': 'failed',
                'execution_config': agent}
        write(run / 'run.json', meta)
        out = self.base / 'account-recovery'
        with patch.object(native, 'inspect_run', return_value=meta) as inspect:
            parallel.prepare_subset(ROOT, registry_path, ROOT / 'config/selections/current.json',
                                    [agent['id']], out, account_recovery_attempts=[attempt])
        inspect.assert_called_once()
        m = parallel.verify(out, prior_files=True); state = read(out / 'parallel-state.json')
        parallel.validate_state(out, m, state)
        self.assertEqual(parallel.remaining_attempts(m['jobs'][0], state['jobs'][0]), 1)
        previous = self.base
        try:
            self.base = out
            result, seen, _, _ = self.execute(m, {(0, 1): 'transient'})
        finally:
            self.base = previous
        self.assertNotIn((0, 2), seen)
        self.assertEqual(result['statuses'], {'failed': 1, 'completed': 69})
        self.assertEqual(read(attempt / 'result.json')['category'], 'account_blocked')
        write(attempt / 'result.json', {'changed': True})
        with self.assertRaisesRegex(ValueError, 'Carried account failure changed'):
            parallel.verify(out, prior_files=True)

    def test_stop_preserves_inflight_and_does_not_launch_or_retry_more(self):
        m = self.campaign([('deepseek', 'dsh', 'prepared')] * 5)
        result, seen, _, _ = self.execute(m, {}, stop_on_start=True)
        self.assertEqual(len(seen), 2)
        self.assertEqual(result['statuses'], {'interrupted': 2, 'queued': 3})
        self.assertFalse(result['running'])

    def test_error_classification_does_not_retry_quota_or_missing_file(self):
        capture = self.base / 'capture'; capture.mkdir()
        log = capture / 'cli.stderr.log'
        for message, expected in [('HTTP 429 overloaded', 'transient'),
                ('HTTP 429 insufficient_quota', 'account_blocked'),
                ("You've reached your weekly usage limit", 'account_blocked'),
                ('HTTP 401 unauthorized', 'account_blocked'), ('', 'failed')]:
            log.write_text(message)
            self.assertEqual(parallel.classify({'status': 'failed'}, capture), expected)
        log.write_text('error: failed to run prompt: The provided authorization grant is invalid')
        self.assertEqual(parallel.classify({'status': 'failed'}, capture), 'account_blocked')
        log.write_text('')
        self.assertEqual(parallel.classify({'status': 'invalid_output'}, capture), 'failed')
        events = capture / 'trajectory.jsonl'
        events.write_text(json.dumps({'type': 'error', 'message': 'insufficient_quota'}))
        self.assertEqual(parallel.classify({'status': 'failed'}, capture), 'account_blocked')
        events.write_text(json.dumps({'type': 'assistant', 'message': 'insufficient_quota'}))
        self.assertEqual(parallel.classify({'status': 'failed'}, capture), 'failed')
        account_error = '[API Error: 400 Access denied, please make sure your account is in good standing.]'
        events.write_text(json.dumps({'type': 'result', 'is_error': True,
                                     'error': {'message': account_error}}))
        self.assertEqual(parallel.classify({'status': 'failed'}, capture), 'account_blocked')
        events.write_text(json.dumps({'type': 'assistant', 'message': account_error}))
        self.assertEqual(parallel.classify({'status': 'failed'}, capture), 'failed')
        events.write_text(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
            'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': 1791631800}}))
        self.assertEqual(parallel.classify({'status': 'timeout'}, capture), 'account_blocked')
        events.write_text(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}}))
        self.assertEqual(parallel.classify({'status': 'timeout'}, capture), 'transient')
        write(capture / 'execution.json', {'phase': 'boundary_probes', 'status': 'failed'})
        self.assertEqual(parallel.classify({'status': 'failed'}, capture), 'setup_blocked')

    def test_recovery_requires_full_successful_write_and_no_later_mutation(self):
        path = self.base / 'trajectory.jsonl'
        body = '# 完整正文\n事实、证据与建议。\n'
        call = {'role': 'assistant', 'tool_calls': [{'id': 'write-1', 'function': {'name': 'Write',
            'arguments': json.dumps({'path': 'answer.md', 'content': body})}}]}
        result = {'role': 'tool', 'tool_call_id': 'write-1', 'content': f'Wrote {len(body.encode())} bytes to answer.md'}
        def save(rows): path.write_text('\n'.join(json.dumps(r) for r in rows))
        save([call, result, {'role': 'assistant', 'content': 'Done.'}])
        actual, receipt = parallel.recover_written_answer('kimi', path)
        self.assertEqual(actual, body); self.assertEqual(receipt['result_line'], 2)
        for rows in ([call], [call, dict(result, content='Permission denied')],
                     [call, dict(result, content='Wrote 999 bytes to answer.md')],
                     [call, result, {'role': 'assistant', 'tool_calls': [{'id': 'edit', 'function': {
                         'name': 'Shell', 'arguments': json.dumps({'command': 'change answer'})}}]}]):
            save(rows)
            with self.assertRaisesRegex(ValueError, 'No complete'):
                parallel.recover_written_answer('kimi', path)


class WorkerIntegrationTests(unittest.TestCase):
    def test_rebase_preserves_attempts_and_limits_explicit_infrastructure_exclusions(self):
        from tests.test_formal_public import FormalPublicTests
        from benchmark.formal_public import tree_hashes
        fixture = FormalPublicTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        previous, out = fixture.base / 'previous', fixture.base / 'repaired'
        parallel.prepare(fixture.root, [fixture.batch], previous)
        m, state = read(previous / parallel.MARKER), read(previous / 'parallel-state.json')
        for index, category in enumerate(('failed', 'interrupted', 'failed')):
            target = parallel.attempt_path(previous, m['jobs'][index], 1)
            capture = target / 'capture'; capture.mkdir(parents=True)
            (capture / 'cli.stderr.log').write_text('authorization grant is invalid' if index == 0 else '')
            (capture / 'trajectory.jsonl').write_text('{"type":"system.version"}\n')
            result = {'category': category, 'status': category}
            write(target / 'result.json', result)
            state['jobs'][index].update(status=category, attempts=[{
                'number': 1, 'result': result, 'files': tree_hashes(target)}])
        write(previous / 'parallel-state.json', state)
        write(previous / 'stop-request.json', {'reason': 'scheduler repair'})
        (previous / '.scheduler.lock').touch()  # A stopped scheduler already owns this lock file.
        before = tree_hashes(previous)
        parallel.rebase(ROOT, previous, out)
        self.assertEqual(tree_hashes(previous), before)
        updated, result = parallel.verify(out, prior_files=True), read(out / 'parallel-state.json')
        self.assertEqual([r['status'] for r in result['jobs'][:3]], ['queued', 'queued', 'failed'])
        for i in range(3):
            self.assertEqual(result['jobs'][i]['attempts'], state['jobs'][i]['attempts'])
        self.assertEqual(parallel.remaining_attempts(updated['jobs'][0], result['jobs'][0]), 2)
        result['jobs'][2]['budget_exclusions'] = {'1': 'invented'}
        with self.assertRaisesRegex(ValueError, 'exclusion changed'):
            parallel.validate_state(out, updated, result)

    def test_prepare_preserves_frozen_formal_source_and_captures(self):
        from tests.test_formal_public import FormalPublicTests
        from benchmark.formal_public import tree_hashes
        fixture = FormalPublicTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        before = tree_hashes(fixture.batch)
        out = fixture.base / 'recovery'
        result = parallel.prepare(fixture.root, [fixture.batch], out)
        self.assertEqual(result['planned'], 83)
        self.assertEqual(result['statuses'], {'queued': 83})
        self.assertFalse(result['official'])
        self.assertEqual(tree_hashes(fixture.batch), before)
        parallel.verify(out, prior_files=True)

    def test_episode_worker_keeps_turns_sequential_and_records_both_answers(self):
        from benchmark.isolation import supported
        if not supported(): self.skipTest('macOS Seatbelt')
        from tests.test_episode_runner import EpisodeTests
        from benchmark.formal_public import snapshot
        fixture = EpisodeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        directory = fixture.base / 'campaign'; directory.mkdir()
        snapshot(ROOT, directory / 'source')
        registry = read(fixture.registry)
        write(directory / 'agents.frozen.json', registry)
        job = {'index': 0, 'agent': 'fixture', 'id': 'FIXTURE', 'selector': 'FIXTURE', 'kind': 'episode'}
        write(directory / parallel.MARKER, {'registry': registry, 'jobs': [job]})
        target = parallel.attempt_path(directory, job, 1); target.mkdir(parents=True)
        with patch.object(__import__('sys'), 'argv', ['worker', str(directory), '0', '1', str(fixture.registry)]):
            parallel.worker()
        result = read(target / 'result.json')
        self.assertEqual(result['category'], 'completed', result)
        self.assertEqual(len(result['answer_files']), 2)
        for name in result['answer_files']:
            self.assertEqual((directory / name).read_bytes(), b'Fixture answer\r\n')

    def test_frozen_worker_runs_native_answer_and_keeps_closing_reply_separate(self):
        from benchmark.isolation import supported
        if not supported(): self.skipTest('macOS Seatbelt')
        from benchmark.formal_public import snapshot
        from tests.test_native import NativeTests
        fixture = NativeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        agent = next(a for a in fixture.registry['agents'] if a['cli'] == 'qwen')
        fixture.fake(agent, final_answer='Done; read answer.md.')
        directory = fixture.base / 'campaign'; directory.mkdir()
        snapshot(ROOT, directory / 'source')
        registry = dict(fixture.registry, agents=[agent], repeats=1)
        write(directory / 'agents.frozen.json', registry)
        job = {'index': 0, 'agent': agent['id'], 'id': 'BY-A-PLAN', 'kind': 'single'}
        write(directory / parallel.MARKER, {'registry': registry, 'jobs': [job]})
        parallel.attempt_path(directory, job, 1).mkdir(parents=True)
        env = dict(os.environ, NATIVE_FIXTURE_KEY='fixture-only', PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        result = __import__('subprocess').run([__import__('sys').executable, '-B', '-c', parallel.WORKER,
            str(directory), '0', '1', str(fixture.registry_path)], cwd=directory / 'source', env=env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = read(parallel.attempt_path(directory, job, 1) / 'result.json')
        self.assertEqual(report['category'], 'completed', str(report) + result.stderr)
        answer = directory / report['answer_files'][0]
        self.assertNotEqual(answer.read_text(), 'Done; read answer.md.')
        self.assertEqual(answer.with_name('final-response.md').read_text(), 'Done; read answer.md.')


if __name__ == '__main__': unittest.main()
