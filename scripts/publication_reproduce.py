#!/usr/bin/env python3
"""Offline verification/preparation for an exported experiment; never calls a model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify_source(folder):
    engine = folder / 'engine'
    manifest = read(folder / 'source-manifest.json')
    expected = manifest['exported_files']
    if manifest.get('projection') == 'public-task-allowlist.1':
        original = manifest['original_files']
        omitted = set(manifest['omitted'])
        changed = manifest['changed']
        if set(expected) != set(original) - omitted:
            raise ValueError('Invalid projected source membership')
        for name, value in expected.items():
            if name in changed:
                if changed[name]['original_sha256'] != original[name] or changed[name]['export_sha256'] != value:
                    raise ValueError('Invalid projection hash binding')
            elif value != original[name]:
                raise ValueError('Undisclosed source change')
    elif expected != manifest['original_files']:
        raise ValueError('Incomplete frozen source tree')
    actual = {}
    for p in engine.rglob('*'):
        if p.is_symlink():
            raise ValueError('Linked frozen source')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
            actual[p.relative_to(engine).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError('Frozen source files changed, missing, or added')
    return engine


WORKER = r'''
import json, socket, subprocess, sys
from pathlib import Path
engine, registry, selection, agent, out = map(str, sys.argv[1:])
sys.path.insert(0, engine)
def forbidden(*args, **kwargs):
    raise RuntimeError('Offline preflight forbids external processes and network calls')
subprocess.Popen = forbidden
socket.socket.connect = forbidden
socket.create_connection = forbidden
from benchmark.parallel_batch import prepare_subset, verify
result = prepare_subset(Path(engine), Path(registry), Path(selection), [agent], Path(out))
verify(Path(out))
print(json.dumps({'agent': agent, 'tasks': result['planned'], 'status': 'prepared_and_verified',
                  'model_calls': 0}, ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    bundle = args.bundle.resolve(); out = args.out.resolve()
    if out.exists():
        raise ValueError('Use a new output directory')
    configs = read(bundle / 'configurations.json')
    sources = {}
    for config in configs:
        directory = (bundle / config['engine']).resolve()
        if not directory.is_relative_to(bundle / 'sources'):
            raise ValueError('Source path escapes bundle')
        if directory not in sources:
            sources[directory] = verify_source(directory)
    out.mkdir(parents=True)
    reports = []
    for config in configs:
        folder = (bundle / config['engine']).resolve()
        dest = out / config['agent']
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(out / 'empty-home'),
               'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'en_US.UTF-8'}
        Path(env['HOME']).mkdir(exist_ok=True)
        result = subprocess.run([sys.executable, '-I', '-B', '-c', WORKER, str(sources[folder]),
            str(folder / 'registry.json'), str(folder / 'selection.json'), config['agent'], str(dest)],
            env=env, cwd=out, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('Offline preparation failed for ' + config['agent'] + ': ' + result.stderr[-3000:])
        reports.append(json.loads(result.stdout))
    report = {'status': 'passed', 'mode': 'local_clean_environment_offline_preflight',
              'independent_machine': False, 'real_model_execution': False,
              'python': sys.version.split()[0], 'configurations': reports,
              'source_snapshots_verified': len(sources), 'credentials_used': False,
              'source_bindings': {folder.relative_to(bundle).as_posix():
                  {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                   for name in ('source-manifest.json', 'registry.json', 'selection.json')}
                  for folder in sources},
              'verifier_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out / 'preflight.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
