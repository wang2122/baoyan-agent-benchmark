"""Reviewable longitudinal source updates; no implicit re-pinning or content approval."""
from pathlib import Path
import re
import tempfile

from .core import digest, file_hash, read, require, write
from .longitudinal import catalog, snapshot
from .runtime_common import file_lock, now
from .task_sources import catalog_changes, load_source, proposed_catalog

SCHEMA = 'longitudinal-source-update.1'


def prepare_update(root, out, version):
    root, out = Path(root).resolve(), Path(out).resolve()
    require(isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', version),
            'Use a simple new catalog version name')
    current = catalog(root)
    require(version != current['version'], 'Source updates require a new catalog version')
    changes = catalog_changes(root, current)
    require(changes, 'No changed source fingerprints to review')
    proposal = proposed_catalog(root, current, version)
    for episode in proposal['episodes']:
        snapshot(root, episode['id'], proposal)
    require(not out.exists() and not out.is_symlink(), 'Update plan destination already exists')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-update-', dir=out.parent) as temp:
        stage = Path(temp) / 'plan'; stage.mkdir()
        write(stage / 'catalog.proposed.json', proposal)
        write(stage / 'source-changes.json', changes)
        files = {}
        for row in changes:
            collection, ident = row['source'].split('/')
            task, rubric, _ = load_source(root, collection, ident)
            for name, value in (('task', task), ('rubric', rubric)):
                relative = 'sources/' + row['source'] + '/' + name + '.json'
                write(stage / relative, value)
                files[relative] = file_hash(stage / relative)
        plan = {'schema_version': SCHEMA, 'base_catalog': current,
                'proposed_sha256': digest(proposal), 'changes_sha256': digest(changes), 'source_files': files}
        write(stage / 'plan.json', plan)
        write(stage / 'review-template.json', {'plan_sha256': digest(plan), 'reviewer': None,
            'sources': [{'source': row['source'], 'approved': False, 'reason': ''} for row in changes]})
        (stage / 'README.md').write_text(
            '本目录仅供组织者复审；不要发送给答题 Agent。\n\n'
            '逐项检查 source-changes.json 及 sources 内的新题面与评分材料，与原版本核对。'
            '确认阶段、学生、时间边界和原评分单位仍适配案例链后，填写 review-template.json，'
            '将真实 reviewer、逐来源 approved 和 reason 另存为 review.json。'
            '使用 longitudinal apply-update --plan-dir 本目录 --review review.json 应用。'
            '这只记录来源兼容性复审，不替代教师校准或公开隐私复审。\n', encoding='utf-8')
        require(not out.exists(), 'Update plan destination appeared during preparation')
        stage.rename(out)
    return {'status': 'review_required', 'plan_dir': str(out), 'changed_sources': len(changes),
            'affected_episodes': sorted({use['episode'] for row in changes for use in row['uses']}),
            'active_catalog_changed': False, 'official': False}


def apply_update(root, plan_dir, review_path):
    root, plan_dir = Path(root).resolve(), Path(plan_dir).resolve()
    target = root / 'data/longitudinal/episodes.json'
    with file_lock(target.with_suffix('.lock'), blocking=False,
                   busy_message='Another process is updating the longitudinal catalog'):
        require(not target.is_symlink(), 'Refusing a linked active catalog')
        plan = read(plan_dir / 'plan.json')
        require(plan.get('schema_version') == SCHEMA, 'Unsupported source update plan')
        current = catalog(root)
        require(plan.get('base_catalog') == current, 'Active catalog changed; prepare a new update plan')
        proposal = read(plan_dir / 'catalog.proposed.json')
        changes = read(plan_dir / 'source-changes.json')
        require(isinstance(proposal.get('version'), str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', proposal['version']),
                'Use a simple new catalog version name')
        require(digest(proposal) == plan.get('proposed_sha256') and digest(changes) == plan.get('changes_sha256'),
                'Source update proposal changed')
        require(changes == catalog_changes(root, current), 'Sources changed after planning; prepare a new plan')
        require(proposal == proposed_catalog(root, current, proposal.get('version'))
                and proposal.get('version') != current['version'], 'Only reviewed source pins and version may change')
        expected_files = {'sources/' + row['source'] + '/' + name + '.json'
                          for row in changes for name in ('task', 'rubric')}
        require(set(plan.get('source_files', {})) == expected_files, 'Incomplete reviewed source snapshots')
        for relative, fingerprint in plan['source_files'].items():
            path = plan_dir / relative
            require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(plan_dir)
                    and file_hash(path) == fingerprint, 'Reviewed source snapshot changed')
            _, collection, ident, filename = relative.split('/')
            task, rubric, _ = load_source(root, collection, ident)
            require(read(path) == (task if filename == 'task.json' else rubric), 'Reviewed source differs from current data')
        review = read(review_path)
        require(review.get('plan_sha256') == digest(plan), 'Review belongs to another source update plan')
        require(isinstance(review.get('reviewer'), str) and review['reviewer'].strip(), 'Record the actual source reviewer')
        decisions = review.get('sources')
        require(isinstance(decisions, list) and len(decisions) == len(changes)
                and all(isinstance(r, dict) for r in decisions)
                and {r.get('source') for r in decisions} == {r['source'] for r in changes},
                'Review each changed source exactly once')
        require(all(r.get('approved') is True and isinstance(r.get('reason'), str) and r['reason'].strip() for r in decisions),
                'Every source needs explicit approval and a review reason; unresolved sources remain blocked')
        for episode in proposal['episodes']:
            snapshot(root, episode['id'], proposal)
        archive = target.parent / 'versions' / (digest(current) + '.json')
        require(not archive.parent.is_symlink(), 'Refusing a linked catalog archive')
        if archive.exists():
            require(not archive.is_symlink() and read(archive) == current, 'Archived catalog differs')
        else:
            write(archive, current)
        proposal['source_update_review'] = {'reviewer': review['reviewer'], 'reviewed_at': now(),
            'plan_sha256': digest(plan), 'decisions': decisions, 'previous_catalog_sha256': digest(current),
            'scope': 'source_compatibility_not_teacher_calibration_or_public_release_approval'}
        write(target, proposal)
    return {'status': 'updated_local_catalog', 'version': proposal['version'], 'previous_catalog': str(archive),
            'existing_runs_changed': False, 'official': False}
