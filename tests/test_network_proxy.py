"""Exercise CONNECT chaining and keep Fake-IP exceptions limited to provider names."""
import socket
import socketserver
import threading
import unittest
import json
import ssl
from unittest.mock import patch
from benchmark.network import (ConnectGate, cloudflare_addresses, configured_proxy,
                               dns_json_addresses, inspect_upstreams, target_addresses)


def address(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, 443))]


class ProxyTests(unittest.TestCase):
    def test_reserved_invalid_probe_never_waits_for_dns_or_doh(self):
        with patch('benchmark.network.socket.getaddrinfo') as dns, \
             patch('benchmark.network.cloudflare_addresses') as doh:
            for host in ('invalid', 'not-allowed.invalid', 'NOT-ALLOWED.INVALID.'):
                with self.assertRaises(ValueError):
                    target_addresses(host, [host], {'host': '127.0.0.1', 'port': 8080}, public_https=True)
            dns.assert_not_called()
            doh.assert_not_called()

    def test_invalid_proxy_does_not_silently_connect_directly_or_leak_credentials(self):
        for value in ('socks5://localhost:1080', 'http://user:secret@localhost:1080', 'http://localhost:bad'):
            with patch('benchmark.network.getproxies', return_value={'https': value}):
                with self.assertRaises(ValueError) as error:
                    configured_proxy()
                self.assertNotIn('secret', str(error.exception))
                self.assertEqual(inspect_upstreams(['api.example'])[0]['status'], 'proxy_configuration_failed')

    def test_fake_ip_requires_named_provider_and_configured_proxy(self):
        proxy = {'host': '127.0.0.1', 'port': 8080}
        with patch('benchmark.network.socket.getaddrinfo', return_value=address('198.18.0.5')):
            self.assertIsNone(target_addresses('api.example', ['api.example'], proxy))
            for host, hosts, upstream in [('api.example', ['api.example'], None),
                    ('untrusted.example', ['api.example'], proxy),
                    ('198.18.0.5', ['198.18.0.5'], proxy), ('api.local', ['api.local'], proxy)]:
                with self.subTest(host=host, proxy=upstream), self.assertRaises(ValueError):
                    target_addresses(host, hosts, upstream)
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254'):
            with patch('benchmark.network.socket.getaddrinfo', return_value=address(ip)):
                with self.assertRaises(ValueError):
                    target_addresses('api.example', ['api.example'], proxy)
        with patch('benchmark.network.socket.getaddrinfo', return_value=address('198.18.0.5')+address('10.0.0.1')):
            with self.assertRaises(ValueError):
                target_addresses('api.example', ['api.example'], proxy)

    def test_preflight_recognizes_provider_proxy_route(self):
        with patch('benchmark.network.getproxies', return_value={'https': 'http://127.0.0.1:8080'}), \
             patch('benchmark.network.socket.getaddrinfo', return_value=address('198.18.0.5')):
            report = inspect_upstreams(['api.example'])[0]
            self.assertEqual(report['status'], 'configured_proxy_fake_ip')
            self.assertIsNone(report['issue'])

    def tunnel(self, ip, response=b'HTTP/1.1 200 OK\r\n\r\n', hosts=('api.example',)):
        seen = []
        class Proxy(socketserver.BaseRequestHandler):
            def handle(self):
                data = bytearray()
                self.request.settimeout(2)
                while not data.endswith(b'\r\n\r\n'):
                    data.extend(self.request.recv(1))
                seen.append(bytes(data))
                self.request.sendall(response)
                if b'200' in response:
                    self.request.sendall(self.request.recv(4))
        server = socketserver.TCPServer(('127.0.0.1', 0), Proxy)
        server.timeout = 2
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        original_dns = socket.getaddrinfo
        def dns(host, port, *args, **kwargs):
            return address(ip) if host == 'api.example' else original_dns(host, port, *args, **kwargs)
        try:
            with patch('benchmark.network.getproxies', return_value={'https': 'http://127.0.0.1:'+str(server.server_address[1])}), \
                 patch('benchmark.network.socket.getaddrinfo', side_effect=dns):
                gate = ConnectGate(hosts, public_https=True)
                try:
                    with socket.create_connection(('127.0.0.1',gate.port), timeout=2) as client:
                        client.sendall(b'CONNECT api.example:443 HTTP/1.1\r\nProxy-Authorization: must-not-forward\r\n\r\n')
                        data = bytearray()
                        while not data.endswith(b'\r\n\r\n'):
                            data.extend(client.recv(1))
                        if b'200' in response:
                            self.assertIn(b'200', data)
                            client.sendall(b'ping')
                            self.assertEqual(client.recv(4), b'ping')
                        else:
                            self.assertIn(b'502', data)
                finally:
                    gate.close()
                self.assertFalse(gate.thread.is_alive())
        finally:
            thread.join(3)
            server.server_close()
        self.assertFalse(thread.is_alive())
        self.assertNotIn(b'must-not-forward', seen[0])
        return seen[0]

    def test_fake_provider_name_tunnels_and_relays_through_selected_proxy(self):
        self.assertTrue(self.tunnel('198.18.0.5').startswith(b'CONNECT api.example:443 '))

    def test_public_address_is_pinned_in_proxy_connect(self):
        self.assertTrue(self.tunnel('1.1.1.1').startswith(b'CONNECT 1.1.1.1:443 '))

    def test_proxy_failure_is_not_a_direct_fallback(self):
        self.tunnel('198.18.0.5', b'HTTP/1.1 407 Proxy Authentication Required\r\n\r\n')

    def test_fake_web_connect_is_pinned_to_verified_doh_ip(self):
        with patch('benchmark.network.cloudflare_addresses', return_value=address('1.1.1.1')) as resolve:
            header = self.tunnel('198.18.0.5', hosts=())
        self.assertTrue(header.startswith(b'CONNECT 1.1.1.1:443 '))
        self.assertEqual(resolve.call_args.args[0], 'api.example')


