#!/usr/bin/env python3
"""Apply owner-approved current70 metadata and sync the public HF projection.

The HF rubric retains projection-only metadata, while scoring nodes and reference
projects follow the selected GitHub source. Answers and scores are not edited.
"""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.core import digest
from scripts.prepare_publication import checksums, jsonl, put, read, sha

APPROVAL = ROOT / 'config/difficulty/current70-owner-approved.csv'
APPROVAL_DATE = '2026-10-12'


def approved_rows(selection):
    with APPROVAL.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    by_id = {row['task_id']: row for row in rows}
    if len(rows) != 70 or len(by_id) != 70 or set(by_id) != set(selection['task_ids']):
        raise ValueError('Difficulty approval must contain exactly the selected 70 unique tasks')
    for entry in selection['tasks']:
        row = by_id[entry['task_id']]
        if row['question_id'] != entry['question_id'] or row['task_path'] != entry['task_path']:
            raise ValueError('Difficulty approval source identity differs: ' + entry['task_id'])
        if row['suggested_difficulty'] not in ('easy', 'medium', 'hard'):
            raise ValueError('Invalid approved difficulty: ' + entry['task_id'])
        prior = None if row['old_difficulty'] in ('', 'unlabeled') else row['old_difficulty']
        if entry['difficulty'] not in (prior, row['suggested_difficulty']):
            raise ValueError('Unexpected starting difficulty: ' + entry['task_id'])
    counts = Counter(row['suggested_difficulty'] for row in rows)
    if counts != {'easy': 14, 'medium': 34, 'hard': 22}:
        raise ValueError('Approved difficulty distribution changed')
    return by_id, dict(counts)


def update_github(selection, by_id, counts):
    for entry in selection['tasks']:
        task_id = entry['task_id']
        path = ROOT / entry['task_path']
        rubric = read(ROOT / entry['rubric_path'])
        task = read(path)
        task['difficulty'] = entry['difficulty'] = by_id[task_id]['suggested_difficulty']
        task['review']['difficulty_status'] = 'approved'
        task['rubric_sha256'] = digest(rubric)
        put(path, task)
        entry['fingerprints']['task'] = digest(task)
        entry['fingerprints']['rubric'] = digest(rubric)
    for track in ('dev', 'staged-dev'):
        path = ROOT / 'data' / track / 'manifest.json'
        manifest = read(path)
        for task_id in manifest['tasks']:
            task = read(ROOT / 'data' / track / task_id / 'task.json')
            rubric = read(ROOT / 'data' / track / task_id / 'rubric.json')
            manifest['fingerprints'][task_id]['task'] = digest(task)
            manifest['fingerprints'][task_id]['rubric'] = digest(rubric)
        manifest['document_sha256'] = sha((ROOT / 'data/questions-and-scoring.md').read_bytes())
        manifest['difficulty_counts'] = dict(sorted(Counter(
            by_id[task_id]['suggested_difficulty'] for task_id in manifest['tasks']).items()))
        manifest['difficulty_approval'] = 'owner_confirmed_' + APPROVAL_DATE
        put(path, manifest)
        selection['source_files_sha256']['data/' + track + '/manifest.json'] = sha(path.read_bytes())
    selection['difficulty_counts'] = counts
    selection['difficulty_approval'] = 'owner_confirmed_' + APPROVAL_DATE
    selection['difficulty_approval_file'] = APPROVAL.relative_to(ROOT).as_posix()
    selection['difficulty_approval_sha256'] = sha(APPROVAL.read_bytes())
    selection['source_document_sha256'] = sha((ROOT / selection['source_document']).read_bytes())
    selection['source_files_sha256']['data/questions-and-scoring.md'] = sha(
        (ROOT / 'data/questions-and-scoring.md').read_bytes())
    selection_path = ROOT / 'config/selections/current.json'
    put(selection_path, selection)
    # Derived episode definitions pin task hashes. Metadata approval changes those
    # hashes without changing the questions or their original scoring.
    episodes_path = ROOT / 'data/longitudinal/episodes.json'
    episodes = read(episodes_path)
    for episode in episodes['episodes']:
        for stage in episode['stages']:
            task_id = stage['task_id']
            if task_id in by_id:
                entry = next(row for row in selection['tasks'] if row['task_id'] == task_id)
                stage['task_sha256'] = entry['fingerprints']['task']
                stage['rubric_sha256'] = entry['fingerprints']['rubric']
    put(episodes_path, episodes)


