import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from toolbox import wsl_agent as agent


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.manager = agent.Manager(self.home)

    def apply(self, url="http://127.0.0.1:18567", codex=False):
        return self.manager.apply(url, codex=codex, verify=False)

    def test_first_setup_preserves_custom_content_and_optional_codex(self):
        (self.home / '.bashrc').write_bytes(b'# custom\r\nexport KEEP=yes\r\n')
        self.apply()
        self.assertTrue((self.home / '.bashrc').read_bytes().startswith(b'# custom\r\nexport KEEP=yes\r\n'))
        self.assertTrue((self.home / '.zshrc').exists())
        self.assertTrue((self.home / '.profile').exists())
        self.assertFalse((self.home / '.codex').exists())
        self.assertIn('18567', self.manager.shell.read_text())
        before = (self.home / '.bashrc').read_bytes()
        self.apply('http://172.20.0.1:21567')
        self.assertEqual((self.home / '.bashrc').read_bytes(), before)
        self.assertIn('172.20.0.1:21567', self.manager.shell.read_text())

    def test_repeat_apply_does_not_replace_last_rollback(self):
        self.apply()
        self.apply()
        self.manager.rollback()
        self.assertFalse(self.manager.shell.exists())

    def test_reversed_managed_markers_preserve_original(self):
        original = b'# <<< WSL Drive Toolbox <<<\ncustom\n# >>> WSL Drive Toolbox >>>\n'
        (self.home / '.bashrc').write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, '区块'):
            self.apply()
        self.assertEqual((self.home / '.bashrc').read_bytes(), original)

    def test_lock_blocks_overlapping_mutation(self):
        with self.manager.lock():
            with self.assertRaisesRegex(RuntimeError, '另一个'):
                self.apply()
        self.assertFalse(self.manager.shell.exists())

    def test_home_boundary_does_not_require_python39_path_methods(self):
        with mock.patch.object(Path, 'is_relative_to', None, create=True):
            self.manager.check_paths([self.home / '.bashrc'])

    def test_home_boundary_rejects_direct_and_parent_escape(self):
        for path in (self.home.parent / 'outside', self.home / '..' / 'outside'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.manager.check_paths([path])

    def test_interrupted_transaction_can_be_rolled_back(self):
        (self.home / '.bashrc').write_text('# original\n')
        self.apply()
        transaction = sorted(self.manager.backups.iterdir())[-1]
        manifest_path = transaction / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['status'] = 'prepared'
        manifest_path.write_text(json.dumps(manifest))
        # Model a process exit after only some files had been replaced.
        (self.home / '.profile').unlink()
        with self.assertRaisesRegex(RuntimeError, '中断'):
            self.apply('http://127.0.0.1:21567')
        self.manager.rollback()
        self.assertEqual((self.home / '.bashrc').read_text(), '# original\n')
        self.assertFalse(self.manager.shell.exists())

    def test_rollback_restores_original_existence_and_content(self):
        original = b'export MY_SETTING=1\n'
        (self.home / '.bashrc').write_bytes(original)
        self.apply(codex=True)
        self.manager.rollback()
        self.assertEqual((self.home / '.bashrc').read_bytes(), original)
        self.assertFalse((self.home / '.profile').exists())
        self.assertFalse(self.manager.shell.exists())
        self.assertFalse((self.home / '.codex/.env').exists())

    def test_off_and_rollback_restore_last_connection(self):
        self.apply()
        connected = self.manager.shell.read_bytes()
        self.manager.apply(None, verify=False)
        self.assertIn(b'unset ', self.manager.shell.read_bytes())
        self.manager.rollback()
        self.assertEqual(self.manager.shell.read_bytes(), connected)

    def test_rollback_refuses_external_edit_without_partial_restore(self):
        self.apply(codex=True)
        (self.home / '.bashrc').write_text('external edit\n')
        before = self.manager.shell.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '外部|其他|冲突'):
            self.manager.rollback()
        self.assertEqual((self.home / '.bashrc').read_text(), 'external edit\n')
        self.assertEqual(self.manager.shell.read_bytes(), before)

    def test_failed_shell_verification_compensates_all_writes(self):
        (self.home / '.bashrc').write_text('# keep\n')
        with mock.patch.object(agent, 'verify_environment', side_effect=RuntimeError('shell failed')):
            with self.assertRaisesRegex(RuntimeError, 'shell failed'):
                self.manager.apply('http://127.0.0.1:18567')
        self.assertEqual((self.home / '.bashrc').read_text(), '# keep\n')
        self.assertFalse(self.manager.shell.exists())

    @unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Requires Linux Bash')
    def test_real_bash_new_shell_loads_connection_and_disable(self):
        import pwd
        with mock.patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_shell=shutil.which('bash'))), \
             mock.patch.dict(os.environ, {'HOME': str(self.home), 'ZDOTDIR': ''}):
            self.manager.apply('http://127.0.0.1:18567')
            agent.verify_environment(self.home, 'http://127.0.0.1:18567')
            self.manager.apply(None)
            agent.verify_environment(self.home, None)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('zsh'), 'Requires Linux Zsh')
    def test_real_zsh_new_shell_loads_connection_and_disable(self):
        import pwd
        with mock.patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_shell=shutil.which('zsh'))), \
             mock.patch.dict(os.environ, {'HOME': str(self.home), 'ZDOTDIR': ''}):
            self.manager.apply('http://127.0.0.1:18567')
            agent.verify_environment(self.home, 'http://127.0.0.1:18567')
            self.manager.apply(None)
            agent.verify_environment(self.home, None)

    def test_codex_preserves_unrelated_keys_and_no_credentials_in_output(self):
        (self.home / '.codex').mkdir()
        (self.home / '.codex/.env').write_text('KEEP=value\nHTTP_PROXY=old\n')
        self.apply(codex=True)
        text = (self.home / '.codex/.env').read_text()
        self.assertIn('KEEP=value\n', text)
        self.assertIn('HTTP_PROXY=http://127.0.0.1:18567', text)

    def test_unmanaged_legacy_proxy_is_not_overwritten(self):
        legacy = self.home / '.config/proxy/7897.sh'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('export PRIVATE_THING=yes\n')
        (self.home / '.bashrc').write_text('source ~/.config/proxy/7897.sh\n')
        with self.assertRaisesRegex(RuntimeError, '旧|未知'):
            self.apply()
        self.assertEqual(legacy.read_text(), 'export PRIVATE_THING=yes\n')

    def test_known_legacy_source_is_migrated_and_rollback_restores_it(self):
        legacy = self.home / '.config/proxy/7897.sh'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('# Managed by the desktop WSL Proxy Switcher.\nexport http_proxy="http://127.0.0.1:7897"\n')
        old = '# custom\nsource ~/.config/proxy/7897.sh\n'
        (self.home / '.bashrc').write_text(old)
        self.apply()
        self.assertNotIn('7897.sh', (self.home / '.bashrc').read_text())
        self.manager.rollback()
        self.assertEqual((self.home / '.bashrc').read_text(), old)

    def test_unmarked_legacy_template_from_older_installs(self):
        legacy = self.home / '.config/proxy/7897.sh'
        legacy.parent.mkdir(parents=True)
        script = (
            '# Local WSL proxy\n'
            'export http_proxy="http://127.0.0.1:7897"\n'
            'export https_proxy="$http_proxy"\n'
            'export HTTP_PROXY="$http_proxy"\n'
            'export HTTPS_PROXY="$http_proxy"\n'
            'export all_proxy="$http_proxy"\n'
            'export ALL_PROXY="$http_proxy"\n\n'
            'export no_proxy="${no_proxy:-${NO_PROXY:-localhost,127.0.0.1,::1,[::1]}}"\n'
            'export NO_PROXY="$no_proxy"\n'
        ).encode()
        legacy.write_bytes(script)
        hook = b'[ -r "$HOME/.config/proxy/7897.sh" ] && . "$HOME/.config/proxy/7897.sh"\n'
        for name in ('.bashrc', '.profile'):
            (self.home / name).write_bytes(hook)
        self.apply()
        self.apply()
        for name in ('.bashrc', '.profile'):
            content = (self.home / name).read_bytes()
            self.assertNotIn(b'7897.sh', content)
            self.assertEqual(content.count(agent.START.encode()), 1)
        self.assertEqual(legacy.read_bytes(), script)
        self.manager.rollback()
        for name in ('.bashrc', '.profile'):
            self.assertEqual((self.home / name).read_bytes(), hook)

        legacy.write_bytes(script + b'export CUSTOM=value\n')
        with self.assertRaisesRegex(RuntimeError, '未知'):
            self.apply()
        self.assertEqual((self.home / '.bashrc').read_bytes(), hook)

    def test_legacy_filename_mentions_are_preserved(self):
        original = (b'# source ~/.config/proxy/7897.sh\n'
                    b'echo "source ~/.config/proxy/7897.sh"\n'
                    b'source ~/.config/proxy/7897.sh.bak\n')
        self.assertEqual(agent.migrate_legacy(original, None), original)
        marked = b'# Managed by the desktop WSL Proxy Switcher.\n'
        self.assertEqual(agent.migrate_legacy(original, marked), original)

    def test_one_line_legacy_conditional_and_unhandled_compound(self):
        path = b'"$HOME/.config/proxy/7897.sh"'
        marked = b'# WSL Proxy Switcher: disabled for newly opened shells.\nunset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY\n'
        conditional = b'if [ -f ' + path + b' ]; then source ' + path + b'; fi # old\r\n'
        self.assertEqual(agent.migrate_legacy(conditional, marked), b': # old\r\n')
        for line in (b'. ' + path + b' && echo ok', b'if custom; then . ' + path + b'; fi'):
            with self.subTest(line=line), self.assertRaisesRegex(RuntimeError, 'source'):
                agent.migrate_legacy(line, marked)

    def test_corrupted_backup_is_rejected(self):
        (self.home / '.bashrc').write_text('# original\n')
        self.apply()
        transaction = sorted(self.manager.backups.iterdir())[-1]
        next(transaction.glob('*.bak')).write_bytes(b'corrupt')
        before = self.manager.shell.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '备份|校验'):
            self.manager.rollback()
        self.assertEqual(self.manager.shell.read_bytes(), before)

    def test_standalone_payload_runs_without_package_imports(self):
        result = subprocess.run([sys.executable, '-', json.dumps({'action': 'status'})],
                                input=Path(agent.__file__).read_bytes(), capture_output=True, check=True)
        self.assertIn('message', json.loads(result.stdout.decode('utf-8')))

    def test_loopback_binding_does_not_probe_other_process_on_gateway(self):
        attempted = []
        def probe(host, port, echo_port, timeout=2.2):
            attempted.append(host)
            return host == '172.20.0.1', 'test probe'
        with mock.patch.object(agent, 'reachable_hosts', return_value=['127.0.0.1', '172.20.0.1']), \
             mock.patch.object(agent, 'probe_http', side_effect=probe):
            result = agent.dispatch({'action': 'probe', 'echo_port': 30000, 'candidates': [
                dict(address='127.0.0.1', host='127.0.0.1', port=18567)]})
        self.assertFalse(result['results'][0]['verified'])
        self.assertEqual(attempted, ['127.0.0.1'])

    def test_wildcard_binding_can_use_nat_gateway(self):
        def probe(host, port, echo_port, timeout=2.2):
            return host == '172.20.0.1', 'test probe'
        with mock.patch.object(agent, 'reachable_hosts', return_value=['127.0.0.1', '::1', '172.20.0.1']), \
             mock.patch.object(agent, 'probe_http', side_effect=probe):
            result = agent.dispatch({'action': 'probe', 'echo_port': 30000, 'candidates': [
                dict(address='0.0.0.0', host='127.0.0.1', port=18567)]})
        self.assertTrue(result['results'][0]['verified'])
        self.assertEqual(result['results'][0]['host'], '172.20.0.1')

    def test_windows_ipv6_wildcard_can_be_dualstack(self):
        def probe(host, port, echo_port, timeout=2.2):
            return host == '172.20.0.1', 'test probe'
        with mock.patch.object(agent, 'reachable_hosts', return_value=['127.0.0.1', '::1', '172.20.0.1']), \
             mock.patch.object(agent, 'probe_http', side_effect=probe):
            result = agent.dispatch({'action': 'probe', 'echo_port': 30000, 'candidates': [
                dict(address='::', host='::1', port=18567)]})
        self.assertTrue(result['results'][0]['verified'])
        self.assertEqual(result['results'][0]['host'], '172.20.0.1')


