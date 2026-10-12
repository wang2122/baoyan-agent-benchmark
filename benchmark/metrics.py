"""Coverage, task scores and resource metrics; unknown values remain null."""
import math
from .core import require

def validate_usage(value):
    require(isinstance(value, dict), 'Usage must be an object')
    for key in ('input_tokens', 'output_tokens'):
        v = value.get(key)
        require(v is None or (type(v) is int and v >= 0), 'Invalid token count')
    cost = value.get('cost_usd')
    require(cost is None or (type(cost) in (int, float) and math.isfinite(cost) and cost >= 0), 'Invalid cost')
    return {k: value.get(k) for k in ('input_tokens', 'output_tokens', 'cost_usd')}


def metrics(expected, attempts, protocol):
    def partition(field):
        result = {}
        for label in sorted({t[field] or 'uncalibrated' for t in expected.values()}):
            ids = [sid for sid, t in expected.items() if (t[field] or 'uncalibrated') == label]
            values = [attempts[sid]['score']['score'] for sid in ids if sid in attempts
                      and attempts[sid]['score'] is not None and attempts[sid]['score']['score'] is not None]
            result[label] = {'expected': len(ids), 'scored': len(values),
                             'mean': round(sum(values) / len(ids), 2) if len(values) == len(ids) else None}
        return result
    count = len(expected)
    require(count > 0, 'No tasks selected')
    resolved = [a['score'] for a in attempts.values() if a['score'] is not None and a['score']['score'] is not None]
    completed = sum(a['meta']['status'] == 'completed' for a in attempts.values())
    threshold = protocol.get('success_threshold')
    require(threshold is None or (type(threshold) in (int, float) and math.isfinite(threshold)
                                  and 0 <= threshold <= 100), 'Invalid success threshold')
    recommendation_count = sum(t['task_kind'] == 'recommendation' for t in expected.values())
    recommendations = [s for s in resolved if s['task_kind'] == 'recommendation']
    means = {name: (round(sum(s['confirmed_component_points'][name] for s in recommendations)
                          / recommendation_count, 2)
                   if recommendation_count and len(recommendations) == recommendation_count else None)
             for name in ('coverage', 'quality')}
    usage = [validate_usage(a['meta'].get('usage') or {}) for a in attempts.values()]
    trace_required = [sid for sid, t in expected.items() if t['execution'].get('trajectory_required', False)]
    trace_recorded = sum(sid in attempts and attempts[sid]['meta'].get('trajectory_status') == 'recorded_unverified'
                         for sid in trace_required)
    totals = {k: (round(sum(u[k] for u in usage), 6) if len(usage) == count
                  and all(u[k] is not None for u in usage) else None)
              for k in ('input_tokens', 'output_tokens', 'cost_usd')}
    return {'by_task_kind': partition('task_kind'), 'by_difficulty': partition('difficulty'),
            'trajectory_requirements': {'required_tasks': len(trace_required), 'recorded_tasks': trace_recorded,
                                        'complete': trace_recorded == len(trace_required),
                                        'provenance': 'adapter_or_operator_reported_not_replayed'},
            'recommendation_component_means': means, 'completion_rate': round(completed / count, 4),
            'success_rate': (round(sum(s['score'] >= threshold for s in resolved) / count, 4)
                             if threshold is not None and len(resolved) == count else None),
            'success_threshold': threshold, 'usage': totals, 'usage_provenance': 'adapter_reported_unverified'}
