"""Portable file primitives: contention, atomic failure and bounded regular reads."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchmark.runtime_common import file_lock, regular_bytes, write

ROOT = Path(__file__).resolve().parents[1]


class RuntimeCommonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_lock_contention_across_processes_and_release(self):
        lock = self.base / 'run.lock'
        script = ('from benchmark.runtime_common import file_lock\n'
                  'import sys\n'
                  'with file_lock(sys.argv[1], blocking=False): print("locked")\n')
        with file_lock(lock):
            result = subprocess.run([sys.executable, '-c', script, str(lock)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
        result = subprocess.run([sys.executable, '-c', script, str(lock)], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'locked')
        self.assertTrue(lock.exists())

    def test_windows_backend_releases_one_byte_and_reports_contention(self):
        windows_os = SimpleNamespace(**{**vars(os), 'name': 'nt'})
        msvcrt = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        lock = self.base / 'windows.lock'
        with patch('benchmark.runtime_common.os', windows_os), patch.dict(sys.modules, msvcrt=msvcrt):
            with file_lock(lock):
                self.assertEqual(lock.read_bytes(), b'\0')
            self.assertEqual([c.args[1:] for c in msvcrt.locking.call_args_list], [(2, 1), (0, 1)])
            msvcrt.locking.side_effect = OSError(errno.EACCES, 'locked')
            with self.assertRaisesRegex(ValueError, 'busy fixture'):
                with file_lock(lock, blocking=False, busy_message='busy fixture'):
                    self.fail('Contended lock entered')

    def test_atomic_write_failure_preserves_previous_bytes(self):
        path = self.base / 'result.json'; path.write_bytes(b'original')
        with patch('benchmark.runtime_common.os.replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                write(path, {'new': True})
        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual(list(self.base.iterdir()), [path])

    def test_regular_reader_rejects_links_and_limits(self):
        path = self.base / 'answer.md'; path.write_bytes(b'original')
        self.assertEqual(regular_bytes(path), b'original')
        with self.assertRaisesRegex(ValueError, 'size limit'):
            regular_bytes(path, 3)
        if os.name != 'nt':
            link = self.base / 'linked'; link.symlink_to(path)
            with self.assertRaises(OSError):
                regular_bytes(link)
            hard = self.base / 'hard'; os.link(path, hard)
            with self.assertRaises(ValueError):
                regular_bytes(hard)


if __name__ == '__main__':
    unittest.main()