if __name__ == '__main__':
    unittest.main()

class GatewayTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.manager = agent.Manager(self.home)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: agent.gateway.stop(self.home))

    def connect(self):
        plan = agent.gateway.prepare(self.home, dict(host='127.0.0.1', port=19000, protocol='http'))
        self.manager.apply(plan['url'], verify=False, extra_files=plan['files'], gateway_mode=True)
        return plan

    @unittest.skipUnless(shutil.which('git'), 'Requires Git')
    def test_personal_git_proxy_inherits_gateway_and_rolls_back(self):
        legacy = self.home / '.config/proxy/7897.sh'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'# Managed by the desktop WSL Proxy Switcher.\nexport http_proxy="http://127.0.0.1:23194"\n')
        xdg = self.home / 'user-config'
        config = xdg / 'git/config'
        config.parent.mkdir(parents=True)
        original = (b'[http]\n\tproxy = http://127.0.0.1:23194/\n'
                    b'[http "https://internal.example"]\n\tproxy = http://localhost:23195\n'
                    b'[include]\n\tpath = ../other.config\n')
        config.write_bytes(original)
        other = xdg / 'other.config'
        other.write_bytes(b'[http "https://other.example"]\n\tproxy = http://localhost:23195\n')
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(xdg)}):
            self.connect()
        self.assertNotIn(b'23194', config.read_bytes())
        self.assertIn(b'23195', config.read_bytes())
        self.assertIn(b'../other.config', config.read_bytes())
        self.assertIn(b'23195', other.read_bytes())
        self.manager.rollback()
        self.assertEqual(config.read_bytes(), original)

    @unittest.skipUnless(shutil.which('git'), 'Requires Git')
    def test_git_removal_does_not_reactivate_proxy_in_an_included_file(self):
        config = self.home / '.gitconfig'
        original = b'[include]\n\tpath = other.config\n[http]\n\tproxy = http://127.0.0.1:19000\n'
        config.write_bytes(original)
        other = self.home / 'other.config'
        other.write_bytes(b'[http]\n\tproxy = http://other.example:3128\n')
        self.connect()
        self.assertEqual(config.read_bytes(), original)

    @unittest.skipUnless(shutil.which('git'), 'Requires Git')
    def test_only_matching_personal_unscoped_git_settings_migrate(self):
        config = self.home / '.gitconfig'
        original = b'[http]\n\tproxy = http://127.0.0.1:19000\n'
        config.write_bytes(original)
        self.connect()
        self.assertNotIn(b'19000', config.read_bytes())
        self.manager.rollback()
        for value in (b'http://127.0.0.1:19009', b'http://user:secret@127.0.0.1:19000',
                      b'http://corporate.example:19000', b'', b'http://127.0.0.1:19000\n\tproxy = '):
            with self.subTest(value=value):
                data = b'[http]\n\tproxy = ' + value + b'\n'
                config.write_bytes(data)
                self.connect()
                self.assertEqual(config.read_bytes(), data)
        config.write_bytes(original)
        with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(self.home / 'custom-git')}):
            self.connect()
        self.assertEqual(config.read_bytes(), original)

    def test_gateway_files_shell_and_process_rollback_together(self):
        self.connect()
        self.assertTrue(agent.gateway.status(self.home)['running'])
        self.assertIn(b'gateway.py', self.manager.shell.read_bytes())
        self.manager.rollback()
        self.assertFalse(agent.gateway.status(self.home)['running'])
        self.assertFalse((self.home / agent.gateway.CONFIG).exists())

    def test_gateway_off_and_rollback_restart_same_entry(self):
        plan = self.connect()
        self.manager.apply(None, verify=False)
        self.assertFalse(agent.gateway.status(self.home)['running'])
        self.manager.rollback()
        self.assertTrue(agent.gateway.status(self.home)['running'])
        self.assertEqual(agent.gateway.status(self.home)['url'], plan['url'])

    def test_failed_gateway_verification_restores_helper_and_process(self):
        plan = agent.gateway.prepare(self.home, dict(host='127.0.0.1', port=19000, protocol='http'))
        with mock.patch.object(agent, 'verify_environment', side_effect=RuntimeError('verify failed')):
            with self.assertRaisesRegex(RuntimeError, 'verify failed'):
                self.manager.apply(plan['url'], extra_files=plan['files'], gateway_mode=True)
        self.assertFalse(agent.gateway.status(self.home)['running'])
        self.assertFalse((self.home / agent.gateway.CONFIG).exists())
    def test_failed_connect_preserves_working_upstream_and_daemon(self):
        self.connect()
        before = (self.home / agent.gateway.CONFIG).read_bytes()
        with mock.patch.object(Path, 'home', return_value=self.home), \
             mock.patch.object(agent, 'verify_environment'), \
             mock.patch.object(agent, 'network_test', return_value=dict(ok=False, reason='dead upstream', message='dead upstream')):
            with self.assertRaisesRegex(RuntimeError, 'dead upstream'):
                agent.dispatch(dict(action='connect_gateway', upstream=dict(host='127.0.0.1', port=19999, protocol='http')))
        self.assertEqual((self.home / agent.gateway.CONFIG).read_bytes(), before)
        self.assertTrue(agent.gateway.status(self.home)['running'])

    def test_failed_first_connect_removes_new_service_and_files(self):
        with mock.patch.object(Path, 'home', return_value=self.home), \
             mock.patch.object(agent, 'verify_environment'), \
             mock.patch.object(agent, 'network_test', return_value=dict(ok=False, reason='dead upstream', message='dead upstream')):
            with self.assertRaisesRegex(RuntimeError, 'dead upstream'):
                agent.dispatch(dict(action='connect_gateway', upstream=dict(host='127.0.0.1', port=19999, protocol='http')))
        self.assertFalse((self.home / agent.gateway.CONFIG).exists())
        self.assertFalse(agent.gateway.status(self.home)['running'])


