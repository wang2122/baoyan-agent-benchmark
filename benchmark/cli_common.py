"""Small answer-side helpers; never imports task banks or scoring code."""
import math
import os
import re
import signal
import subprocess
from pathlib import Path

from .runtime_common import digest, file_lock, now, read, require, sha, write


def result_exit_code(result):
    """Translate command outcomes, including returned partial reports, to shell status."""
    if not isinstance(result, dict):
        return 0
    status = result.get('status')
    if status == 'interrupted' or result.get('exit_code') == 130:
        return 130
    for name in ('runs', 'records'):
        rows = result.get(name, [])
        if isinstance(rows, list) and any(isinstance(row, dict) and row.get('status') == 'interrupted'
                                         for row in rows):
            return 130
    if status in ('setup_required', 'incomplete', 'failed', 'timeout', 'invalid_output',
                  'model_mismatch', 'launch_failed', 'missing_answer'):
        return 1
    for expected in ('planned', 'expected'):
        if type(result.get(expected)) is int and type(result.get('completed')) is int:
            if result['completed'] != result[expected]:
                return 1
    records = result.get('records')
    if isinstance(records, list) and type(result.get('completed')) is int and result['completed'] != len(records):
        return 1
    code = result.get('exit_code')
    return code if type(code) is int and 0 <= code <= 255 else 0


def safe_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value),
            'Invalid task or agent ID')
    return value


def validate_usage(value):
    require(isinstance(value, dict), 'Usage must be an object')
    for key in ('input_tokens', 'output_tokens'):
        v = value.get(key)
        require(v is None or type(v) is int and v >= 0, 'Invalid token count')
    cost = value.get('cost_usd')
    require(cost is None or type(cost) in (int, float) and math.isfinite(cost) and cost >= 0,
            'Invalid cost')
    return {k: value.get(k) for k in ('input_tokens', 'output_tokens', 'cost_usd')}


def regular(root, relative):
    """Read only regular files under root; reject symlinked parent directories too."""
    root = Path(root)
    path = root / relative
    require(not root.is_symlink() and path.is_file() and not path.is_symlink(), 'Missing or symlink file')
    require(path.resolve().is_relative_to(root.resolve()), 'File escapes its packet')
    for parent in path.parents:
        if parent == root:
            break
        require(not parent.is_symlink(), 'Symlink directory inside packet')
    return path


def stop_owned(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
