import json
import os
import shutil
import ssl
import struct
import time
import subprocess
from unittest import mock
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest

from toolbox import gateway


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Fixture:
    def __init__(self, handler):
        self.server = Server(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def header(sock):
    value = b''
    while not value.endswith(b'\r\n\r\n'):
        part = sock.recv(1)
        if not part:
            break
        value += part
    return value


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(8192)
            if not data:
                return
            self.request.sendall(data)


class HttpProxy(socketserver.BaseRequestHandler):
    def handle(self):
        request = header(self.request)
        self.server.requests.append(request)
        if request.startswith(b'CONNECT '):
            self.request.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
            Echo.handle(self)
        else:
            body = self.server.label
            self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)


class SocksProxy(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.greeting = gateway.receive_exact(self.request, 3)
        if self.server.greeting != b'\x05\x01\x00':
            return
        self.request.sendall(b'\x05')
        self.request.sendall(b'\x00')
        head = gateway.receive_exact(self.request, 4)
        self.server.atyp = head[3]
        if head[3] == 3:
            size = gateway.receive_exact(self.request, 1)[0]
            self.server.target = gateway.receive_exact(self.request, size).decode()
        else:
            size = 4 if head[3] == 1 else 16
            self.server.target = socket.inet_ntop(socket.AF_INET if head[3] == 1 else socket.AF_INET6, gateway.receive_exact(self.request, size))
        gateway.receive_exact(self.request, 2)
        for part in (b'\x05\x00\x00\x03', b'\x01x', b'\x00\x00'):
            self.request.sendall(part)
        self.forward()

    def forward(self):
        Echo.handle(self)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: gateway.stop(self.home))

    def fixture(self, handler=HttpProxy, label=b'first'):
        fixture = Fixture(handler)
        fixture.server.requests = []
        fixture.server.label = label
        self.addCleanup(fixture.close)
        return fixture

    def install(self, upstream):
        plan = gateway.prepare(self.home, upstream)
        for path, data in plan['files'].items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        gateway.ensure(self.home)
        return plan

    def test_http_connect_and_fragmented_socks_remote_dns(self):
        for protocol, handler in [('http', HttpProxy), ('socks5', SocksProxy)]:
            proxy = self.fixture(handler)
            with gateway.open_tunnel(dict(host='127.0.0.1', port=proxy.port, protocol=protocol), 'remote.invalid', 443) as sock:
                sock.sendall(b'nonce')
                self.assertEqual(sock.recv(5), b'nonce')
            if protocol == 'socks5':
                self.assertEqual(proxy.server.atyp, 3)
                self.assertEqual(proxy.server.target, 'remote.invalid')

    def test_plain_http_uses_absolute_form_and_switch_keeps_stable_url(self):
        one, two = self.fixture(label=b'first'), self.fixture(label=b'second')
        first = self.install(dict(host='127.0.0.1', port=one.port, protocol='http'))
        def fetch():
            port = int(first['url'].rsplit(':', 1)[1])
            with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
                sock.sendall(b'GET http://example.invalid/hello?q=1 HTTP/1.1\r\nHost: example.invalid\r\n\r\n')
                response = b''
                while True:
                    part = sock.recv(8192)
                    if not part:
                        return response
                    response += part
        self.assertTrue(fetch().endswith(b'first'))
        second = self.install(dict(host='127.0.0.1', port=two.port, protocol='http'))
        self.assertEqual(first['url'], second['url'])
        self.assertTrue(fetch().endswith(b'second'))
        self.assertTrue(two.server.requests[0].startswith(b'GET http://example.invalid/hello?q=1 HTTP/1.1'))
        self.assertEqual(gateway.status(self.home)['counts']['example.invalid'], 2)

    def test_restart_preserves_url_and_occupied_unrelated_port_fails(self):
        proxy = self.fixture()
        plan = self.install(dict(host='127.0.0.1', port=proxy.port, protocol='http'))
        gateway.stop(self.home)
        self.assertFalse(gateway.status(self.home)['running'])
        gateway.ensure(self.home)
        self.assertEqual(gateway.status(self.home)['url'], plan['url'])
        gateway.stop(self.home)
        port = int(plan['url'].rsplit(':', 1)[1])
        with socket.socket() as occupied:
            if os.name != 'nt':
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(('127.0.0.1', port))
            occupied.listen()
            with self.assertRaisesRegex(RuntimeError, '占用|occupied'):
                gateway.ensure(self.home)

    def test_stop_finds_owned_listener_after_config_removed(self):
        proxy = self.fixture()
        plan = self.install(dict(host='127.0.0.1', port=proxy.port, protocol='http'))
        (self.home / gateway.CONFIG).unlink()
        gateway.stop(self.home)
        self.assertFalse(gateway.status(self.home)['running'])
        with socket.socket() as sock:
            if os.name != 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', int(plan['url'].rsplit(':', 1)[1])))

    def test_switch_leaves_existing_connect_tunnel_alive(self):
        one, two = self.fixture(), self.fixture()
        first = self.install(dict(host='127.0.0.1', port=one.port, protocol='http'))
        port = int(first['url'].rsplit(':', 1)[1])
        with gateway.open_tunnel(dict(host='127.0.0.1', port=port, protocol='http'), 'persist.invalid', 443) as tunnel:
            self.install(dict(host='127.0.0.1', port=two.port, protocol='http'))
            tunnel.sendall(b'existing connection')
            self.assertEqual(tunnel.recv(19), b'existing connection')
            with gateway.open_tunnel(dict(host='127.0.0.1', port=port, protocol='http'), 'new.invalid', 443) as fresh:
                fresh.sendall(b'new')
                self.assertEqual(fresh.recv(3), b'new')
        self.assertIn(b'persist.invalid', one.server.requests[0])
        self.assertIn(b'new.invalid', two.server.requests[0])

    def test_socks_plain_http_uses_origin_form(self):
        class SocksHttp(SocksProxy):
            def forward(self):
                self.server.requests.append(header(self.request))
                self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        upstream = self.fixture(SocksHttp)
        plan = self.install(dict(host='127.0.0.1', port=upstream.port, protocol='socks5'))
        with socket.create_connection(('127.0.0.1', int(plan['url'].rsplit(':', 1)[1])), timeout=3) as sock:
            sock.sendall(b'GET http://remote.invalid/path?q=1 HTTP/1.1\r\nHost: remote.invalid\r\n\r\n')
            self.assertIn(b'200', header(sock))
            self.assertEqual(gateway.receive_exact(sock, 2), b'ok')
        self.assertTrue(upstream.server.requests[0].startswith(b'GET /path?q=1 HTTP/1.1'))

    @unittest.skipUnless(shutil.which('openssl'), 'Requires openssl for a temporary localhost test certificate')
    def test_https_upstream_validates_certificate_and_accepts_trusted_local_ca(self):
        cert, key = self.home / 'cert.pem', self.home / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                        '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        class TlsProxy(HttpProxy):
            def handle(self):
                try:
                    self.request = context.wrap_socket(self.request, server_side=True)
                except ssl.SSLError:
                    return
                try:
                    super().handle()
                finally:
                    self.request.close()
        proxy = self.fixture(TlsProxy)
        upstream = dict(host='localhost', port=proxy.port, protocol='https')
        with self.assertRaises(ssl.SSLCertVerificationError):
            gateway.open_tunnel(upstream, 'remote.invalid', 443)
        trusted = ssl.create_default_context(cafile=str(cert))
        with mock.patch.object(ssl, 'create_default_context', return_value=trusted):
            with gateway.open_tunnel(upstream, 'remote.invalid', 443) as sock:
                sock.sendall(b'tls nonce')
                self.assertEqual(sock.recv(9), b'tls nonce')
        wrong_host = dict(upstream, host='127.0.0.1')
        with mock.patch.object(ssl, 'create_default_context', return_value=trusted):
            with self.assertRaises(ssl.SSLCertVerificationError):
                gateway.open_tunnel(wrong_host, 'remote.invalid', 443)

        from toolbox import wsl_agent
        class Origin(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with context.wrap_socket(self.request, server_side=True) as stream:
                        self.server.requests.append(header(stream))
                        stream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
                except OSError:
                    pass
        origin = self.fixture(Origin)
        class ForwardTlsProxy(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with context.wrap_socket(self.request, server_side=True) as stream:
                        self.server.requests.append(header(stream))
                        with socket.create_connection(('127.0.0.1', origin.port), timeout=3) as remote:
                            stream.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
                            gateway.relay(stream, remote)
                except OSError:
                    pass
        forward = self.fixture(ForwardTlsProxy)
        with mock.patch.object(ssl, 'create_default_context', return_value=trusted):
            with gateway.open_tunnel(dict(upstream, port=forward.port), 'localhost', origin.port) as stream:
                wsl_agent.tls_get(stream, 'localhost', timeout=3)
        self.assertTrue(origin.server.requests[0].startswith(b'GET / HTTP/1.1'))
        with mock.patch.object(ssl, 'create_default_context', return_value=trusted):
            with gateway.open_tunnel(dict(upstream, port=forward.port), 'localhost', origin.port) as stream:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    wsl_agent.tls_get(stream, 'wrong.invalid', timeout=3)

    def test_partial_http_response_reset_does_not_inject_502_into_body(self):
        expected = b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\npartial'
        class ResetProxy(socketserver.BaseRequestHandler):
            def handle(self):
                header(self.request)
                self.request.sendall(expected)
                time.sleep(.05)
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('hh' if os.name == 'nt' else 'ii', 1, 0))
                self.request.close()
        proxy = self.fixture(ResetProxy)
        plan = self.install(dict(host='127.0.0.1', port=proxy.port, protocol='http'))
        with socket.create_connection(('127.0.0.1', int(plan['url'].rsplit(':', 1)[1])), timeout=3) as sock:
            sock.sendall(b'GET http://remote.invalid/ HTTP/1.1\r\nHost: remote.invalid\r\n\r\n')
            response = b''
            while True:
                part = sock.recv(8192)
                if not part:
                    break
                response += part
        self.assertEqual(response, expected)

    def test_ensure_rechecks_owned_listener_after_stale_not_running_snapshot(self):
        proxy = self.fixture()
        self.install(dict(host='127.0.0.1', port=proxy.port, protocol='http'))
        current = gateway.status(self.home)
        original_status = gateway.status
        calls = []
        def stale_once(home):
            calls.append(home)
            return dict(current, running=False) if len(calls) == 1 else original_status(home)
        with mock.patch.object(gateway, 'status', side_effect=stale_once):
            self.assertTrue(gateway.ensure(self.home)['running'])

    def test_socks_authentication_rejected(self):
        class Auth(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(3)
                self.request.sendall(b'\x05\x02')
        proxy = self.fixture(Auth)
        with self.assertRaisesRegex(OSError, '认证|authentication'):
            gateway.open_tunnel(dict(host='127.0.0.1', port=proxy.port, protocol='socks5'), 'example.invalid', 443)


if __name__ == '__main__':
    unittest.main()