class UniversalProbeTests(unittest.TestCase):
    def test_auto_detects_socks_with_local_nonce_echo(self):
        from tests.test_gateway import Fixture, SocksProxy
        proxy = Fixture(SocksProxy)
        self.addCleanup(proxy.close)
        result = agent.dispatch(dict(action='probe', echo_port=23456, candidates=[
            dict(address='127.0.0.1', port=proxy.port, protocol='auto')]))
        self.assertTrue(result['results'][0]['verified'])
        self.assertEqual(result['results'][0]['protocol'], 'socks5')
        self.assertEqual(result['coverage']['completed'], 1)

    def test_external_fallback_requires_explicit_system_or_manual_source(self):
        candidates = [dict(address='127.0.0.1', port=12345, protocol='http', source='listener'),
                      dict(address='127.0.0.1', port=12345, protocol='http', source='system')]
        with mock.patch.object(agent, 'probe_http', return_value=(False, 'loopback forbidden')), \
             mock.patch.object(agent, 'network_test', return_value=dict(ok=True, reason='TLS verified')) as network:
            result = agent.dispatch(dict(action='probe', echo_port=23456, candidates=candidates, allow_external=True))
        self.assertFalse(result['results'][0]['verified'])
        self.assertTrue(result['results'][1]['verified'])
        self.assertEqual(network.call_count, 1)

    def test_budget_reports_pending_candidates(self):
        result = agent.dispatch(dict(action='probe', echo_port=23456, budget=0, candidates=[
            dict(address='127.0.0.1', port=12345, protocol='auto')]))
        self.assertEqual(result['coverage']['pending'], 1)
        self.assertFalse(result['results'][0]['verified'])


