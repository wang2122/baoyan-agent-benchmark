"""Execution/evaluation compatibility, separate from full release provenance.

Membership is explicit and versioned. Changed behavior still invalidates a batch;
operator guides and independent analyses are not execution identity.
"""
from pathlib import Path

from .core import digest, file_hash, require

SCHEMA = 'workflow-components.1'
COMMON = ('__init__.py', '__main__.py', 'runtime_common.py', 'core.py', 'batch.py',
          'runner.py', 'manual.py', 'packaging.py', 'reproduction.py', 'workflow_versions.py')
NATIVE = ('native.py', 'native_runtime.py', 'native_auth.py', 'cli_adapters.py', 'cli_batch.py',
          'cli_common.py', 'cli_packet.py', 'cli_packet_runner.py', 'cli_setup.py', 'credentials.py',
          'isolation.py', 'network.py', 'native_sessions.py', 'episode_state.py')
LEGACY = ('adapters.py', 'isolation.py', 'network.py', 'cli_common.py')
EVALUATION = ('core.py', 'runtime_common.py', 'scoring.py', 'project_matching.py', 'metrics.py', 'submissions.py',
              'archives.py', 'reproduction.py', 'workflow_versions.py', 'reports.py', 'judge.py')


def component_files(root, names):
    root = Path(root).resolve()
    result = {}
    for name in sorted(set(names)):
        path = root / name
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root),
                'Missing or linked workflow component: ' + name)
        result[name] = file_hash(path)
    return result


def components(root, source):
    require(source in ('fixed_profile', 'native_cli', 'study_protocol', 'legacy_batch'),
            'Unknown workflow source for component identity')
    runtime = set(COMMON) | set(NATIVE if source == 'native_cli' else LEGACY)
    if source == 'study_protocol':
        runtime.add('study.py')
    names = ['benchmark/' + name for name in runtime]
    if source != 'native_cli':
        names.append('scripts/baselines.py')
    evaluation = ['benchmark/' + name for name in EVALUATION]
    evaluation += ['docs/scoring.md', 'docs/review-protocol.md']
    return {'schema_version': SCHEMA, 'runtime': component_files(root, names),
            'evaluation': component_files(root, evaluation)}


def verify_components(root, context):
    expected = components(root, context.get('source', 'legacy_batch'))
    saved = context.get('components')
    require(isinstance(saved, dict) and set(saved) == set(expected)
            and saved.get('schema_version') == SCHEMA, 'Missing or unsupported workflow component identity')
    for name in ('runtime', 'evaluation'):
        require(saved[name] == expected[name], name + ' component changed; use the matching frozen release')


def context_identity(context):
    """Ignore full-release audit changes only for the explicitly versioned new context."""
    if context.get('schema_version') == 'benchmark-reproduction.2':
        require(context.get('components', {}).get('schema_version') == SCHEMA,
                'New reproduction context requires component identity')
        audit_fields = {'engine_sha256'}
        if context.get('source') == 'native_cli' and context.get('recipe_sha256'):
            # The selected immutable recipes are identity; unrelated local registrations are provenance.
            audit_fields.add('registry_file_sha256')
        return {k: v for k, v in context.items() if k not in audit_fields}
    return context


def same_context(left, right):
    return context_identity(left) == context_identity(right)


def runtime_identity(root, source):
    return digest(components(root, source)['runtime'])
