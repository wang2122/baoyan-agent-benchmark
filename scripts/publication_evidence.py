"""Publication provenance and privacy evidence; reads public runs only."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from datetime import datetime, timezone

from scripts.prepare_publication import ROOT, USER_HOME, clean, export_capture, inside, jsonl, local_home_matches, put, read, sha

PATTERNS = {
    'email': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    'mobile_phone': re.compile(r'(?<![A-Za-z0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![A-Za-z0-9])'),
    'user_home': USER_HOME,
    'jwt': re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b'),
    'credential_token': re.compile(r'\b(?:sk-|hf_|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{16,}\b'),
    'signed_url': re.compile(r'(?i)(?:[?&]|\\u0026)(?:token|access_token|api_key|key|signature|x-amz-signature|x-goog-signature)=(?!\[REDACTED)[^\s"&\\<>]+'),
}


def pattern_matches(text):
    return {kind: local_home_matches(text) if kind == 'user_home' else list(pattern.finditer(text))
            for kind, pattern in PATTERNS.items()}


def scan(folder):
    """Return locations/counts, never sensitive snippets."""
    findings = []
    totals = Counter()
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.suffix not in {'.json', '.jsonl', '.md', '.log', '.txt'}:
            continue
        if path.name in {'privacy-scan.json', 'privacy-review.json'}:
            continue
        text = path.read_text(encoding='utf-8')
        matches = pattern_matches(text)
        counts = {kind: len(hits) for kind, hits in matches.items() if hits}
        if counts:
            totals.update(counts)
            findings.append({'path': path.relative_to(folder).as_posix(), 'sha256': sha(path.read_bytes()),
                             'counts': counts, 'lines': {k: sorted({text.count('\n', 0, m.start()) + 1 for m in hits})
                                                       for k, hits in matches.items() if hits}})
    return {'scope': str(folder.name), 'counts': dict(totals), 'findings': findings,
            'status': 'findings_require_review' if findings else 'no_pattern_findings',
            'human_review_complete': False,
            'limitations': ['Pattern scan cannot prove anonymity, copyright clearance, or absence of every secret.']}


def usage_events(path, cli, previous=None):
    """Known telemetry only. Preserve missing; do not price tokens or infer helper usage."""
    empty = {'input_tokens': None, 'output_tokens': None, 'cost_usd': None}
    if path is None or not path.is_file():
        return empty, 'missing', None
    try:
        events = [json.loads(s) for s in path.read_text().splitlines() if s.strip()]
    except (ValueError, UnicodeError):
        return empty, 'unparseable_events', None
    raw = None; basis = 'missing'; cumulative = None
    if cli == 'codex':
        rows = [e.get('usage') for e in events if e.get('type') == 'turn.completed']
        if len(rows) == 1:
            raw, basis = rows[0], 'codex_turn_completed'
    elif cli in ('qwen', 'claude'):
        rows = [e.get('usage') for e in events if e.get('type') == 'result']
        if len(rows) == 1:
            raw, basis = rows[0], 'cli_result_usage'
    elif cli == 'antigravity':
        rows = [e.get('result', {}) for e in events if e.get('event') == 'result']
        if len(rows) == 1:
            terminal = rows[0]
            cumulative = terminal.get('usage')
            if terminal.get('num_turns') == 1:
                raw, basis = cumulative, 'antigravity_first_turn'
            elif previous is not None and cumulative:
                raw = {k: cumulative[k] - previous[k] if type(cumulative.get(k)) is int and
                       type(previous.get(k)) is int and cumulative[k] >= previous[k] else None
                       for k in ('input_tokens', 'output_tokens')}
                basis = 'antigravity_cumulative_delta'
            else:
                basis = 'cumulative_without_baseline_unknown'
    elif cli == 'dsh':
        steps = [e.get('usage') or {} for e in events if e.get('type') == 'status' and e.get('phase') == 'step_end']
        if steps:
            raw = {}
            for dest, key in [('input_tokens', 'inputTokens'), ('output_tokens', 'outputTokens')]:
                values = [s.get(key) for s in steps]
                raw[dest] = sum(values) if all(type(v) is int and v >= 0 for v in values) else None
            basis = 'dsh_step_end_sum_uncached_input'
    if isinstance(raw, dict):
        for k in ('input_tokens', 'output_tokens'):
            if type(raw.get(k)) is int and raw[k] >= 0:
                empty[k] = raw[k]
    return empty, basis, cumulative


def ledger(index, selection, out):
    allowed_agents = set(index['campaign_by_agent'])
    allowed_tasks = set(selection['task_ids'])
    queue = [Path(p).resolve() for p in index['campaign_by_agent'].values()]
    visited = set(); calls = {}; controller_failures = {}; gaps = []; lineage = []; references = 0

    def public_path(path):
        path = Path(path).resolve()
        if not path.is_relative_to(ROOT / 'runs'):
            raise ValueError('History path outside public runs')
        return path

    def add_owner(path):
        path = public_path(path)
        for p in [path, *path.parents]:
            if p == ROOT / 'runs':
                break
            if (p / 'parallel-batch.json').is_file() or (p / 'formal-batch.json').is_file():
                queue.append(p); break

    def capture(source, hashes, agent, task_id, status=None, origin=None):
        nonlocal references
        source = public_path(source)
        # Verify every read file against the immutable reference when available.
        def checked(name):
            path = inside(source, name)
            if not path.is_file():
                return None
            raw = path.read_bytes()
            if hashes is not None and hashes.get(name) != sha(raw):
                raise ValueError('Historical evidence changed: ' + str(path))
            return json.loads(raw)
        entries = []
        if (source / 'native-episode.json').is_file():
            execution = checked('native-episode.json')
            contract = execution['contract']
            recipe = contract.get('agent', {})
            if not recipe:
                recipe = next((x for x in contract.values() if isinstance(x, dict) and x.get('cli')), {})
            previous = None
            for stage in execution['attempts']:
                if not stage.get('finished_at'):
                    gaps.append({'agent': agent, 'task_id': task_id, 'reason': 'unfinished_stage_not_read'}); continue
                stage_id = stage['stage_id']
                details = checked('stages/' + stage_id + '/execution.json') or {}
                cli = details.get('cli', recipe.get('cli'))
                events = source / 'stages' / stage_id / 'events.jsonl'
                if events.is_file() and hashes is not None and hashes.get(events.relative_to(source).as_posix()) != sha(events.read_bytes()):
                    raise ValueError('Historical stage events changed')
                usage, basis, current = usage_events(events, cli, previous)
                previous = current
                entries.append((dict(stage, cli=cli, auth_mode=details.get('auth_mode'), model=details.get('requested_model'),
                                     usage=usage, usage_basis=basis), stage_id, events))
        elif (source / 'run.json').is_file():
            meta = checked('run.json')
            if meta.get('adapter') != agent or meta.get('task_id') != task_id:
                raise ValueError('Historical run identity mismatch')
            if not meta.get('started_at') or not meta.get('finished_at') or meta.get('status') in ('prepared','running'):
                return
            events = source / 'workspace/trajectory.jsonl'
            if events.is_file() and hashes is not None and hashes.get('workspace/trajectory.jsonl') != sha(events.read_bytes()):
                raise ValueError('Historical events changed')
            usage = meta.get('usage')
            basis = 'recorded_run_usage'
            if not usage or all(usage.get(k) is None for k in ('input_tokens', 'output_tokens')):
                usage, basis, _ = usage_events(events, meta.get('cli'))
            entries.append((dict(meta, usage=usage, usage_basis=basis), None, events))
        else:
            gaps.append({'agent': agent, 'task_id': task_id, 'reason': 'no_terminal_capture_metadata', 'origin': origin})
            return
        for meta, stage_id, events in entries:
            references += 1
            identity = {'agent': agent, 'task_id': task_id, 'stage_id': stage_id, 'started_at': meta.get('started_at'),
                        'session': meta.get('session_id', meta.get('native_session_id'))}
            if not identity['started_at']:
                gaps.append({'agent': agent, 'task_id': task_id, 'reason': 'missing_start_no_reliable_deduplication'});continue
            key = sha(json.dumps(identity, sort_keys=True).encode())[:24]
            alias = {'source': source.relative_to(ROOT).as_posix(), 'origin': origin}
            event_hash = sha(events.read_bytes()) if events.is_file() else None
            usage = meta.get('usage') or {}
            metrics = {k: usage.get(k) for k in ('input_tokens','output_tokens','cost_usd')}
            if key in calls:
                old = calls[key]
                if old['event_sha256'] != event_hash or old['usage'] != metrics:
                    raise ValueError('Conflicting duplicate invocation evidence: ' + key)
                if alias not in old['aliases']:
                    old['aliases'].append(alias)
                continue
            calls[key] = {'invocation_id': key, 'agent': agent, 'task_id': task_id, 'stage_id': stage_id,
                'cli': meta.get('cli'), 'model': meta.get('model'), 'auth_mode': meta.get('auth_mode'),
                'started_at': meta.get('started_at'), 'finished_at': meta.get('finished_at'),
                'status': meta.get('status', status), 'elapsed_seconds': meta.get('elapsed_seconds'),
                'usage': metrics, 'usage_basis': meta['usage_basis'], 'billed_cost_usd': None,
                'event_sha256': event_hash, 'aliases': [alias]}
            # Include historical public text evidence once per capture, not per alias or stage.
            evidence_id = sha(str(source).encode())[:24]
            dest = out / 'history' / evidence_id
            if not dest.exists():
                actual = hashes if hashes is not None else {p.relative_to(source).as_posix(): sha(p.read_bytes())
                    for p in source.rglob('*') if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts}
                export_capture(source, actual, dest)
            calls[key]['evidence'] = dest.relative_to(out).as_posix()

    while queue:
        directory = public_path(queue.pop(0))
        if directory in visited: continue
        visited.add(directory)
        parallel = directory / 'parallel-batch.json'
        if not parallel.is_file():
            # Original full-batch captures are reached through explicit historical lineage.
            plan_path = directory / 'answers/full-batch.json'
            if not plan_path.is_file():
                gaps.append({'source': directory.name, 'reason': 'missing_original_plan'}); continue
            plan = read(plan_path)
            lineage.append({'campaign': directory.name, 'kind': 'original_full_batch', 'manifest_sha256': sha(plan_path.read_bytes())})
            for job in plan['jobs']:
                if job['agent'] in allowed_agents and job['id'] in allowed_tasks:
                    src = inside(directory / 'answers', job['path'])
                    if src.exists(): capture(src, None, job['agent'], job['id'], origin=directory.name)
            continue
        batch = read(parallel); state = read(directory / 'parallel-state.json')
        lineage.append({'campaign': directory.name, 'kind': batch['schema_version'],
                        'manifest_sha256': sha(parallel.read_bytes()), 'policy': clean(batch.get('policy'))})
        for field in ('superseded_campaign','previous_recovery'):
            if batch.get(field): add_owner(batch[field]['path'])
        for prior in batch.get('prior_batches', []): add_owner(prior['path'])
        for job in batch['jobs']:
            agent, task = job['agent'], job['id']
            if agent not in allowed_agents or task not in allowed_tasks: continue
            row = state['jobs'][job['index']]
            for attempt in row['attempts']:
                if not attempt.get('finished_at') or not attempt.get('files'):
                    gaps.append({'agent': agent,'task_id': task,'reason': 'active_attempt_excluded', 'campaign': directory.name});continue
                source = directory / 'attempts' / str(job['index']) / ('a' + str(attempt['number']))
                # Stage root or a single CLI run nested under capture.
                prefix = 'capture/' if job['kind'] != 'single' else f'capture/{agent}/{task}/r{job["repeat"]}/'
                hashes = {n[len(prefix):]: h for n,h in attempt['files'].items() if n.startswith(prefix)}
                if hashes:
                    capture(source / prefix, hashes, agent, task, attempt.get('result',{}).get('status'), directory.name)
                else:
                    # A controller cancellation with no CLI metadata is not a proven model call.
                    key = sha(json.dumps([agent,task,attempt.get('started_at'),attempt.get('finished_at')]).encode())[:24]
                    if key not in controller_failures:
                        dest = out / 'controller-failures' / key
                        export_capture(source, attempt['files'], dest)
                        start = datetime.fromisoformat(attempt['started_at'])
                        end = datetime.fromisoformat(attempt['finished_at'])
                        controller_failures[key] = {'id':key,'agent':agent,'task_id':task,
                            'started_at':attempt['started_at'],'finished_at':attempt['finished_at'],
                            'elapsed_seconds':(end-start).total_seconds(),'result':clean(attempt.get('result')),
                            'model_call_evidence':False,'billed_cost_usd':None,
                            'evidence':dest.relative_to(out).as_posix(),'origins':[]}
                    controller_failures[key]['origins'].append({'campaign':directory.name,'attempt':attempt['number']})
            for field in ('reused_runs','account_recovery_attempts'):
                carried = batch.get(field,{}).get(str(job['index']))
                if not carried: continue
                source = public_path(carried['path']); add_owner(source)
                if field == 'reused_runs':
                    capture(source, carried['files'], agent, task, origin=directory.name + ':reused')
                else:
                    prefix = f'capture/{agent}/{task}/r{job["repeat"]}/'
                    capture(source / prefix, {n[len(prefix):]:h for n,h in carried['files'].items() if n.startswith(prefix)},
                            agent,task,origin=directory.name + ':account_recovery')
    rows = sorted(calls.values(), key=lambda r:(r['agent'],r['task_id'],r['started_at'],r['stage_id'] or ''))
    summaries=[]
    for agent in sorted(allowed_agents):
        records=[r for r in rows if r['agent']==agent]
        summary={'agent':agent,'unique_invocations':len(records),'status_counts':dict(Counter(r['status'] for r in records)),
                 'deduplicated_aliases':sum(len(r['aliases'])-1 for r in records), 'billed_cost_usd':None}
        for metric in ('input_tokens','output_tokens','cost_usd'):
            known=[r['usage'][metric] for r in records if r['usage'][metric] is not None]
            summary[metric]={'reported_sum':sum(known) if known else None,'known_invocations':len(known),
                             'unknown_invocations':len(records)-len(known)}
        durations=[r['elapsed_seconds'] for r in records if r['elapsed_seconds'] is not None]
        summary['invocation_seconds']={'reported_sum':sum(durations) if durations else None,
                                      'unknown_invocations':len(records)-len(durations)}
        summaries.append(summary)
    jsonl(out / 'invocations.jsonl', rows)
    jsonl(out / 'controller-failures.jsonl', list(controller_failures.values()))
    put(out / 'cost-summary.json', {'schema':'historical-cost-ledger.1','scope':'selected20_active_agents_reachable_lineage',
        'snapshot_finished_at': datetime.now(timezone.utc).isoformat(),
        'call_unit': 'Recorded native CLI launches, including failures before inference; each executed stage is a launch',
        'call_references':references,'unique_invocations':len(rows),'duplicate_references_removed':references-len(rows),
        'per_agent':summaries,'gaps':gaps,'lineage':lineage,'actual_billing_known':False,
        'controller_failures_without_cli_capture':len(controller_failures),
        'limitations':['Excludes unrelated pilots and agents outside the selected cohort.',
                      'Active calls are excluded; unknown values are not zero.',
                      'Token semantics differ by CLI; dsh input excludes separately reported cache reads.',
                      'Qwen helper search usage may be absent. Subscription bills cannot be inferred from token counts.',
                      'Summed invocation durations are not wall-clock study elapsed time.']})
    return summaries
