from contextlib import contextmanager
import socket
import threading
import time
import unittest
from unittest import mock

from toolbox import proxy
from toolbox.wsl_agent import probe_http, https_test


def candidate(port=18567, address='127.0.0.1', pid=123, app='SakuraCat'):
    return dict(app=app, host=address, address=address, port=port, pid=pid,
                created='2026-01-01T00:00:00Z', process='sakuracat.exe', path='C:/Apps/SakuraCat/sakuracat.exe')


class SelectionTests(unittest.TestCase):
    def test_unique_candidate_selects_without_system_proxy(self):
        row = candidate()
        selected, _ = proxy.select_candidate([row], {'enabled': False})
        self.assertEqual(selected, row)

    def test_system_proxy_matches_endpoint_not_port_only(self):
        a, b = candidate(), candidate(address='127.0.0.2', pid=456)
        selected, _ = proxy.select_candidate([a, b], {'enabled': True, 'server': '127.0.0.2:18567'})
        self.assertEqual(selected, b)

    def test_multiple_candidates_and_pac_require_selection(self):
        rows = [candidate(), candidate(port=21567)]
        self.assertIsNone(proxy.select_candidate(rows, {'enabled': False})[0])
        self.assertIsNone(proxy.select_candidate(rows[:1], {'enabled': True, 'pac': True})[0])

    def test_owner_identity_includes_address_path_and_start_time(self):
        row = candidate()
        for key, value in [('address', '127.0.0.2'), ('created', 'later'), ('path', 'C:/other.exe')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                proxy.ensure_owner(row, {'listeners': [dict(row, **{key: value})]})

    def test_ambiguous_connect_never_writes(self):
        rows = [candidate(), candidate(port=21567)]
        with mock.patch.object(proxy, 'scan', return_value=dict(candidates=rows, selected=None, reason='choose', issues=[])):
            with self.assertRaises(proxy.SelectionRequired) as error:
                proxy.connect('Example', 'testuser')
        self.assertEqual(error.exception.candidates, rows)
        self.assertEqual(error.exception.reason, 'choose')

    def test_empty_user_is_rejected_before_wsl_invocation(self):
        with self.assertRaisesRegex(ValueError, '用户'):
            proxy.connect('Example', '')

    def test_process_name_alone_does_not_identify_unknown_mihomo(self):
        self.assertIsNone(proxy.application(dict(process='mihomo.exe', path='C:/other/mihomo.exe')))
        self.assertEqual(proxy.application(dict(process='mihomo.exe', path='C:/Apps/Clash Verge/mihomo.exe')), 'Clash Verge')

    def test_known_core_with_hidden_path_keeps_pid_and_creation_identity(self):
        row = dict(candidate(), process='com.vortex.helper.exe', path='', address='::')
        self.assertEqual(proxy.application(row), 'SakuraCat')
        proxy.ensure_owner(row, {'listeners': [row]})
        with self.assertRaises(RuntimeError):
            proxy.ensure_owner(row, {'listeners': [dict(row, created='restarted')]})

    def test_dualstack_binding_conflicts_with_other_ipv4_owner(self):
        row = dict(candidate(), address='::')
        with self.assertRaises(RuntimeError):
            proxy.ensure_owner(row, {'listeners': [row, candidate(address='127.0.0.1', pid=456)]})

    def test_listener_disappearing_after_probe_prevents_configuration(self):
        row = candidate()
        inventory = {'listeners': [row]}
        with mock.patch.object(proxy, 'windows_inventory', side_effect=[inventory, {'listeners': []}]), \
             mock.patch.object(proxy, '_probe', return_value=([dict(row, verified=True)], [])):
            with self.assertRaisesRegex(RuntimeError, '变化'):
                proxy.connect('Example', 'testuser', row)

    def test_manual_candidate_resolves_real_owner_before_apply(self):
        row = candidate()
        inventory = {'listeners': [row]}
        def agent(distro, user, request, timeout=60):
            if request['action'] == 'probe':
                return {'results': [dict(request['candidates'][0], host='127.0.0.1', verified=True)]}
            if request['url'] != 'http://127.0.0.1:18567':
                raise AssertionError('incorrect effective endpoint')
            return {'message': 'applied'}
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=agent):
            result = proxy.connect('Example', 'testuser', {'app': '手动', 'host': '127.0.0.1', 'port': 18567})
        self.assertEqual(result['candidate']['pid'], 123)
        self.assertEqual(result['message'], 'applied')


class ProtocolTests(unittest.TestCase):
    @contextmanager
    def server(self, kind):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        port = listener.getsockname()[1]
        def serve():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(1)
                    header = b''
                    while not header.endswith(b'\r\n\r\n'):
                        part = conn.recv(1)
                        if not part:
                            return
                        header += part
                    if kind == 'http':
                        for data in (b'HT', b'TP/1.1 200 OK\r\n', b'\r\n'):
                            conn.sendall(data)
                        token = b''
                        while not token.endswith(b'\n'):
                            token += conn.recv(256)
                        conn.sendall(token[:3])
                        conn.sendall(token[3:])
                    elif kind == 'auth':
                        conn.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\n\r\n')
                    elif kind == 'wrong':
                        conn.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
                        conn.recv(256)
                        conn.sendall(b'wrong\n')
                    elif kind == 'large':
                        conn.sendall(b'HTTP/1.1 200 OK\r\nX: ' + b'x' * 8300)
                    elif kind == 'timeout':
                        time.sleep(.3)
                    elif kind == 'tls_timeout':
                        conn.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
                        time.sleep(.3)
                    else:
                        conn.sendall(b'HTTP/1.1 405 Method Not Allowed\r\n\r\n')
            except OSError:
                pass
            finally:
                listener.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield port
        finally:
            listener.close()
            thread.join(2)

    def test_fragmented_connect_and_random_echo(self):
        with self.server('http') as port:
            self.assertTrue(probe_http('127.0.0.1', port, 30000)[0])

    def test_control_auth_wrong_echo_and_large_header_fail(self):
        for kind in ('auth', 'wrong', 'large', 'control'):
            with self.subTest(kind=kind), self.server(kind) as port:
                self.assertFalse(probe_http('127.0.0.1', port, 30000, timeout=.3)[0])

    def test_connect_probe_has_overall_deadline(self):
        with self.server('timeout') as port:
            started = time.monotonic()
            self.assertFalse(probe_http('127.0.0.1', port, 30000, timeout=.05)[0])
            self.assertLess(time.monotonic() - started, .25)

    def test_external_tls_diagnostic_has_overall_deadline(self):
        with self.server('tls_timeout') as port:
            started = time.monotonic()
            result = https_test(f'http://127.0.0.1:{port}', timeout=.05)
            self.assertIn('未通过', result)
            self.assertLess(time.monotonic() - started, .25)


if __name__ == '__main__':
    unittest.main()
