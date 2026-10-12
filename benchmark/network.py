"""Per-attempt HTTPS CONNECT gate. No TLS interception, request bodies or tokens logged."""
import ipaddress
import http.client
import json
import select
import socket
import socketserver
import ssl
import threading
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import getproxies


def dns_json_addresses(payload, host, record_type):
    """Accept only this question's address/CNAME chain and public A/AAAA records."""
    try:
        question = payload['Question']
        if (type(payload['Status']) is not int or payload['Status'] != 0 or payload.get('TC') or
                len(question) != 1 or question[0]['name'].lower().rstrip('.') != host.lower() or
                question[0]['type'] != record_type):
            raise ValueError
        answers = payload.get('Answer', [])
        if not isinstance(answers, list) or len(answers) > 64:
            raise ValueError
        names = {host.lower()}
        for _ in range(len(answers)):
            for item in answers:
                if item['type'] == 5 and item['name'].lower().rstrip('.') in names:
                    names.add(item['data'].lower().rstrip('.'))
        ips = []
        for item in answers:
            if item['type'] not in (1, 28):
                continue
            ip = ipaddress.ip_address(item['data'])
            if (not ip.is_global or ip.is_multicast or ip.is_reserved or
                    ip.version != (4 if item['type'] == 1 else 6)):
                raise ValueError
            if item['type'] == record_type and item['name'].lower().rstrip('.') in names:
                ips.append(ip)
        return ips
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('Invalid or non-public DNS-over-HTTPS response') from None


def cloudflare_addresses(host, proxy):
    """Resolve Fake-IP web names through the existing proxy; no credentials or redirects.

    Pin the resolver itself to 1.1.1.1 so local Fake-IP DNS cannot affect bootstrap.
    HTTPSConnection verifies its IP certificate after CONNECT, using the system CA.
    """
    ips = []
    for record_type in (1, 28):
        connection = http.client.HTTPSConnection(proxy['host'], proxy['port'], timeout=5,
            context=ssl.create_default_context(cafile='/private/etc/ssl/cert.pem'))
        connection.set_tunnel('1.1.1.1', 443)
        try:
            connection.request('GET', '/dns-query?' + urlencode({'name': host, 'type': record_type}),
                               headers={'Accept': 'application/dns-json'})
            response = connection.getresponse()
            data = response.read(65537)
            if response.status != 200 or len(data) > 65536:
                raise ValueError('DNS-over-HTTPS request failed')
            ips.extend(dns_json_addresses(json.loads(data), host, record_type))
        except (http.client.HTTPException, UnicodeError):
            raise ValueError('DNS-over-HTTPS request failed') from None
        finally:
            connection.close()
    if not ips:
        raise ValueError('No public DNS-over-HTTPS addresses')
    return [(socket.AF_INET if ip.version == 4 else socket.AF_INET6, socket.SOCK_STREAM, 0, '',
             (str(ip), 443) if ip.version == 4 else (str(ip), 443, 0, 0)) for ip in ips]


def configured_proxy():
    """Read host HTTPS proxy configuration once, outside the candidate environment."""
    value = getproxies().get('https')
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'http' or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port or 80
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('Configured HTTPS proxy must be an unauthenticated HTTP CONNECT proxy') from None
    return {'host': parsed.hostname, 'port': port}


