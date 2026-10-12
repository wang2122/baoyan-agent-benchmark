"""Local preview leaderboard, derived only from replay-verified result batches."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from .core import digest, file_hash, read, require, write
from .reproduction import batch_context, batch_runs, suite_rows
from .runtime_common import file_lock
from .statistics import report_statistics

FILES = ('index.html', 'leaderboard.json')
MARKER = 'leaderboard-manifest.json'


def export_leaderboard(root, batches, out, update=False, expected_repeats=None, samples=2000, seed=0,
                       suite='dev'):
    from . import formal_public
    formal = [formal_public.is_formal(b) for b in batches]
    if any(formal):
        require(all(formal), 'Export formal registrations and preview batches separately')
        return formal_public.export(batches, out, update, samples, seed, expected_repeats)
    from .full_batch import is_full
    from .full_judge import is_judged
    full = [is_full(b) or is_judged(b) for b in batches]
    if any(full):
        from .full_leaderboard import export
        require(all(full), 'Export legacy and full-scope batches separately')
        return export(root, batches, out, update, expected_repeats, samples, seed)
    out = Path(out).absolute()
    require(not out.is_symlink(), 'Leaderboard output cannot be linked')
    out.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(out.parent / ('.' + out.name + '.leaderboard.lock'), blocking=False,
                   busy_message='Leaderboard export already running'):
        return build_leaderboard(root, batches, out, update, expected_repeats, samples, seed, suite)


def build_leaderboard(root, batches, out, update, repeats, samples, seed, suite):
    contexts = [batch_context(root, batch)[0] for batch in batches]
    require(all(context['suite'] in ('dev', 'public-smoke') for context in contexts),
            'This preview exporter accepts public dev/smoke only; private test is not published')
    with tempfile.TemporaryDirectory(prefix='.leaderboard-', dir=out.parent) as temp:
        temp = Path(temp)
        if batches:
            report_statistics(root, batches, temp / 'statistics', expected_repeats=repeats, samples=samples, seed=seed)
            stats = read(temp / 'statistics/statistics.json')
            suite = stats['suite']
            rows = stats['rows']
            by_config = {row['configuration_id']: row for row in rows}
            for row in rows:
                row.update(pending_attempts=0, missing_reviews=0)
            for batch in batches:
                for run in batch_runs(batch)[1]:
                    meta = read(run / 'run.json')
                    key = [meta['adapter'], meta['adapter_version'], meta['model'], meta['timeout_seconds'],
                           meta.get('experiment_sha256')]
                    row = by_config[digest(key)[:16]]
                    if meta['status'] != 'completed':
                        continue
                    if not (run / 'score.json').exists():
                        row['missing_reviews'] += 1
                    elif read(run / 'score.json').get('score') is None:
                        row['pending_attempts'] += 1
            metadata = {name: stats[name] for name in ('context_sha256', 'protocol_sha256', 'aggregation',
                        'reviewer_profiles', 'bootstrap_samples', 'seed')}
            expected_tasks = len(contexts[0]['task_ids'])
        else:
            require(suite in ('dev', 'public-smoke'), 'Preview suite must be dev or public-smoke')
            expected_tasks = len(suite_rows(root, suite))
            rows, metadata = [], {'context_sha256': None, 'protocol_sha256': None,
                'aggregation': 'equal_task_weight_after_equal_repeat_weight', 'reviewer_profiles': [],
                'bootstrap_samples': samples, 'seed': seed}
        value = dict(metadata, schema_version='benchmark-leaderboard.1', official=False, suite=suite,
                     generated_at=datetime.now(timezone.utc).isoformat(), expected_tasks=expected_tasks, rows=rows,
                     verification_scope='saved_judgments_and_calculation_not_new_semantic_review',
                     note='开发预览；教师校准与正式私有测试尚未完成。缺失或待核不计零分，不给完整均分。')
        publish(value, out, update)
    complete = bool(rows) and all(row['complete'] for row in rows)
    return {'status': 'leaderboard_complete' if complete else 'leaderboard_preview',
            'exit_code': 0 if complete or not rows else 1, 'official': False,
            'page': str(out / 'index.html'), 'data': str(out / 'leaderboard.json'), 'configurations': len(rows)}


def publish(value, out, update=False):
    out = Path(out).absolute()
    require(not out.is_symlink(), 'Linked leaderboard destination')
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        require(update and out.is_dir(), 'Leaderboard exists; use --update for a managed export')
        require({p.name for p in out.iterdir()} == set(FILES) | {MARKER},
                'Output contains other files; preserve them and choose a new directory')
        require(all((out / name).is_file() and not (out / name).is_symlink() for name in (*FILES, MARKER)),
                'Missing or linked leaderboard files')
        require(read(out / MARKER) == {'schema_version': 'benchmark-leaderboard-export.1',
                'files': {name: file_hash(out / name) for name in FILES}},
                'Generated leaderboard changed; preserve edits and choose a new directory')
    with tempfile.TemporaryDirectory(prefix='.leaderboard-site-', dir=out.parent) as temp:
        stage = Path(temp) / 'site'
        stage.mkdir()
        write(stage / 'leaderboard.json', value)
        template = (Path(__file__).with_name('leaderboard.html')).read_text(encoding='utf-8')
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('&', '\\u0026')
        require(template.count('/* BENCHMARK_DATA */null') == 1, 'Leaderboard template data slot missing')
        (stage / 'index.html').write_text(template.replace('/* BENCHMARK_DATA */null', payload), encoding='utf-8')
        write(stage / MARKER, {'schema_version': 'benchmark-leaderboard-export.1',
                              'files': {name: file_hash(stage / name) for name in FILES}})
        if out.exists():
            for name in (*FILES, MARKER):
                os.replace(stage / name, out / name)
        else:
            stage.rename(out)