def sync_hf(hf_root, selection, by_id, counts):
    hf_root = hf_root.resolve()
    if hf_root == ROOT or not (hf_root / 'selection-70.json').is_file():
        raise ValueError('Expected a separate approved HF current70 snapshot')
    hf_selection = read(hf_root / 'selection-70.json')
    if hf_selection['task_ids'] != selection['task_ids'] or hf_selection['task_count'] != 70:
        raise ValueError('HF snapshot does not match the current selected 70 IDs')
    task_rows = {}
    rubric_rows = {}
    for name, table in (('single-stage.jsonl', task_rows), ('staged.jsonl', task_rows),
                        ('rubrics.jsonl', rubric_rows)):
        for row in (json.loads(line) for line in (hf_root / name).read_text().splitlines() if line):
            if row['task_id'] in table:
                raise ValueError('Duplicate HF row: ' + row['task_id'])
            table[row['task_id']] = row
    if set(task_rows) != set(selection['task_ids']) or set(rubric_rows) != set(task_rows):
        raise ValueError('HF JSONL rows do not cover the selected tasks')
    node_differences = []
    reference_differences = []
    for entry, hf_entry in zip(selection['tasks'], hf_selection['tasks']):
        task_id = entry['task_id']
        if hf_entry['task_id'] != task_id:
            raise ValueError('HF selection order differs')
        source_task = read(ROOT / entry['task_path'])
        source_rubric = read(ROOT / entry['rubric_path'])
        task_path = hf_root / 'tasks' / task_id / 'task.json'
        rubric_path = hf_root / 'tasks' / task_id / 'rubric.json'
        task, rubric = read(task_path), read(rubric_path)
        if any(task.get(key) != source_task.get(key) for key in ('prompt', 'stages')):
            raise ValueError('Question body differs: ' + task_id)
        if [(n['id'], n['points']) for n in rubric['nodes']] != [
                (n['id'], n['points']) for n in source_rubric['nodes']]:
            raise ValueError('Scoring node IDs or points differ: ' + task_id)
        rubric['nodes'] = source_rubric['nodes']
        rubric['reference_candidates'] = source_rubric.get('reference_candidates', [])
        if task['task_version'] != source_task['task_version']:
            raise ValueError('Task version differs: ' + task_id)
        task['difficulty'] = entry['difficulty']
        task['review'].update({key: source_task['review'][key] for key in (
            'content_status', 'difficulty_status', 'teacher_calibration')})
        rubric['teacher_calibration'] = source_rubric['teacher_calibration']
        put(rubric_path, rubric)
        task['rubric_sha256'] = digest(rubric)
        put(task_path, task)
        hf_entry['difficulty'] = entry['difficulty']
        hf_entry['fingerprints'] = {'task': digest(task), 'rubric': digest(rubric)}
        for row in (task_rows[task_id], rubric_rows[task_id]):
            if 'effective_review' in row:
                row['effective_review']['teacher_calibration'] = 'completed_owner_confirmed'
                row['effective_review']['basis'] = 'owner_confirmed_current_public70'
                # Historical score equivalence is a separate, unverified question.
                row['effective_review']['actual_score_equivalence_verified'] = False
        task_rows[task_id]['task_json'] = json.dumps(task, ensure_ascii=False, sort_keys=True)
        task_rows[task_id]['task_sha256'] = digest(task)
        task_rows[task_id]['rubric_sha256'] = digest(rubric)
        rubric_rows[task_id]['rubric_json'] = json.dumps(rubric, ensure_ascii=False, sort_keys=True)
        rubric_rows[task_id]['rubric_sha256'] = digest(rubric)
    for name, rows in (('single-stage.jsonl', task_rows), ('staged.jsonl', task_rows),
                       ('rubrics.jsonl', rubric_rows)):
        old_ids = [json.loads(line)['task_id'] for line in (hf_root / name).read_text().splitlines() if line]
        jsonl(hf_root / name, [rows[task_id] for task_id in old_ids])
    hf_selection['difficulty_counts'] = counts
    hf_selection['difficulty_approval'] = 'owner_confirmed_' + APPROVAL_DATE
    for name in hf_selection['source_files_sha256']:
        path = hf_root / name
        if not path.is_file() or not path.resolve().is_relative_to(hf_root):
            raise ValueError('HF selection dependency is missing or unsafe: ' + name)
        hf_selection['source_files_sha256'][name] = sha(path.read_bytes())
    put(hf_root / 'selection-70.json', hf_selection)
    manifest = read(hf_root / 'manifest.json')
    manifest['difficulty_counts'] = counts
    manifest['difficulty_approval'] = 'owner_confirmed_' + APPROVAL_DATE
    manifest['teacher_calibration'] = 'completed_owner_confirmed'
    manifest['projection_schema'] = 'github-source-to-hf-public70.1'
    manifest['github_source_selection_sha256'] = sha((ROOT / 'config/selections/current.json').read_bytes())
    manifest['projection_note'] = ('HF questions, scoring nodes and reference candidates match the '
                                   'GitHub selected source. Projection-only rubric metadata and full '
                                   'rubric hashes remain separate.')
    manifest['projection_differences'] = {
        'rubric_nodes_task_ids': node_differences,
        'reference_candidates_task_ids': reference_differences,
        'scoring_document_differs': sha((hf_root / 'scoring.md').read_bytes()) !=
            sha((ROOT / 'docs/scoring.md').read_bytes()),
    }
    manifest['github_source_scoring_sha256'] = sha((ROOT / 'docs/scoring.md').read_bytes())
    manifest['selection_sha256'] = sha((hf_root / 'selection-70.json').read_bytes())
    put(hf_root / 'manifest.json', manifest)
    checksums(hf_root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hf-root', type=Path, help='Separate approved current70 HF checkout')
    args = parser.parse_args()
    selection = read(ROOT / 'config/selections/current.json')
    by_id, counts = approved_rows(selection)
    update_github(selection, by_id, counts)
    if args.hf_root:
        sync_hf(args.hf_root, selection, by_id, counts)
    print(json.dumps({'tasks': 70, 'difficulty_counts': counts, 'hf_updated': bool(args.hf_root)},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