def dns_reply(host='web.example', record_type=1, records=None):
    return {'Status': 0, 'TC': False, 'Question': [{'name': host + '.', 'type': record_type}],
            'Answer': records if records is not None else [
                {'name': host + '.', 'type': record_type,
                 'data': '1.1.1.1' if record_type == 1 else '2606:4700:4700::1111'}]}


class PublicDnsTests(unittest.TestCase):
    def test_cname_chain_ignores_unrelated_public_addresses(self):
        value = dns_reply(records=[{'name': 'cdn.example.', 'type': 1, 'data': '1.1.1.1'},
            {'name': 'web.example.', 'type': 5, 'data': 'cdn.example.'},
            {'name': 'unrelated.example.', 'type': 1, 'data': '8.8.8.8'}])
        self.assertEqual(list(map(str, dns_json_addresses(value, 'web.example', 1))), ['1.1.1.1'])

    def test_private_reserved_mixed_and_wrong_dns_answers_fail_closed(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '198.18.0.5', '224.0.0.1', '::1'):
            value = dns_reply()
            value['Answer'].append({'name': 'web.example.', 'type': 28 if ':' in ip else 1, 'data': ip})
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                dns_json_addresses(value, 'web.example', 1)
        for value in (dns_reply('other.example'), dns_reply(record_type=28),
                      dict(dns_reply(), TC=True), dict(dns_reply(), Status=2),
                      dict(dns_reply(), Answer='malformed'), {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dns_json_addresses(value, 'web.example', 1)

    def test_doh_only_for_public_web_fake_names_not_private_or_literals(self):
        proxy = {'host': '127.0.0.1', 'port': 8080}
        with patch('benchmark.network.cloudflare_addresses', return_value=address('1.1.1.1')) as resolve:
            with patch('benchmark.network.socket.getaddrinfo', return_value=address('198.18.0.5')):
                self.assertEqual(target_addresses('web.example', (), proxy, public_https=True), address('1.1.1.1'))
                resolve.assert_called_once_with('web.example', proxy)
                resolve.reset_mock()
                for host, enabled in [('web.example', False), ('198.18.0.5', True), ('host.local', True)]:
                    with self.assertRaises(ValueError):
                        target_addresses(host, (), proxy, public_https=enabled)
                resolve.assert_not_called()
            for ips in (address('127.0.0.1'), address('198.18.0.5') + address('10.0.0.1')):
                with patch('benchmark.network.socket.getaddrinfo', return_value=ips), self.assertRaises(ValueError):
                    target_addresses('web.example', (), proxy, public_https=True)
            resolve.assert_not_called()

    def test_doh_uses_tls_fixed_resolver_no_auth_and_closes_connections(self):
        with patch('benchmark.network.http.client.HTTPSConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.side_effect = [json.dumps(dns_reply()).encode(), json.dumps(dns_reply(record_type=28)).encode()]
            result = cloudflare_addresses('web.example', {'host': '127.0.0.1', 'port': 8080})
        self.assertEqual([r[4][0] for r in result], ['1.1.1.1', '2606:4700:4700::1111'])
        context = factory.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.return_value.close.call_count, 2)
        for call in factory.return_value.set_tunnel.call_args_list:
            self.assertEqual(call.args, ('1.1.1.1', 443))
        for call in factory.return_value.request.call_args_list:
            self.assertEqual(call.kwargs['headers'], {'Accept': 'application/dns-json'})
            self.assertTrue(call.args[1].startswith('/dns-query?name=web.example&type='))

    def test_doh_errors_and_redirects_do_not_fall_back_or_return_names(self):
        for status, body in ((302, b'{}'), (500, b'{}'), (200, b'x' * 65537), (200, b'{}')):
            with patch('benchmark.network.http.client.HTTPSConnection') as factory:
                response = factory.return_value.getresponse.return_value
                response.status, response.read.return_value = status, body
                with self.assertRaises(ValueError):
                    cloudflare_addresses('web.example', {'host': '127.0.0.1', 'port': 8080})
                factory.return_value.close.assert_called_once()
        with patch('benchmark.network.socket.getaddrinfo', return_value=address('198.18.0.5')), \
             patch('benchmark.network.cloudflare_addresses', side_effect=OSError('resolver down')):
            with self.assertRaises(OSError):
                target_addresses('web.example', (), {'host': '127.0.0.1', 'port': 8080}, public_https=True)
