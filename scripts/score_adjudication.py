"""Replay explicit node rulings without rewriting candidate facts or original reviews.

This maintenance layer is separate from benchmark.scoring.score. It accepts only
pending nodes, uses the frozen rubric's weights/grades, and preserves the original
interval and every unadjudicated contribution. It does not reassess candidates.
"""
import argparse
import copy
import hashlib
import json
from fractions import Fraction
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark.core import digest
from benchmark.scoring import reference_weights, rounded


def require(value, message):
    if not value:
        raise ValueError(message)


def replay(original, rubric, ledger):
    """Return an effective-score record; original objects are never modified."""
    require(ledger['schema'] == 'node-adjudication-ledger.1', 'Unknown ledger schema')
    require(original['task_id'] == rubric['id'] == ledger['task_id'], 'Task mismatch')
    require(original['answer_sha256'] == ledger['answer_sha256'], 'Answer mismatch')
    require(original['rubric_sha256'] == ledger['original_rubric_sha256'], 'Rubric version mismatch')
    require(digest(original) == ledger['original_score_content_sha256'], 'Original score content changed')
    require(digest(rubric) == ledger['original_rubric_content_sha256'], 'Frozen display rubric changed')
    require(original['status'] != 'quantity_not_comparable', 'Quantity hold cannot be settled by a node overlay')
    nodes = original['nodes']
    require(len({n['id'] for n in nodes}) == len(nodes), 'Duplicate original node')
    rules = {n['id']: n for n in rubric['nodes']}
    weights = reference_weights(rubric)
    known = set(weights) | {n['id'] for n in rubric['nodes'] if n['grading'] in ('three_level', 'school_tier')}
    require({n['id'] for n in nodes} == known, 'Incomplete original node ledger')
    changes = ledger['rulings']
    require(len({x['node_id'] for x in changes}) == len(changes), 'Duplicate ruling')
    require({x['node_id'] for x in changes} <= set(original['pending_nodes']), 'Only pending nodes may be adjudicated')
    by_id = {x['node_id']: x for x in changes}
    original_total = Fraction(0); original_pending = Fraction(0)
    total = Fraction(0); pending_weight = Fraction(0); remaining = []; output = []
    for node in nodes:
        ident = node['id']
        if node['kind'] == 'coverage':
            maximum = Fraction(node['weight']['numerator'], node['weight']['denominator'])
            require(maximum == weights[ident], 'Coverage weight differs from frozen rubric')
            old_pending = node['status'] == 'pending'
            if node['status'] == 'hit':
                factor = Fraction(str(node.get('match_coefficient', 1)))
                require(factor in (Fraction(1), Fraction(3, 5), Fraction(3, 10)), 'Unknown match coefficient')
                contribution = maximum * factor
            else:
                require(node['status'] in ('miss', 'pending'), 'Unknown coverage status')
                contribution = Fraction(0)
        else:
            require(node['kind'] in ('quality', 'school_tier'), 'Unsupported node kind')
            maximum = Fraction(str(rules[ident]['points']))
            if node['kind'] == 'quality':
                require(rules[ident]['grading'] == 'three_level', 'Quality grading mismatch')
                old_pending = node['grade'] == 'pending'
                require(old_pending or type(node['grade']) in (int, float) and node['grade'] in (0, .5, 1), 'Invalid original grade')
                contribution = Fraction(0) if old_pending else maximum * Fraction(str(node['grade']))
            else:
                old_pending = node['points'] is None
                contribution = Fraction(0) if old_pending else Fraction(str(node['points']))
        require(old_pending == (ident in original['pending_nodes']), 'Pending-node binding mismatch')
        require(node['points'] is None if old_pending else node['points'] == rounded(contribution), 'Original node points mismatch')
        original_total += contribution
        if old_pending: original_pending += maximum
        ruling = by_id.get(ident)
        pending = old_pending
        disposition = 'retained_original'
        if ruling:
            require(bool(ruling.get('decision')), 'Missing ruling rationale')
            status = ruling['status']
            require(status in ('owner_policy_no_verified_credit', 'resolved_by_official_evidence', 'resolved_counter_evidence'), 'Unsupported ruling status')
            require(type(ruling['grade']) in (int, float) and ruling['grade'] in (0, .5, 1), 'Ruling outside original grading levels')
            if status == 'owner_policy_no_verified_credit':
                require(ruling['grade'] == 0 and ruling['points'] == 0, 'No-credit policy must award zero')
                require(bool(ruling.get('policy_authorization')) and ident in ledger.get('authorized_policy_nodes', []), 'Policy authorization missing')
                require(ruling.get('factual_status') == 'unresolved', 'Policy settlement cannot assert a resolved fact')
            else:
                require(bool(ruling.get('sources')) and all(s.get('url', '').startswith(('https://', 'http://')) for s in ruling['sources']), 'Evidence source missing')
            if node['kind'] == 'quality':
                require(ruling.get('rule') == rules[ident], 'Ruling must quote the original rubric rule')
                require(bool(rules[ident].get('half_credit')), 'Original half-credit rule missing')
                contribution = maximum * Fraction(str(ruling['grade']))
            else:
                require(node['kind'] == 'coverage' and ruling['grade'] == 0, 'Only no-credit coverage decisions are supported')
                contribution = Fraction(0)
            require(ruling['points'] == rounded(contribution), 'Ruling points do not follow the original weight')
            pending = False; disposition = status
        total += contribution
        if pending: remaining.append(ident); pending_weight += maximum
        output.append({'id': ident, 'kind': node['kind'], 'original_node': copy.deepcopy(node),
                       'effective_points': None if pending else rounded(contribution),
                       'effective_fraction': None if pending else {'numerator': contribution.numerator, 'denominator': contribution.denominator},
                       'maximum_fraction': {'numerator': maximum.numerator, 'denominator': maximum.denominator},
                       'disposition': disposition})
    require(original['score_interval'] == [rounded(original_total), rounded(original_total + original_pending)], 'Original interval cannot be reconstructed')
    require(0 <= total <= total + pending_weight <= 100, 'Effective score out of bounds')
    interval = [rounded(total), rounded(total + pending_weight)]
    if 'expected_interval' in ledger: require(interval == ledger['expected_interval'], 'Effective interval differs from accepted proposal')
    return {'schema': 'effective-node-score.1', 'task_id': original['task_id'], 'answer_sha256': original['answer_sha256'],
            'original_rubric_sha256': original['rubric_sha256'], 'original_score_interval': original['score_interval'][:],
            'original_pending_nodes': original['pending_nodes'][:], 'score_interval': interval,
            'score': None if remaining else interval[0], 'pending_nodes': remaining,
            'nodes': output, 'official': False, 'teacher_calibration': 'pending',
            'original_candidate_facts_changed': False, 'original_judgment_changed': False,
            'validation_mode': 'maintenance_node_overlay_not_native_candidate_revalidation'}


def replay_files(root, item):
    values = {}
    for key in ('original_score', 'original_rubric', 'ledger'):
        relative = Path(item[key]['path'])
        require(not relative.is_absolute() and '..' not in relative.parts, 'Unsafe public evidence path')
        p = root / relative
        require(hashlib.sha256(p.read_bytes()).hexdigest() == item[key]['sha256'], 'Evidence file hash changed: ' + key)
        values[key] = json.loads(p.read_text())
    value = replay(values['original_score'], values['original_rubric'], values['ledger'])
    expected = json.loads((root / item['effective_score_path']).read_text())
    require(value == expected, 'Published effective score does not reproduce')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--ledger', type=Path, default=Path('experiments/execution-records/dev70/materials/qwen-adjudication-ledger.json'))
    args = parser.parse_args()
    bundle = json.loads((args.root / args.ledger).read_text())
    rows = [replay_files(args.root, x) for x in bundle['records']]
    print(json.dumps({'status': 'passed', 'replayed_tasks': len(rows), 'intervals': {r['task_id']: r['score_interval'] for r in rows}}))


if __name__ == '__main__': main()