class LinuxGatewayShellSmoke(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and shutil.which('curl'), 'Requires Linux Bash and curl')
    def test_new_bash_restarts_helper_and_real_curl_uses_stable_entry(self):
        from tests.test_gateway import Fixture, HttpProxy
        proxy = Fixture(HttpProxy)
        proxy.server.requests, proxy.server.label = [], b'isolated shell gateway'
        self.addCleanup(proxy.close)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plan = agent.gateway.prepare(home, dict(host='127.0.0.1', port=proxy.port, protocol='http'))
            env = dict(os.environ, HOME=str(home), ZDOTDIR='', no_proxy='', NO_PROXY='')
            try:
                with mock.patch.dict(os.environ, env):
                    manager = agent.Manager(home)
                    manager.apply(plan['url'], extra_files=plan['files'], gateway_mode=True)
                    agent.gateway.stop(home)
                    result = subprocess.run(['bash', '-ic', 'printf __TOOLBOX_CURL__; curl -fsS --max-time 4 http://origin.invalid/isolated'],
                                            env=env, capture_output=True, timeout=8)
                    self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
                    self.assertEqual(result.stdout.rsplit(b'__TOOLBOX_CURL__', 1)[1], b'isolated shell gateway')
                    self.assertTrue(agent.gateway.status(home)['running'])
                    self.assertEqual(agent.gateway.status(home)['url'], plan['url'])
                    self.assertEqual(agent.gateway.status(home)['counts']['origin.invalid'], 1)
                    manager.rollback()
                    self.assertFalse(agent.gateway.status(home)['running'])
            finally:
                agent.gateway.stop(home)
