#!/usr/bin/env python3
"""One contributor entry point; real calls require --execute, never pushes Git."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark.core import digest, file_hash, read, require, write
from benchmark import full_packet, full_packet_runner as runner
from benchmark.full_packet_io import extract_archive, validate_directory
from scripts.prepare_publication import clean, export_public_answers
from scripts.publication_evidence import scan

RECORDS = Path('runs/community/records')
CONFIGS = {
    'astra': ('gpt-6-astra', 'current'),
    'sol': ('gpt-6-1-sol', 'current'),
}


def export_record(root, package, archive, records_root):
    """Validate raw return, then prepare an answer-only, unreviewed local record."""
    root, package, archive, records_root = map(Path, (root, package, archive, records_root))
    require(archive.is_file(), 'Missing return ZIP')
    with tempfile.TemporaryDirectory(prefix='community-submit-') as tmp:
        temp = Path(tmp)
        full_packet.import_full(root, package, archive, temp / 'validated')
        source = temp / 'returned'; extract_archive(archive, source)
        transport = validate_directory(source)
        summary = read(source / 'summary.json')
        aid = summary['agent']; batch_id = digest(transport)[:20]
        destination = records_root / aid / (batch_id + '-answers')
        if destination.exists():
            checks = read(destination / 'checksums.json')['files']
            require(checks == {p.relative_to(destination).as_posix(): file_hash(p)
                              for p in destination.rglob('*') if p.is_file() and p.name != 'checksums.json'},
                    'Existing public record changed; preserve it for review')
            require(read(destination / 'record.json')['return_manifest_sha256'] == digest(transport),
                    'Existing submission has a different identity')
            return {'record': str(destination), 'raw_zip': str(archive), 'already_prepared': True}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.community-record-', dir=destination.parent) as staging:
            stage = Path(staging) / 'record'; stage.mkdir()
            # Drop all checkpoints, delivered arbitrary files, prompts and executable code.
            allowed = {'answer.md', 'final-response.md'}
            hashes = {name: info['sha256'] for name, info in transport['files'].items()
                      if name.startswith('runs/') and Path(name).name in allowed}
            artifacts = export_public_answers(source, hashes, stage / 'captures')
            write(stage / 'record.json', {
                'schema': 'community-execution-record.1', 'agent': aid, 'packet_id': summary['packet_id'],
                'return_manifest_sha256': digest(transport), 'raw_zip_sha256': file_hash(archive),
                'configuration': clean(read(source / 'registry.json')),
                'execution': clean(summary), 'review_status': 'submitted_unreviewed', 'official': False,
                'isolation_verified': False, 'import_verified': True, 'scored': False,
                'artifact_count': len(artifacts),
                'provenance': 'Answer-only redacted copies; full execution evidence and raw ZIP retained privately.',
            })
            write(stage / 'privacy-scan.json', scan(stage))
            require(read(stage / 'privacy-scan.json')['status'] == 'no_pattern_findings',
                    'Remaining privacy patterns; keep the original ZIP and ask the organizer to review')
            (stage / 'README.md').write_text(
                f'# {aid} answer submission\n\n'
                f'Completed {summary["completed"]}/{summary["expected"]} tasks. '
                'Integrity checked; organizer review and scoring pending.\n\n'
                '[Task states and recipe](record.json) · [Answers](captures/)\n\n'
                'Raw ZIP and execution trajectories are retained privately. '
                'Review this diff before publishing. A pattern scan is not a guarantee that all private content was removed.\n',
                encoding='utf-8')
            write(stage / 'checksums.json', {'files': {p.relative_to(stage).as_posix(): file_hash(p)
                                                     for p in sorted(stage.rglob('*')) if p.is_file()}})
            stage.rename(destination)
    return {'record': str(destination), 'completed': summary['completed'], 'expected': summary['expected'],
            'raw_zip': str(archive), 'review_status': 'submitted_unreviewed', 'pushed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', choices=CONFIGS)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--execute', action='store_true', help='Setup, check and use your subscription to run')
    action.add_argument('--submit', action='store_true', help='Prepare the unified Git execution record; no model calls or push')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-stages', type=int)
    args = parser.parse_args(argv)
    require(not args.resume or args.execute, '--resume requires --execute')
    require(args.max_stages is None or args.execute, '--max-stages requires --execute')
    aid, selection = CONFIGS[args.model]
    work = ROOT / 'runs/community-gpt' / args.model
    package = work / 'package'; answers = work / 'answers'
    if not package.exists():
        require(not args.submit, 'Run or preview this assignment before submitting')
        full_packet.export_full(ROOT, ROOT / 'config/agents-cli.example.json', package,
                                agent_ids=[aid], include_episodes=False,
                                selection_path=ROOT / f'config/selections/{selection}.json')
    candidate = package / 'candidates' / aid
    launcher = [sys.executable, str(candidate / 'run_agent.py')]
    if args.submit:
        require((answers / 'batch.json').is_file(), 'No execution records yet')
        manifest, registry = runner.load_packet(candidate)
        from benchmark.runtime_common import file_lock
        with file_lock(answers.parent / ('.' + answers.name + '.lock'), blocking=False,
                       busy_message='Answer batch is running'):
            returned = runner.report(answers, manifest, registry)
        result = export_record(ROOT, package, Path(returned['results_zip']), ROOT / RECORDS)
        write(work / 'submission-receipt.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print('Generated records stay in the ignored local runs directory; retain the raw ZIP for review.')
        return 0
    if args.execute:
        if not (candidate / '.local/registry.json').exists():
            subprocess.run(launcher + ['setup'], cwd=candidate, check=True)
        subprocess.run(launcher + ['check'], cwd=candidate, check=True)
    command = launcher + ['--out', str(answers)]
    if args.execute: command.append('--execute')
    if args.resume: command.append('--resume')
    if args.max_stages is not None: command += ['--max-stages', str(args.max_stages)]
    code = subprocess.run(command, cwd=candidate).returncode
    if args.execute:
        print(f'Preserve all results. Prepare your record with: python3 scripts/community_run.py {args.model} --submit')
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        sys.exit(2)
