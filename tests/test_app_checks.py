import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import json
import os
import subprocess
import shutil

from toolbox import app_checks


class ApplicationChecksTests(unittest.TestCase):
    def test_proxy_display_redacts_credentials_and_query(self):
        self.assertEqual(app_checks.safe_proxy('http://alice:secret@127.0.0.1:1234/?token=private'), 'http://127.0.0.1:1234')
        self.assertNotIn('secret', app_checks.safe_proxy('secret malformed value'))

    def test_codex_dotenv_override_is_reported_without_other_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / '.codex').mkdir()
            (home / '.codex/.env').write_text('API_KEY=private\nHTTPS_PROXY=http://user:secret@old-proxy:8080\n', encoding='utf-8')
            result = app_checks.codex_overrides(home, {}, 'http://127.0.0.1:1234')
            self.assertTrue(result)
            self.assertIn('old-proxy:8080', result[0])
            self.assertNotIn('private', str(result))
            self.assertNotIn('secret', str(result))

    def test_network_success_without_gateway_evidence_is_not_routed_success(self):
        self.assertEqual(app_checks.route_status(0, {'example.com':2}, {'example.com':2}, 'example.com'), 'bypass')
        self.assertEqual(app_checks.route_status(0, {'example.com':2}, {'example.com':3}, 'example.com'), 'ok')
        self.assertEqual(app_checks.route_status(1, {}, {'example.com':1}, 'example.com'), 'failed')

    def test_no_proxy_wildcard_and_mismatched_env_have_clear_warnings(self):
        warnings = app_checks.environment_warnings({'NO_PROXY':'*','https_proxy':'http://localhost:9'}, 'http://127.0.0.1:1234')
        self.assertTrue(any('NO_PROXY' in item for item in warnings))
        self.assertTrue(any('https_proxy' in item for item in warnings))

    def test_node_proxy_support_uses_documented_version_floor(self):
        self.assertFalse(app_checks.node_supports_env_proxy('v20.19.0'))
        self.assertFalse(app_checks.node_supports_env_proxy('v22.20.0'))
        self.assertTrue(app_checks.node_supports_env_proxy('v22.21.0'))
        self.assertTrue(app_checks.node_supports_env_proxy('v24.0.0'))

    def test_diagnostics_preserve_new_shell_config_and_unsets(self):
        captured = {'HOME':'/tmp/fixture','PATH':'/custom/bin','PIP_CONFIG_FILE':'/tmp/custom.ini','GIT_CONFIG_GLOBAL':'/tmp/gitconfig'}
        result = subprocess.CompletedProcess([],0,'__TOOLBOX_ENV__'+json.dumps(captured),'')
        with mock.patch.dict('sys.modules', {'pwd':SimpleNamespace(getpwuid=lambda _:SimpleNamespace(pw_shell='/bin/bash'))}), \
             mock.patch.object(app_checks.os, 'getuid', return_value=123, create=True), \
             mock.patch.dict(os.environ, {'REMOVED_BY_SHELL':'old'}, clear=True), mock.patch.object(app_checks, '_run', return_value=result):
            env = app_checks.shell_environment(Path('/tmp/fixture'))
        self.assertEqual(env['PIP_CONFIG_FILE'], '/tmp/custom.ini')
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/tmp/gitconfig')
        self.assertFalse('REMOVED_BY_SHELL' in env, 'variables unset by the shell must remain absent')

    def test_pip_diagnostics_use_shell_path_executable(self):
        with mock.patch.object(app_checks.shutil, 'which', side_effect=lambda name, **_: '/venv/bin/pip' if name=='pip' else None):
            self.assertEqual(app_checks.pip_command({'PATH':'/venv/bin'}), ['/venv/bin/pip'])

    @unittest.skipUnless(os.name == 'posix' and shutil.which('curl'), 'Requires Linux curl')
    def test_real_curl_new_shell_follows_http_then_socks_at_same_entry(self):
        from toolbox import gateway, wsl_agent
        from tests.test_gateway import Fixture, HttpProxy, SocksProxy, header

        class SocksHttp(SocksProxy):
            def forward(self):
                self.server.request = header(self.request)
                self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nsecond')

        first, second = Fixture(HttpProxy), Fixture(SocksHttp)
        first.server.requests, first.server.label = [], b'first'
        try:
            with tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                manager = wsl_agent.Manager(home)
                try:
                    plan = gateway.prepare(home, {'host':'127.0.0.1','port':first.port,'protocol':'http'})
                    manager.apply(plan['url'], extra_files=plan['files'], gateway_mode=True)
                    env = app_checks.shell_environment(home)
                    command = ['curl','-q','--silent','--fail','--max-time','4','http://toolbox-check.invalid/']
                    one = app_checks._run(command, env, home)
                    self.assertEqual(one.returncode, 0)
                    self.assertEqual(one.stdout, 'first')
                    updated = gateway.prepare(home, {'host':'127.0.0.1','port':second.port,'protocol':'socks5'})
                    manager.apply(updated['url'], extra_files=updated['files'], gateway_mode=True)
                    self.assertEqual(updated['url'], plan['url'])
                    # Reuse the already-captured application environment: only the upstream changed.
                    two = app_checks._run(command, env, home)
                    self.assertEqual(two.returncode, 0)
                    self.assertEqual(two.stdout, 'second')
                    self.assertTrue(second.server.request.startswith(b'GET / HTTP/1.1'))
                    self.assertEqual(gateway.status(home)['counts']['toolbox-check.invalid'], 2)
                finally:
                    gateway.stop(home)
        finally:
            first.close()
            second.close()
