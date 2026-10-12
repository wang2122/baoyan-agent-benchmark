"""Incremental local setup and selected credential provenance; no installed CLIs required."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark.__main__ import main
from benchmark.cli_setup import setup
from benchmark.core import read, write
from benchmark.credentials import credential_environment, credentials_path, import_credentials, save_credentials

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.catalog = read(ROOT / 'config/agents-cli.example.json')
        self.source = self.base / 'config/agents-cli.example.json'
        self.out = self.base / 'runs/config/agents.local.json'
        write(self.source, self.catalog)
        self.output = io.StringIO()
        stream = redirect_stdout(self.output); stream.__enter__()
        self.addCleanup(stream.__exit__, None, None, None)
        self.addCleanup(patch.stopall)
        patch('benchmark.cli_setup.shutil.which', return_value=None).start()
        patch('benchmark.cli_adapters.check_agent', side_effect=lambda agent, work, **kw: {
            'id': agent['id'], 'issues': [], 'installed_version': None,
            'credentials': kw.get('credential_source')}).start()
        patch.dict(os.environ, {}, clear=True).start()

    def test_main_adds_agents_from_catalog_and_preserves_saved_recipe(self):
        main(['--root', str(self.base), 'setup', '--agent', 'deepseek-v4-1-flash', '--non-interactive'])
        value = read(self.out)
        value['agents'][0].update(binary='/chosen/local/dsh', cli_version='pinned-custom', model='chosen-model')
        write(self.out, value)
        before = copy.deepcopy(value['agents'][0])
        result = main(['--root', str(self.base), 'setup', '--agent', 'antigravity-gemini-3-8-flash-high', '--non-interactive'])
        saved = {a['id']: a for a in read(self.out)['agents']}
        self.assertEqual(saved[before['id']], before)
        self.assertEqual(set(saved), {'deepseek-v4-1-flash', 'antigravity-gemini-3-8-flash-high'})
        self.assertEqual(result['selected_agents'], ['antigravity-gemini-3-8-flash-high'])
        self.assertEqual([r['id'] for r in result['agents']], ['antigravity-gemini-3-8-flash-high'])
        # Editing only one of the two configured products keeps the other one.
        result = main(['--root', str(self.base), 'setup', '--agent', 'deepseek-v4-1-flash', '--non-interactive'])
        self.assertEqual(set(result['configured_agents']), set(saved))
        self.assertEqual(read(self.out)['agents'][0], before)

    def test_explicit_import_is_selected_private_and_never_printed(self):
        source = self.base / '.env'
        source.write_text("export DEEPSEEK_API_KEY='fixture-key-value'\nUNRELATED_KEY=never-import\n")
        result = setup(self.source, self.out, ['deepseek-v4-1-flash'], interactive=False, import_path=source)
        secret_file = credentials_path(self.out)
        self.assertEqual(read(secret_file), {'DEEPSEEK_API_KEY': 'fixture-key-value'})
        if os.name == 'posix':
            self.assertEqual(secret_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('fixture-key-value', self.output.getvalue() + json.dumps(result) + self.out.read_text())
        self.assertEqual(result['agents'][0]['credentials']['variables'][0]['source'], 'local_credentials_file')
        self.assertNotIn('DEEPSEEK_API_KEY', os.environ)
        self.assertEqual(result['agents'][0]['credentials']['account_verification'], 'not_checked')

    def test_environment_wins_and_only_selected_names_are_loaded(self):
        registry = dict(self.catalog, agents=[next(a for a in self.catalog['agents'] if a['cli'] == 'dsh')])
        save_credentials(credentials_path(self.out), {'DEEPSEEK_API_KEY': 'local', 'UNRELATED_KEY': 'private'})
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'existing'}):
            with credential_environment(registry, self.out) as sources:
                self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'existing')
                self.assertNotIn('UNRELATED_KEY', os.environ)
                self.assertEqual(sources[registry['agents'][0]['id']]['variables'][0]['source'], 'environment')
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'existing')

    def test_import_uses_the_auth_mode_selected_during_setup(self):
        agent = next(a for a in self.catalog['agents'] if a['cli'] == 'claude')
        source = self.base / '.env'; source.write_text('ANTHROPIC_API_KEY=fixture-selected-api')
        with patch('builtins.input', side_effect=['api', '', '', '']), \
                patch('benchmark.cli_setup.getpass.getpass', side_effect=AssertionError('Unnecessary secret prompt')):
            setup(self.source, self.out, [agent['id']], interactive=True, import_path=source)
        self.assertEqual(read(self.out)['agents'][0]['auth'], 'api')
        self.assertEqual(read(credentials_path(self.out)), {'ANTHROPIC_API_KEY': 'fixture-selected-api'})

    def test_import_never_executes_shell_or_overwrites_other_credentials(self):
        agent = next(a for a in self.catalog['agents'] if a['cli'] == 'dsh')
        source = self.base / '.env'
        for invalid in ('$(touch marker)', '${ANOTHER_KEY}', '`echo secret`'):
            source.write_text('DEEPSEEK_API_KEY=' + invalid)
            with self.assertRaisesRegex(ValueError, 'literal'):
                import_credentials(source, [agent])
        source.write_text('DEEPSEEK_API_KEY=second')
        save_credentials(credentials_path(self.out), {'DEEPSEEK_API_KEY': 'first'})
        with self.assertRaisesRegex(ValueError, 'differ from saved'):
            setup(self.source, self.out, [agent['id']], interactive=False, import_path=source)
        self.assertEqual(read(credentials_path(self.out)), {'DEEPSEEK_API_KEY': 'first'})
        self.assertFalse(self.out.exists())
        self.assertFalse((self.base / 'marker').exists())

    def test_frozen_setup_preserves_recipe_and_only_changes_local_binary(self):
        source = dict(self.catalog, agents=[next(a for a in self.catalog['agents'] if a['cli'] == 'dsh')])
        source['agents'][0]['cli_version'] = 'pinned'
        write(self.source, source)
        setup(self.source, self.out, interactive=False, frozen=True)
        self.assertEqual(read(self.out), source)
        before = self.source.read_bytes()
        setup(self.source, self.out, interactive=False, frozen=True)
        self.assertEqual(self.source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
