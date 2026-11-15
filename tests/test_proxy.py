from contextlib import contextmanager
import socket
import ssl
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

    def test_process_names_are_labels_not_an_allowlist(self):
        self.assertEqual(proxy.application(dict(process='mihomo.exe', path='C:/other/mihomo.exe')), 'mihomo')
        self.assertEqual(proxy.application(dict(process='unknown-client.exe')), 'unknown-client')
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
            if request.get('upstream') != dict(host='127.0.0.1', port=18567, protocol='auto'):
                raise AssertionError('incorrect effective endpoint')
            return {'message': 'applied'}
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=agent):
            result = proxy.connect('Example', 'testuser', {'app': '手动', 'host': '127.0.0.1', 'port': 18567})
        self.assertEqual(result['candidate']['pid'], 123)
        self.assertEqual(result['message'], 'applied')


class UniversalDiscoveryTests(unittest.TestCase):
    def inventory(self, rows, **setting):
        return dict(listeners=rows, system_proxy=setting)

    def probe(self, distro, user, request, timeout=60):
        return dict(results=[dict(row, verified=True,
                                 protocol='http' if row['protocol'] == 'auto' else row['protocol'])
                             for row in request['candidates']], partial=False)

    def test_separate_system_endpoints_keep_transport_and_unknown_process(self):
        rows = [dict(candidate(port=18001), process='mystery.exe'), candidate(port=18002)]
        inventory = self.inventory(rows, enabled=True,
                                   server='http=127.0.0.1:18001;https=127.0.0.1:18002')
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=self.probe):
            result = proxy.scan('Example', 'testuser')
        self.assertEqual({row['port'] for row in result['candidates']}, {18001, 18002})
        self.assertEqual({row['protocol'] for row in result['candidates']}, {'http'})
        self.assertTrue(all(row['source'] == 'system' for row in result['candidates']))
        self.assertIsNone(result['selected'])
        self.assertEqual(result['coverage']['unconfirmed'], 0)

    def test_literal_https_system_uri_is_tls_transport(self):
        endpoints = proxy.system_endpoints(dict(enabled=True, server='https=https://localhost:8443'))
        self.assertEqual(endpoints[0]['protocol'], 'https')

    def test_no_priority_success_automatically_checks_every_local_listener(self):
        rows = [dict(candidate(port=18000 + index), process='unknown-%d.exe' % index)
                for index in range(4)]
        inventory = self.inventory(rows, enabled=False)
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=self.probe) as agent:
            result = proxy.scan('Example', 'testuser')
        self.assertEqual(len(result['candidates']), len(rows))
        request = agent.call_args.args[2]
        self.assertEqual(len(request['candidates']), len(rows))
        self.assertFalse(request.get('allow_external', False))
        self.assertEqual(result['coverage']['scope'], 'all_listeners')

    def test_priority_success_skips_other_listeners_until_extended(self):
        rows = [candidate(port=18001), dict(candidate(port=18002), process='unknown.exe')]
        inventory = self.inventory(rows, enabled=True, server='127.0.0.1:18001')
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=self.probe):
            normal = proxy.scan('Example', 'testuser')
            extended = proxy.scan('Example', 'testuser', extended=True)
        self.assertEqual(len(normal['candidates']), 1)
        self.assertTrue(normal['coverage']['partial'])
        self.assertEqual(normal['coverage']['unconfirmed'], 1)
        self.assertEqual(len(extended['candidates']), 2)

    def test_system_selection_respects_proxy_transport(self):
        socks = dict(candidate(), protocol='socks5')
        self.assertIsNone(proxy.select_candidate([socks], dict(enabled=True, server='127.0.0.1:18567'))[0])

    def test_failed_priority_tries_unknown_listener_without_public_requests(self):
        rows = [candidate(port=18001), dict(candidate(port=18002), process='unknown.exe')]
        inventory = self.inventory(rows, enabled=True, server='127.0.0.1:18001')
        seen = []
        def probe(distro, user, request, timeout=60):
            seen.append(request)
            return dict(results=[dict(row, protocol='http', verified=row['port'] == 18002,
                                      reason='not a proxy') for row in request['candidates']])
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=probe):
            result = proxy.scan('Example', 'testuser')
        self.assertEqual([row['port'] for row in result['candidates'] if row['verified']], [18002])
        self.assertFalse(any(request['allow_external'] for request in seen))
        self.assertTrue(all(request['budget'] <= 18 and request['max_workers'] <= 8 for request in seen))

    def test_partial_probe_reports_pending_coverage(self):
        rows = [dict(candidate(port=18000 + index), process='unknown.exe') for index in range(3)]
        response = dict(results=[dict(rows[0], verified=False, reason='not proxy')],
                        coverage=dict(total=3, completed=1, pending=2))
        with mock.patch.object(proxy, 'windows_inventory', return_value=self.inventory(rows)), \
             mock.patch.object(proxy, 'run_agent', return_value=response):
            result = proxy.scan('Example', 'testuser', extended=True)
        self.assertEqual(result['coverage']['unconfirmed'], 2)
        self.assertTrue(result['coverage']['partial'])

    def test_coverage_counts_protocol_variants_and_unowned_listeners(self):
        rows = [candidate(), dict(candidate(port=18002), created='')]
        inventory = self.inventory(rows, enabled=True,
                                   server='http://127.0.0.1:18567;https://127.0.0.1:18567')
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=self.probe):
            result = proxy.scan('Example', 'testuser', extended=True)
        self.assertEqual(result['coverage']['total'], 3)
        self.assertEqual(result['coverage']['completed'], 2)
        self.assertEqual(result['coverage']['unconfirmed'], 1)

    def test_remote_manual_uri_does_not_claim_a_wildcard_listener(self):
        inventory = self.inventory([dict(candidate(), address='0.0.0.0')])
        def agent(distro, user, request, timeout=60):
            if request['action'] == 'probe':
                self.assertTrue(request['allow_external'])
                self.assertEqual(request['candidates'][0]['endpoint_kind'], 'remote')
                return self.probe(distro, user, request, timeout)
            self.assertEqual(request['upstream'], dict(host='192.0.2.40', port=18567, protocol='socks5'))
            return dict(message='applied')
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent', side_effect=agent):
            result = proxy.connect('Example', 'testuser', dict(host='socks5://192.0.2.40:18567'))
        self.assertNotIn('pid', result['candidate'])

    def test_manual_missing_loopback_listener_is_rejected(self):
        with mock.patch.object(proxy, 'windows_inventory', return_value=self.inventory([])), \
             mock.patch.object(proxy, 'run_agent') as agent:
            with self.assertRaisesRegex(RuntimeError, '监听进程'):
                proxy.connect('Example', 'testuser', dict(host='localhost', port=9000))
        agent.assert_not_called()

    def test_manual_local_interface_requires_real_listener(self):
        inventory = dict(self.inventory([]), local_addresses=['192.168.1.25'])
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory), \
             mock.patch.object(proxy, 'run_agent') as agent:
            with self.assertRaisesRegex(RuntimeError, '监听进程'):
                proxy.connect('Example', 'testuser', dict(host='192.168.1.25', port=9000))
        agent.assert_not_called()

    def test_authenticated_manual_uri_is_rejected(self):
        with mock.patch.object(proxy, 'windows_inventory', return_value=self.inventory([])), \
             mock.patch.object(proxy, 'run_agent') as agent:
            with self.assertRaises(ValueError):
                proxy.connect('Example', 'testuser', dict(host='http://user:secret@192.0.2.40:8080'))
        agent.assert_not_called()

    def test_authenticated_system_endpoint_does_not_leak_credentials(self):
        inventory = self.inventory([], enabled=True, server='http://user:secret@192.0.2.40:8080')
        with mock.patch.object(proxy, 'windows_inventory', return_value=inventory):
            result = proxy.scan('Example', 'testuser')
        self.assertNotIn('secret', repr(result))

    def test_apps_delegates_with_readonly_diagnostic_timeout(self):
        with mock.patch.object(proxy, 'run_agent', return_value={'tools': []}) as agent:
            self.assertEqual(proxy.check_apps('Example', 'testuser'), {'tools': []})
        self.assertEqual(agent.call_args.args[2], dict(action='apps'))
        self.assertEqual(agent.call_args.kwargs['timeout'], 150)

    def test_manual_protocol_survives_owner_resolution_and_gateway_apply(self):
        row = candidate()
        with mock.patch.object(proxy, 'windows_inventory', return_value=self.inventory([row])), \
             mock.patch.object(proxy, 'run_agent', side_effect=lambda d, u, req, timeout=60:
                               self.probe(d, u, req, timeout) if req['action'] == 'probe' else
                               dict(local_url='http://127.0.0.1:34567', network={'ok': True})) as agent:
            result = proxy.connect('Example', 'testuser', dict(host='127.0.0.1', port=row['port'], protocol='socks5'))
        self.assertEqual(result['candidate']['protocol'], 'socks5')
        self.assertEqual(agent.call_args.args[2]['action'], 'connect_gateway')
        self.assertEqual(agent.call_args.args[2]['upstream']['protocol'], 'socks5')
        self.assertEqual(result['local_url'], 'http://127.0.0.1:34567')

    def test_diagnose_uses_current_gateway_without_discovery(self):
        def agent(distro, user, request, timeout=60):
            if request['action'] == 'status':
                return dict(message='configured', configured=True, url='http://127.0.0.1:34567')
            if request['action'] == 'gateway_status':
                return dict(running=True, url='http://127.0.0.1:34567')
            self.assertEqual(request['url'], 'http://127.0.0.1:34567')
            return dict(message='HTTPS checked')
        with mock.patch.object(proxy, 'scan', side_effect=AssertionError('must not select another upstream')), \
             mock.patch.object(proxy, 'run_agent', side_effect=agent):
            result = proxy.diagnose('Example', 'testuser')
        self.assertIn('HTTPS checked', result['message'])

    def test_payload_injects_modules_over_stdin(self):
        sources = {'gateway.py': b'VALUE = 1', 'app_checks.py': b'VALUE = 2',
                   'wsl_agent.py': b'print("{}")'}
        response = mock.Mock(stdout=b'{}', returncode=0)
        with mock.patch.object(proxy.Path, 'read_bytes', lambda path: sources[path.name]), \
             mock.patch.object(proxy.Path, 'exists', return_value=True), \
             mock.patch.object(proxy.subprocess, 'run', return_value=response) as run:
            proxy.run_agent('Example', 'testuser', dict(action='status'))
        command = run.call_args.args[0]
        payload = run.call_args.kwargs['input'].decode('utf-8')
        self.assertIn('toolbox_gateway', payload)
        self.assertIn('toolbox_app_checks', payload)
        self.assertIn('__source__', payload)
        self.assertNotIn('VALUE', ' '.join(command))


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
        # Windows trust-store initialization is setup, outside the measured I/O budget.
        context = ssl.create_default_context()
        with self.server('tls_timeout') as port, \
             mock.patch('toolbox.wsl_agent.ssl.create_default_context', return_value=context):
            started = time.monotonic()
            result = https_test(f'http://127.0.0.1:{port}', timeout=.05)
            self.assertIn('未通过', result)
            self.assertLess(time.monotonic() - started, .25)


if __name__ == '__main__':
    unittest.main()
