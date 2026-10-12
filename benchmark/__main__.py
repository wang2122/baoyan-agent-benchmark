"""python3 -m benchmark --help"""
import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path
from .core import (SPLITS, collection, digest, file_hash, guard_private_output,
                   load_tasks, read, require, write)
from .packaging import task_package, judge_package
from .batch import batch_run, batch_status
from .runner import aggregate, execute, prepare
from .scoring import judgment_template, score
from .runtime_common import file_lock


def parser():
    p = argparse.ArgumentParser(description='Baoyan benchmark: public dev and private test')
    p.add_argument('--root', type=Path, default=Path.cwd(), help='Benchmark project root')
    p.add_argument('--private-root', type=Path, help='Private test collection root, outside public repository')
    sub = p.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('setup', help='Configure selected native CLIs locally; no inference')
    setup.add_argument('--registry', type=Path)
    setup.add_argument('--agent', action='append')
    setup.add_argument('--out', type=Path)
    setup.add_argument('--non-interactive', action='store_true')
    setup.add_argument('--import-credentials', type=Path,
                       help='Explicit local JSON/.env import of selected credential names; never sources shell code')
    cli_check = sub.add_parser('cli-check', help='Inspect pinned native CLI versions/flags; no inference')
    cli_check.add_argument('--registry', type=Path)
    cli_check.add_argument('--agent', action='append')
    episode = sub.add_parser('episode-run', help='Preview or execute a native same-session episode for any supported CLI')
    episode.add_argument('--registry', type=Path)
    episode.add_argument('--agent', required=True)
    episode.add_argument('--episode', required=True)
    episode.add_argument('--out', type=Path, required=True)
    episode.add_argument('--execute', action='store_true')
    episode.add_argument('--resume', action='store_true')
    episode.add_argument('--max-stages', type=int)
    for name in ('cli-batch', 'cli-package'):
        native = sub.add_parser(name, help='Native product trial: preview a batch or export answer-only packets')
        native.add_argument('--registry', type=Path)
        native.add_argument('--agent', action='append')
        native.add_argument('--suite', '--split', choices=('dev', 'test', 'public-smoke'), default='dev')
        native.add_argument('--task', action='append')
        native.add_argument('--protocol', type=Path)
        native.add_argument('--out', type=Path, required=True)
        native.add_argument('--include-staged', action='store_true', help='Include original two-stage tasks')
        native.add_argument('--include-episodes', action='store_true', help='Include derived longitudinal episodes separately')
        native.add_argument('--episode', action='append', help='Select derived episode ID (repeatable)')
        native.add_argument('--selection', type=Path,
                            help='Freeze a public task selection and use its tasks as the report denominator')
        if name == 'cli-batch':
            native.add_argument('--execute', action='store_true')
            native.add_argument('--resume', action='store_true')
    native = sub.add_parser('cli-import', help='Validate native returned evidence and prepare a standard review batch')
    native.add_argument('--package', type=Path, required=True)
    native.add_argument('--results', type=Path, required=True)
    native.add_argument('--out', type=Path, required=True)
    sub.add_parser('validate', help='Validate dataset integrity and split isolation')
    sub.add_parser('inventory', help='Count canonical tasks, stages, cases and derived episodes separately')
    export = sub.add_parser('export-hf', help='Generate public dev JSONL and dataset card; never reads private test or uploads')
    export.add_argument('--out', type=Path, required=True)
    export.add_argument('--scope', choices=('preview', 'public'), default='preview')
    export.add_argument('--review', type=Path)
    run = sub.add_parser('run', help='Prepare a task; optionally execute a user-specified command adapter')
    run.add_argument('--split', choices=SPLITS, required=True)
    run.add_argument('--task', required=True)
    run.add_argument('--out', type=Path, required=True)
    run.add_argument('--command', help='JSON argv array; no shell expansion; external sandbox is adapter responsibility')
    run.add_argument('--adapter')
    run.add_argument('--adapter-version')
    run.add_argument('--model')
    run.add_argument('--repeat', type=int, default=1)
    run.add_argument('--timeout', type=int, help='Wall-clock process limit, seconds; does not enforce token/cost limits')
    attach = sub.add_parser('record-answer', help='Import the original answer from a manually isolated Agent session')
    attach.add_argument('--run-dir', type=Path, required=True)
    attach.add_argument('--answer', type=Path)
    attach.add_argument('--session', type=Path, help='Filled official App session JSON; required for manual_app baselines')
    attach.add_argument('--adapter')
    attach.add_argument('--adapter-version')
    attach.add_argument('--model')
    attach.add_argument('--repeat', type=int, default=1)
    for name in ('judge-template', 'score'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--run-dir', type=Path, required=True)
        if name == 'score':
            cmd.add_argument('--judgment', type=Path, required=True)
    ag = sub.add_parser('aggregate', help='Produce internal diagnostics; incomplete coverage has no total mean')
    ag.add_argument('--split', choices=SPLITS, required=True)
    ag.add_argument('--run-dirs', nargs='+', type=Path, required=True)
    ag.add_argument('--out', type=Path, required=True, help='New output directory')
    pack = sub.add_parser('task-package', help='Create an organizer task package with separate candidate workspace/eval')
    pack.add_argument('--split', choices=SPLITS, required=True)
    pack.add_argument('--task', required=True)
    pack.add_argument('--out', type=Path, required=True)
    judge = sub.add_parser('judge-package', help='Prepare a role-separated packet for an LLM or human judge')
    judge.add_argument('--run-dir', type=Path, required=True)
    judge.add_argument('--out', type=Path, required=True)
    batch = sub.add_parser('batch', help='Prepare all baseline/task/repeat combinations; execution is opt-in')
    batch.add_argument('--split', choices=SPLITS, required=True)
    batch.add_argument('--registry', type=Path, required=True)
    batch.add_argument('--protocol', type=Path, help='Explicit study protocol; omitted uses the existing config/protocol.json')
    batch.add_argument('--out', type=Path, required=True)
    batch.add_argument('--execute', action='store_true')
    batch.add_argument('--resume', action='store_true', help='Continue prepared runs only; never retry attempted runs')
    batch.add_argument('--baseline', action='append', help='Select baseline ID (repeatable)')
    batch.add_argument('--task', action='append', help='Select task ID (repeatable)')
    status = sub.add_parser('status', help='Read batch progress without starting clients')
    status.add_argument('--batch-dir', type=Path, required=True)
    formal = sub.add_parser('formal-prepare', help='Freeze a project-official public evaluation; no model calls')
    formal.add_argument('--registry', type=Path)
    formal.add_argument('--agent', action='append')
    formal.add_argument('--out', type=Path, required=True)
    formal.add_argument('--source-root', type=Path, help='Matching release for explicitly reused captures')
    formal.add_argument('--reuse-batch', type=Path, help='Stopped full batch; reuse completed attempts and retain other attempt evidence')
    formal_run = sub.add_parser('formal-run', help='Run or resume a frozen formal public evaluation')
    formal_run.add_argument('--batch-dir', type=Path, required=True)
    formal_run.add_argument('--credentials-registry', type=Path)
    formal_run.add_argument('--execute', action='store_true')
    recovery = sub.add_parser('parallel-prepare', help='Freeze an explicit parallel recovery campaign; preserve all prior attempts')
    recovery.add_argument('--prior-batch', type=Path, action='append', required=True)
    recovery.add_argument('--out', type=Path, required=True)
    recovery = sub.add_parser('parallel-rebase', help='Preserve a stopped recovery while freezing a repaired scheduler')
    recovery.add_argument('--previous-recovery', type=Path, required=True)
    recovery.add_argument('--out', type=Path, required=True)
    recovery = sub.add_parser('parallel-run', help='Run queued recovery jobs with bounded concurrency and transient retries')
    recovery.add_argument('--batch-dir', type=Path, required=True)
    recovery.add_argument('--credentials-registry', type=Path, required=True)
    recovery.add_argument('--execute', action='store_true')
    recovery = sub.add_parser('parallel-status', help='Read recovery progress without starting clients')
    recovery.add_argument('--batch-dir', type=Path, required=True)
    recovery = sub.add_parser('parallel-audit', help='Record explicit completeness decisions for legacy captures')
    recovery.add_argument('--batch-dir', type=Path, required=True)
    recovery.add_argument('--decisions', type=Path, required=True)
    repro = sub.add_parser('reproduce', help='Use a fixed public workflow profile; model execution is opt-in')
    repro.add_argument('--profile', type=Path, required=True)
    repro.add_argument('--out', type=Path, required=True)
    repro.add_argument('--baseline', action='append')
    repro.add_argument('--model', help='Explicit model override for one CLI baseline; never falls back automatically')
    repro.add_argument('--execute', action='store_true')
    repro.add_argument('--resume', action='store_true')
    for name in ('submission-export', 'results-export'):
        transfer = sub.add_parser(name, help='Export a checksummed ZIP; no logs, environment or credentials')
        transfer.add_argument('--batch-dir', type=Path, required=True)
        transfer.add_argument('--out', type=Path, required=True)
    transfer = sub.add_parser('submission-import', help='Validate answer/result ZIP against this release; never executes package files')
    transfer.add_argument('--package', type=Path, required=True)
    transfer.add_argument('--out', type=Path, required=True)
    review = sub.add_parser('review-batch', help='Generate per-answer judge packets and a versioned reviewer manifest')
    review.add_argument('--batch-dir', type=Path, required=True)
    review.add_argument('--out', type=Path, required=True)
    review = sub.add_parser('review-apply', help='Validate all completed-answer reviews and score into a new batch')
    review.add_argument('--batch-dir', type=Path, required=True)
    review.add_argument('--reviews', type=Path, required=True)
    review.add_argument('--out', type=Path, required=True)
    judge = sub.add_parser('judge-batch', help='Fixed API judge, score replay and local leaderboard; calls require --execute')
    judge.add_argument('--batch-dir', type=Path, required=True)
    judge.add_argument('--config', type=Path, required=True)
    judge.add_argument('--out', type=Path, required=True)
    judge.add_argument('--execute', action='store_true')
    judge.add_argument('--resume', action='store_true', help='Continue unattempted packets only; preserve failures')
    board = sub.add_parser('leaderboard-export', help='Replay verified public results into a local HTML/JSON preview')
    board.add_argument('--batch-dir', action='append', type=Path, default=[])
    board.add_argument('--out', type=Path, required=True)
    board.add_argument('--update', action='store_true', help='Replace only an unchanged managed leaderboard export')
    board.add_argument('--suite', choices=('dev', 'public-smoke'), default='dev', help='Suite for an empty page')
    board.add_argument('--expected-repeats', type=int)
    board.add_argument('--bootstrap-samples', type=int, default=2000)
    board.add_argument('--seed', type=int, default=0)
    verify = sub.add_parser('results-verify', help='Recompute stored scores without calling a model or changing files')
    verify.add_argument('--batch-dir', type=Path, required=True)
    release = sub.add_parser('release-package', help='Build a local preview or reviewed public ZIP; never uploads')
    release.add_argument('--out', type=Path, required=True)
    release.add_argument('--scope', choices=('preview', 'public'), default='preview')
    release.add_argument('--review', type=Path, help='Explicit fingerprint-bound public release approval')
    release = sub.add_parser('release-verify', help='Check a release archive without executing its contents')
    release.add_argument('--package', type=Path, required=True)
    stats = sub.add_parser('statistics', help='Verified repeated-run means, student-group intervals and paired differences')
    stats.add_argument('--batch-dir', action='append', type=Path, required=True)
    stats.add_argument('--out', type=Path, required=True)
    stats.add_argument('--expected-repeats', type=int)
    stats.add_argument('--bootstrap-samples', type=int, default=2000)
    stats.add_argument('--seed', type=int, default=0)
    calibration = sub.add_parser('calibration-compare', help='Compare separate reviewer score sets for identical original answers')
    calibration.add_argument('--batch-dir', action='append', type=Path, required=True)
    calibration.add_argument('--out', type=Path, required=True)
    adjudicate = sub.add_parser('calibration-adjudicate', help='Save human dispute decisions without rewriting original scores')
    adjudicate.add_argument('--report-dir', type=Path, required=True)
    adjudicate.add_argument('--decisions', type=Path, required=True)
    adjudicate.add_argument('--out', type=Path, required=True)
    study = sub.add_parser('study-prepare', help='Create proposed study, registry and evidence templates; no model calls')
    study.add_argument('--out', type=Path, required=True)
    study.add_argument('--registry', type=Path, help='Optional native CLI registry; default retains existing baseline templates')
    for name in ('study-check', 'study-freeze'):
        study = sub.add_parser(name, help='Check operator evidence or save a new frozen protocol snapshot')
        study.add_argument('--protocol', type=Path, required=True)
        study.add_argument('--readiness', type=Path, required=name == 'study-freeze')
        if name == 'study-freeze':
            study.add_argument('--out', type=Path, required=True)
    return p


def answer_snapshot(directory):
    meta = read(directory / 'run.json')
    require(meta['status'] == 'completed', 'Only completed runs can be judged')
    task, rubric = read(directory / 'task.json'), read(directory / 'rubric.json')
    require(meta['task_sha256'] == digest(task) and meta['rubric_sha256'] == digest(rubric), 'Run snapshots changed')
    path = directory / 'workspace/answer.md'
    require(path.is_file() and not path.is_symlink(), 'Missing original answer')
    require(file_hash(path) == meta['answer_file_sha256'], 'Original answer was edited after capture')
    return task, rubric, path.read_text(encoding='utf-8')


def main(argv=None):
    args = parser().parse_args(argv)
    root = args.root.resolve()
    if args.action.startswith('parallel-'):
        from . import parallel_batch
        require(args.private_root is None, 'Recovery currently supports public batches only')
        if args.action == 'parallel-prepare':
            return parallel_batch.prepare(root, args.prior_batch, args.out)
        if args.action == 'parallel-rebase':
            return parallel_batch.rebase(root, args.previous_recovery, args.out)
        if args.action == 'parallel-audit':
            return parallel_batch.audit(args.batch_dir, args.decisions)
        if args.action == 'parallel-run' and args.execute:
            return parallel_batch.run(args.batch_dir, args.credentials_registry)
        return parallel_batch.status(args.batch_dir)
    if args.action in ('formal-prepare', 'formal-run'):
        from . import formal_public
        require(args.private_root is None, 'Formal public evaluations do not use private test data')
        default_registry = root / 'runs/config/agents.local.json'
        if args.action == 'formal-prepare':
            return formal_public.prepare(root, args.registry or default_registry, args.agent, args.out,
                                         args.source_root, args.reuse_batch)
        if not args.execute:
            return formal_public.status(args.batch_dir)
        return formal_public.run(args.batch_dir, args.credentials_registry or default_registry)
    if args.action == 'episode-run':
        from .episode_runner import run_episode
        require(args.private_root is None, 'episode-run currently uses the local dev pilot catalog; private episodes are not supported')
        saved = root / 'runs/config/agents.local.json'
        return run_episode(root, args.registry or (saved if saved.is_file() else root / 'config/agents-cli.example.json'),
                           args.episode, args.agent, args.out, execute=args.execute,
                           resume=args.resume, max_stages=args.max_stages)
    if args.action in ('setup', 'cli-check', 'cli-batch', 'cli-package'):
        saved = root / 'runs/config/agents.local.json'
        registry_path = args.registry or (saved if saved.is_file() else root / 'config/agents-cli.example.json')
        if args.action == 'setup':
            from .cli_setup import setup, interactive_terminal
            return setup(registry_path, args.out or root / 'runs/config/agents.local.json', args.agent,
                         interactive_terminal(args.non_interactive),
                         catalog_path=None if args.registry else root / 'config/agents-cli.example.json',
                         import_path=args.import_credentials)
        if args.action == 'cli-check':
            from .cli_adapters import select_agents
            from .cli_batch import inspect_agents
            from .cli_setup import credential_environment
            registry = read(registry_path)
            agents = select_agents(registry, args.agent)
            with credential_environment(dict(registry, agents=agents), registry_path) as sources:
                return inspect_agents(agents, check_network=True, credential_sources=sources)
        from .native import export_native, run_native
        if args.action == 'cli-package':
            if args.include_staged or args.include_episodes or args.selection:
                from .full_packet import export_full
                require(args.private_root is None, 'Full packets currently support public dev only')
                return export_full(root, registry_path, args.out, args.suite, args.agent, args.task,
                                   args.protocol, args.include_staged, args.include_episodes, args.episode,
                                   selection_path=args.selection)
            require(args.episode is None, '--episode requires --include-episodes')
            return export_native(root, registry_path, args.out, args.suite, args.agent, args.task,
                                 args.protocol, args.private_root)
        if args.include_staged or args.include_episodes or args.selection:
            from .full_batch import run
            require(args.private_root is None, 'Full batches currently support public dev only')
            return run(root, registry_path, args.out, args.suite, args.agent, args.task, args.protocol,
                       args.execute, args.resume, args.include_staged, args.include_episodes, args.episode,
                       selection_path=args.selection)
        require(args.episode is None, '--episode requires --include-episodes')
        return run_native(root, registry_path, args.out, args.suite, args.agent, args.task,
                          args.protocol, args.execute, args.resume, args.private_root)
    if args.action == 'cli-import':
        if (args.package / 'full-package.json').is_file():
            from .full_packet import import_full
            require(args.private_root is None, 'Full packets currently support public dev only')
            return import_full(root, args.package, args.results, args.out)
        from .native import import_native
        return import_native(root, args.package, args.results, args.out, args.private_root)
    if args.action == 'judge-batch':
        from . import formal_public
        if formal_public.is_formal(args.batch_dir):
            return formal_public.review(args.batch_dir, args.config, args.out, args.execute, args.resume)
        from .judge import judge_batch
        return judge_batch(root, args.batch_dir, args.config, args.out, args.execute, args.resume, args.private_root)
    if args.action == 'leaderboard-export':
        from .leaderboard import export_leaderboard
        return export_leaderboard(root, args.batch_dir, args.out, args.update, args.expected_repeats,
                                  args.bootstrap_samples, args.seed, args.suite)
    if args.action == 'statistics':
        from .statistics import report_statistics
        return report_statistics(root, args.batch_dir, args.out, args.private_root,
                                 args.expected_repeats, args.bootstrap_samples, args.seed)
    if args.action == 'calibration-compare':
        from .calibration import compare_reviews
        return compare_reviews(root, args.batch_dir, args.out, args.private_root)
    if args.action == 'calibration-adjudicate':
        from .calibration import record_adjudication
        return record_adjudication(root, args.report_dir, args.decisions, args.out)
    if args.action in ('study-prepare', 'study-check', 'study-freeze'):
        from .study import check_study, freeze_study, prepare_study
        if args.action == 'study-prepare':
            return prepare_study(root, args.out, args.registry)
        if args.action == 'study-check':
            return check_study(root, args.protocol, args.readiness)
        return freeze_study(root, args.protocol, args.readiness, args.out)
    if args.action == 'reproduce':
        from .reproduction import reproduce
        return reproduce(root, args.profile, args.out, args.baseline, args.model, args.execute, args.resume)
    if args.action in ('submission-export', 'results-export', 'submission-import',
                       'review-batch', 'review-apply', 'results-verify'):
        from .submissions import (apply_reviews, export_submission, import_submission,
                                  review_batch, verify_results)
        if args.action in ('submission-export', 'results-export'):
            return export_submission(root, args.batch_dir, args.out, args.private_root,
                                     args.action == 'results-export')
        if args.action == 'submission-import':
            return import_submission(root, args.package, args.out, args.private_root)
        if args.action == 'review-batch':
            return review_batch(root, args.batch_dir, args.out, args.private_root)
        if args.action == 'review-apply':
            return apply_reviews(root, args.batch_dir, args.reviews, args.out, args.private_root)
        return verify_results(root, args.batch_dir, args.private_root)
    if args.action in ('release-package', 'release-verify'):
        from .release import build_release, verify_release
        return build_release(root, args.out, args.scope, args.review) if args.action == 'release-package' else verify_release(args.package)
    if args.action == 'status':
        from . import formal_public
        if formal_public.is_formal(args.batch_dir):
            return formal_public.status(args.batch_dir)
        from .full_batch import is_full, report
        if is_full(args.batch_dir):
            return report(args.batch_dir, read(args.batch_dir / 'full-batch.json'))
        return batch_status(args.batch_dir)
    if args.action in ('run', 'batch', 'task-package', 'aggregate'):
        guard_private_output(args.split, args.out, root)
    if args.action == 'export-hf':
        from .dataset import export_hf
        return export_hf(root, args.out, args.scope, args.review)
    if args.action == 'inventory':
        from .inventory import inventory
        return inventory(root, args.private_root)
    if args.action == 'validate':
        from .task_sources import inspect_extra_collections
        groups = collection(root, args.private_root)
        counts = {split: {'tasks': len(rows), 'case_groups': len({t['case_group_id'] for t, _ in rows}),
                  'difficulty': {k: sum(t['difficulty'] == k for t, _ in rows) for k in ('easy', 'medium', 'hard')}}
                  for split, rows in groups.items()}
        extra = inspect_extra_collections(root)
        return {'status': 'source_review_required' if extra['exit_code'] else 'valid_structure', 'counts': counts,
                'task_count': sum(c['tasks'] for c in counts.values()),
                'extra_collections': extra, 'exit_code': extra['exit_code'],
                'private_test_checked': args.private_root is not None,
                'formal_results_enabled': read(root / 'config/protocol.json')['formal_results_enabled']}
    if args.action in ('task-package', 'batch'):
        rows = collection(root, args.private_root).get(args.split, [])
        if args.action == 'batch':
            from .adapters import select_baselines
            if args.task:
                require(set(args.task) <= {t['id'] for t, _ in rows}, 'Unknown selected task')
                require(len(args.task) == len(set(args.task)), 'Duplicate selected task')
                rows = [(t, r) for t, r in rows if t['id'] in args.task]
            registry = select_baselines(read(args.registry), args.baseline)
            # One writer per batch. OS lock releases on interruption; file is organizer-only.
            with file_lock(args.out.parent / ('.' + args.out.name + '.lock'), blocking=False,
                           busy_message='Another process is operating this batch'):
                protocol = read(args.protocol or root / 'config/protocol.json')
                require(isinstance(protocol, dict), 'Protocol must be a JSON object')
                if protocol.get('schema_version') == 'benchmark-study-protocol.1':
                    require(protocol['evaluation_split'] == args.split, 'Study protocol split differs from batch')
                    if protocol.get('baseline_registry_sha256') is not None:
                        require(file_hash(args.registry) == protocol['baseline_registry_sha256'], 'Study registry fingerprint differs')
                    if protocol.get('dataset_manifest_sha256') is not None:
                        manifest_path = (args.private_root if args.split == 'test' else root / 'data/dev') / 'manifest.json'
                        require(file_hash(manifest_path) == protocol['dataset_manifest_sha256'], 'Study dataset fingerprint differs')
                    from .study import run_study_batch
                    return run_study_batch(root, rows, protocol, registry, args.registry, args.out,
                                           args.execute, args.resume, args.private_root)
                return batch_run(rows, protocol, registry, args.out, args.execute, args.resume)
        selected = [(t, r) for t, r in rows if t['id'] == args.task]
        require(len(selected) == 1, 'Task not found in requested split')
        return task_package(*selected[0], args.out)
    if args.action == 'judge-package':
        snapshot = answer_snapshot(args.run_dir.resolve())
        guard_private_output(snapshot[0]['split'], args.out, root)
        return judge_package(*snapshot, args.out)
    if args.action == 'run':
        rows = collection(root, args.private_root)
        selected = [(t, r) for t, r in rows.get(args.split, []) if t['id'] == args.task]
        require(len(selected) == 1, 'Task not found in requested split')
        command = json.loads(args.command) if args.command else None
        if command is not None:
            require(all((args.adapter, args.adapter_version, args.model, args.timeout)), 'Command adapter metadata/timeout required')
        task, rubric = selected[0]
        out = prepare(task, rubric, args.out, read(root / 'config/protocol.json'))
        if command is not None:
            return execute(out, command, args.adapter, args.adapter_version, args.model, args.repeat, args.timeout)
        return {'status': 'prepared', 'workspace': str(out / 'workspace'), 'official': False,
                'note': 'Only give workspace to the tested Agent; evaluator snapshots stay with organizer.'}
    if args.action == 'record-answer':
        out = args.run_dir.resolve()
        meta = read(out / 'run.json')
        if meta.get('execution_mode') == 'manual_app':
            from .manual import record_app_answer
            require(not any((args.adapter, args.adapter_version, args.model)),
                    'Official App identity comes from the prepared registry; omit identity overrides')
            return record_app_answer(out, args.answer, args.session)
        require(all((args.answer, args.adapter, args.adapter_version, args.model)),
                'Manual command imports require --answer, --adapter, --adapter-version and --model')
        require(args.session is None, '--session is only supported for official App attempts')
        require(meta['status'] == 'prepared', 'Cannot overwrite an attempted run')
        require(args.answer.is_file() and args.answer.read_text().strip(), 'Missing/empty answer')
        require(args.repeat > 0 and all(s.strip() for s in (args.adapter, args.adapter_version, args.model)), 'Invalid identity')
        dest = out / 'workspace/answer.md'
        require(not dest.exists(), 'Answer already exists')
        shutil.copyfile(args.answer, dest)
        meta.update(status='completed', adapter=args.adapter, adapter_version=args.adapter_version, model=args.model,
                    repeat=args.repeat, timeout_seconds=None, elapsed_seconds=None, trajectory_status='missing',
                    answer_file_sha256=file_hash(dest), official=False)
        write(out / 'run.json', meta)
        return meta
    if args.action in ('judge-template', 'score'):
        out = args.run_dir.resolve()
        task, rubric, answer = answer_snapshot(out)
        if args.action == 'judge-template':
            path = out / 'judgment-template.json'
            require(not path.exists(), 'Judgment template already exists')
            value = judgment_template(task, rubric, answer)
            write(path, value)
            return {'template': str(path), 'rubric': str(out / 'rubric.json'), 'official': False}
        require(not (out / 'score.json').exists(), 'Score already exists; preserve old results and use a new review copy')
        judgment = read(args.judgment)
        result = score(task, rubric, answer, judgment)
        write(out / 'judgment.json', judgment)
        write(out / 'score.json', result)
        return result
    if args.action == 'aggregate':
        require(not args.out.exists(), 'Report output already exists')
        result = aggregate(load_tasks(root, args.split, args.private_root), args.run_dirs, read(root / 'config/protocol.json'))
        write(args.out / 'summary.json', result)
        lines = ['# 评测汇总（未经正式审核）', '', '|Agent|版本|模型|重复|已评分/总题数|均分|', '|---|---|---|---|---|---|']
        for row in result['rows']:
            cells = [row['adapter'], row['adapter_version'], row['model'], str(row['repeat']),
                     f"{row['scored_tasks']}/{row['expected_tasks']}", str(row['mean_score']) if row['mean_score'] is not None else '待核/覆盖不全']
            lines.append('|' + '|'.join(c.replace('|', '／').replace('\n', ' ') for c in cells) + '|')
        (args.out / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return result


if __name__ == '__main__':
    try:
        from .cli_common import result_exit_code
        result = main()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(result_exit_code(result))
    except (ValueError, KeyError, OSError, TypeError, zipfile.BadZipFile) as error:
        print('Error: ' + str(error), file=sys.stderr)
        sys.exit(2)