def target_addresses(host, hosts=(), proxy=None, *, public_https=False):
    """Pin public DNS results; only exact provider names may use proxy Fake-IP resolution."""
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', host) or
            host.lower().rstrip('.').endswith(('.local', '.localhost', '.internal', '.invalid')) or
            host.lower().rstrip('.') in ('localhost', 'invalid')):
        raise ValueError('Invalid CONNECT host')
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if ips and all(ip.is_global and not ip.is_multicast and not ip.is_reserved for ip in ips):
        return addresses
    # No blanket exception for 198.18/15: never tunnel arbitrary names or IP literals.
    fake = ipaddress.ip_network('198.18.0.0/15')
    named = bool(re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', host))
    try:
        ipaddress.ip_address(host)
        named = False
    except ValueError:
        pass
    if proxy and named and ips and all(ip.version == 4 and ip in fake for ip in ips):
        if host.lower() in hosts:
            return None  # Only the trusted host proxy resolves this exact provider name.
        if public_https:
            return cloudflare_addresses(host, proxy)
    raise ValueError('Non-public upstream address')


def public_addresses(host, port=443):
    """Resolve once and connect to those IPs, rejecting local/private DNS answers."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ValueError('Non-public upstream address')
    return addresses


def inspect_upstreams(hosts):
    """DNS-only readiness check using the same policy as the execution gate."""
    reports = []
    try:
        proxy = configured_proxy()
    except ValueError as error:
        return [{'host': host, 'status': 'proxy_configuration_failed', 'issue': str(error)}
                for host in sorted(set(hosts))]
    for host in sorted(set(hosts)):
        try:
            addresses = target_addresses(host, hosts, proxy)
            status, issue = ('configured_proxy_fake_ip' if addresses is None else 'public_dns'), None
        except ValueError:
            status = 'non_public_dns'
            issue = (f'{host}: DNS resolves to a non-public address; required sandbox will reject it. '
                     'Fake-IP DNS requires a configured HTTP CONNECT HTTPS proxy and an exact provider hostname; '
                     'the benchmark does not change system networking or bypass the gate.')
        except OSError:
            status, issue = 'dns_failed', f'{host}: DNS resolution failed; check local network/DNS and rerun cli-check.'
        reports.append({'host': host, 'status': status, 'issue': issue, 'upstream_proxy': proxy})
    return reports


class ConnectGate:
    def __init__(self, hosts, *, public_https=False):
        self.hosts = frozenset(h.lower() for h in hosts)
        self.public_https = public_https
        self.upstream_proxy = configured_proxy()
        self.denied_hosts = set()
        self._connections = set()
        self._lock = threading.Lock()
        gate = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                upstream = None
                gate.track(self.request)
                try:
                    self.request.settimeout(10)
                    header = bytearray()
                    while not header.endswith(b'\r\n\r\n') and len(header) <= 16384:
                        chunk = self.request.recv(1)
                        if not chunk:
                            return
                        header.extend(chunk)
                    if not header.endswith(b'\r\n\r\n'):
                        raise ValueError('Oversized proxy header')
                    line = bytes(header).split(b'\r\n', 1)[0].decode('ascii')
                    method, target, protocol = line.split(' ')
                    host, port = target.rsplit(':', 1)
                    if (method != 'CONNECT' or protocol not in ('HTTP/1.0', 'HTTP/1.1') or
                            port != '443' or (not gate.public_https and host.lower() not in gate.hosts)):
                        # Report only approved-host failures/counts, never arbitrary user strings.
                        with gate._lock:
                            gate.denied_hosts.add(host if host in gate.hosts else '<not-allowlisted>')
                        self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                        return
                    addresses = target_addresses(host, gate.hosts, gate.upstream_proxy,
                                                 public_https=gate.public_https)
                    if gate.upstream_proxy:
                        upstream = socket.create_connection((gate.upstream_proxy['host'], gate.upstream_proxy['port']), timeout=10)
                        gate.track(upstream)
                        # Pin public destinations to the checked IP; Fake-IP provider names use proxy DNS.
                        destination = host if addresses is None else addresses[0][4][0]
                        authority = ('[' + destination + ']' if ':' in destination else destination) + ':443'
                        upstream.sendall(('CONNECT ' + authority + ' HTTP/1.1\r\nHost: ' + authority + '\r\n\r\n').encode('ascii'))
                        response = bytearray()
                        while not response.endswith(b'\r\n\r\n') and len(response) <= 16384:
                            chunk = upstream.recv(1)
                            if not chunk:
                                raise OSError('Proxy closed CONNECT')
                            response.extend(chunk)
                        first = bytes(response).split(b'\r\n', 1)[0].split()
                        if not response.endswith(b'\r\n\r\n') or len(first) < 2 or first[1] != b'200':
                            raise OSError('Configured proxy rejected CONNECT')
                    else:
                        for family, kind, proto, _, address in addresses:
                            upstream = socket.socket(family, kind, proto)
                            upstream.settimeout(10)
                            try:
                                upstream.connect(address)
                                break
                            except OSError:
                                upstream.close()
                                upstream = None
                    if upstream is None:
                        raise OSError('Upstream unavailable')
                    gate.track(upstream)
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    while True:
                        ready, _, _ = select.select([self.request, upstream], [], [], 1)
                        if gate.closed:
                            return
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            (upstream if source is self.request else self.request).sendall(data)
                except (OSError, ValueError, UnicodeError):
                    with gate._lock:
                        gate.denied_hosts.add('<unreachable-or-nonpublic>')
                    try:
                        self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
                    except OSError:
                        pass
                finally:
                    gate.untrack(self.request)
                    if upstream is not None:
                        gate.untrack(upstream)
                        upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.closed = False
        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def track(self, connection):
        with self._lock:
            self._connections.add(connection)

    def untrack(self, connection):
        with self._lock:
            self._connections.discard(connection)

    def close(self):
        self.closed = True
        self.server.shutdown()
        with self._lock:
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server.server_close()
        self.thread.join(timeout=1)
