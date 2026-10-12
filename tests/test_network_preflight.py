"""Readiness diagnostics must use the execution gate's DNS rules, without inference."""
import socket
import unittest
from unittest.mock import patch
from benchmark.network import inspect_upstreams
from benchmark.cli_adapters import check_agent


class NetworkPreflightTests(unittest.TestCase):
    def setUp(self):
        proxy = patch('benchmark.network.getproxies', return_value={})
        proxy.start()
        self.addCleanup(proxy.stop)

    def test_public_nonpublic_and_fake_ip_dns(self):
        for address, expected in [('1.1.1.1', 'public_dns'), ('198.18.0.66', 'non_public_dns'),
                                  ('127.0.0.1', 'non_public_dns'), ('10.0.0.1', 'non_public_dns'),
                                  ('::1', 'non_public_dns')]:
            with self.subTest(address=address), patch('benchmark.network.socket.getaddrinfo',
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, '', (address, 443))]):
                report = inspect_upstreams(['api.example'])[0]
                self.assertEqual(report['status'], expected)
                if expected != 'public_dns':
                    self.assertIn('Fake-IP', report['issue'])

    def test_explicit_readiness_includes_dns_without_calling_cli_or_model(self):
        agent = {'id': 'fixture', 'cli': 'dsh', 'auth': 'api', 'binary': 'missing-cli',
                 'cli_version': None, 'api_key_env': 'FIXTURE_KEY', 'base_url': 'https://api.example'}
        with patch('benchmark.cli_adapters.shutil.which', return_value=None), \
             patch('benchmark.native_auth.auth_issues', return_value=[]), \
             patch('benchmark.network.socket.getaddrinfo', return_value=[
                 (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('198.18.0.66', 443))]) as dns:
            local = check_agent(agent, '.')
            dns.assert_not_called()
            report = check_agent(agent, '.', check_network=True)
        self.assertNotIn('network', local)
        self.assertEqual(report['network'][0]['status'], 'non_public_dns')
        self.assertTrue(any('Fake-IP' in item for item in report['issues']))
        self.assertEqual(report['model_access'], 'not_checked')

    def test_dns_failure_is_actionable_and_does_not_disclose_exception(self):
        with patch('benchmark.network.socket.getaddrinfo', side_effect=OSError('private diagnostic')):
            report = inspect_upstreams(['api.example'])[0]
        self.assertEqual(report['status'], 'dns_failed')
        self.assertIn('rerun cli-check', report['issue'])
        self.assertNotIn('private diagnostic', str(report))


if __name__ == '__main__':
    unittest.main()
