"""Declared DSH dependencies may load; unrelated packages and host links may not."""
import json
import tempfile
import unittest
from pathlib import Path

from benchmark.isolation import npm_runtime_dependencies


class DshDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.npm = self.root / 'node_modules'

    def package(self, name, **fields):
        p = self.npm / name
        p.mkdir(parents=True)
        (p / 'package.json').write_text(json.dumps(dict(name=name, **fields)))
        return p

    def test_transitive_cycle_and_unrelated_packages(self):
        dsh = self.package('@deepseek-ai/dsh', dependencies={'@deepseek-ai/boot': '1'})
        boot = self.package('@deepseek-ai/boot', dependencies={'helper': '1'})
        helper = self.package('helper', dependencies={'@deepseek-ai/boot': '1'})
        unrelated = self.package('unrelated')
        paths = npm_runtime_dependencies(dsh)
        self.assertEqual(set(paths), {boot, helper})
        self.assertNotIn(self.npm, paths)
        self.assertNotIn(unrelated, paths)

    def test_nested_dependency_precedes_hoisted_version(self):
        dsh = self.package('@deepseek-ai/dsh', dependencies={'helper': '1'})
        hoisted = self.package('helper')
        nested = dsh / 'node_modules/helper'
        nested.mkdir(parents=True)
        (nested / 'package.json').write_text('{"name":"helper"}')
        self.assertEqual(npm_runtime_dependencies(dsh), [nested])
        self.assertNotIn(hoisted, npm_runtime_dependencies(dsh))

    def test_missing_required_is_error_but_optional_can_be_absent(self):
        dsh = self.package('@deepseek-ai/dsh', optionalDependencies={'optional-platform': '1'},
                           peerDependencies={'peer': '1', 'optional-peer': '1'},
                           peerDependenciesMeta={'optional-peer': {'optional': True}})
        peer = self.package('peer')
        self.assertEqual(npm_runtime_dependencies(dsh), [peer])
        (dsh / 'package.json').write_text('{"dependencies":{"required":"1"}}')
        with self.assertRaisesRegex(ValueError, 'Missing installed'):
            npm_runtime_dependencies(dsh)

    def test_symlink_to_host_checkout_and_traversal_are_rejected(self):
        dsh = self.package('@deepseek-ai/dsh', dependencies={'linked': '1'})
        host = self.root / 'private-project'
        host.mkdir()
        (host / 'package.json').write_text('{"name":"linked"}')
        (self.npm / 'linked').symlink_to(host, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            npm_runtime_dependencies(dsh)
        (dsh / 'package.json').write_text('{"dependencies":{"../private-project":"1"}}')
        with self.assertRaisesRegex(ValueError, 'Invalid npm dependency name'):
            npm_runtime_dependencies(dsh)


if __name__ == '__main__':
    unittest.main()
