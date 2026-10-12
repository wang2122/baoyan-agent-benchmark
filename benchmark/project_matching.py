"""Exact project weights and maximum-weight one-to-one reference assignment."""
from fractions import Fraction

from .core import require

POLICY = 'layered_project_match.1'
FACTORS = {'exact': Fraction(1), 'same_department': Fraction(3, 5),
           'related_department': Fraction(3, 10)}
OUTCOME_WEIGHTS = {'camp': 1, 'excellent_camp': 2, 'admission': 3}


def reference_weights(rubric):
    """Use only verified project-level outcomes stored in the frozen rubric."""
    weights = {}
    for ref in rubric['reference_candidates']:
        outcomes = ref.get('historical_outcomes', [])
        require(isinstance(outcomes, list), '项目历史结果须为数组')
        weight = 1
        for outcome in outcomes:
            require(isinstance(outcome, dict) and type(outcome.get('verified')) is bool,
                    '历史结果须明确是否核实')
            if not outcome['verified']:
                continue
            require(outcome.get('status') in OUTCOME_WEIGHTS, '未知项目历史结果状态')
            require(outcome.get('reference_id') == ref['id'], '历史结果必须绑定具体参考项目')
            require(all(isinstance(outcome.get(k), str) and outcome[k].strip()
                        for k in ('source', 'applicable_cycle', 'scope')), '核准历史结果缺少证据或适用范围')
            weight = max(weight, OUTCOME_WEIGHTS[outcome['status']])
        weights[ref['id']] = weight
    denominator = sum(weights.values())
    return {ident: Fraction(rubric['coverage_points'] * weight, denominator)
            for ident, weight in weights.items()}


def maximum_assignment(reference_ids, edges):
    """Hungarian assignment with zero-weight dummy columns for unmatched refs.

    edges maps (reference ID, canonical candidate key) to a positive Fraction.
    Duplicate answer occurrences are merged by the caller before this step.
    Stable input order and sorted candidate keys resolve equally optimal ties.
    """
    keys = sorted({key for _, key in edges})
    n, m = len(reference_ids), len(keys) + len(reference_ids)
    if not n or not keys:
        return {}
    cost = [[-edges.get((ident, key), Fraction(0)) for key in keys] + [Fraction(0)] * n
            for ident in reference_ids]
    u, v = [Fraction(0)] * (n + 1), [Fraction(0)] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minima, used = [None] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], None, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                current = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if minima[j] is None or current < minima[j]:
                    minima[j], way[j] = current, j0
                if delta is None or minima[j] < delta:
                    delta, j1 = minima[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                elif minima[j] is not None:
                    minima[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return {reference_ids[p[j] - 1]: keys[j - 1]
            for j in range(1, len(keys) + 1) if p[j] and
            (reference_ids[p[j] - 1], keys[j - 1]) in edges}
