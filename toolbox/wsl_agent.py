#!/usr/bin/env python3
"""Standalone WSL payload. JSON request in argv, JSON response on stdout.

The Windows controller sends this file to ``python3 -``; no package imports or
Windows paths are needed inside Linux. All configuration belongs to this user.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


try:
    from . import gateway
except ImportError:
    gateway = sys.modules.get('toolbox_gateway')


KEYS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY')
START = '# >>> WSL Drive Toolbox >>>'
END = '# <<< WSL Drive Toolbox <<<'
SHELL_MARK = '# Managed by WSL Drive Toolbox.'
SHELL_FILES = ('.bashrc', '.profile', '.zshrc', '.zprofile')
LEGACY_MARKERS = ('# Managed by the desktop WSL Proxy Switcher.',
                  '# WSL Proxy Switcher: disabled for newly opened shells.')


def digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def read(path):
    return path.read_bytes() if path.exists() else None


def atomic_write(path, data):
    """Replace one file in its own directory, retaining its original mode."""
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix='.wsl-toolbox-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_url(value):
    if value is None:
        return
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or not parsed.hostname or not parsed.port or
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('代理地址必须为不含认证信息的 http://主机:端口。')
    if not re.fullmatch(r'[A-Za-z0-9.:-]+', parsed.hostname):
        raise ValueError('代理主机地址无效。')


def shell_content(url, gateway_mode=False):
    if url is None:
        return (SHELL_MARK + '\nunset ' + ' '.join(KEYS) + '\n').encode()
    lines = [SHELL_MARK]
    if gateway_mode:
        lines += ['export NODE_USE_ENV_PROXY=1', 'python3 "$HOME/.local/lib/wsl-drive-toolbox/gateway.py" ensure "$HOME" || return 1']
    lines += ['export ' + key + '=' + shlex.quote(url) for key in KEYS]
    lines += ['export no_proxy="${no_proxy:-${NO_PROXY:-localhost,127.0.0.1,::1,[::1]}}"',
              'export NO_PROXY="$no_proxy"']
    return ('\n'.join(lines) + '\n').encode()


def managed_block(data):
    text = (data or b'').decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    block = newline.join((START, '[ ! -f "$HOME/.config/wsl-drive-toolbox/proxy.sh" ] || . "$HOME/.config/wsl-drive-toolbox/proxy.sh"', END))
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise RuntimeError('shell 管理区块不完整，未覆盖原文件。')
    if START in text:
        begin, end = text.index(START), text.index(END) + len(END)
        if text.index(END) < begin:
            raise RuntimeError('shell 管理区块顺序不正确，未覆盖原文件。')
        return (text[:begin] + block + text[end:]).encode('utf-8')
    return (text + (newline if text and not text.endswith('\n') else '') + block + newline).encode('utf-8')


def edit_env(data, url):
    text = (data or b'').decode('utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    pattern = re.compile(r'^\s*(?:export\s+)?(' + '|'.join(KEYS) + r')\s*=')
    lines, seen = [], set()
    for line in text.splitlines(keepends=True):
        match = pattern.match(line)
        if match:
            key = match.group(1)
            if key not in seen:
                lines.append(key + '=' + (url or '') + newline)
                seen.add(key)
        else:
            lines.append(line)
    result = ''.join(lines)
    if result and not result.endswith('\n'):
        result += newline
    result += ''.join(key + '=' + (url or '') + newline for key in KEYS if key not in seen)
    return result.encode('utf-8')


def known_legacy_proxy(legacy):
    content = (legacy or b'').decode('utf-8', errors='replace')
    commands = [s.strip() for s in content.splitlines() if s.strip() and not s.lstrip().startswith('#')]
    if commands == ['unset ' + ' '.join(KEYS)]:
        return content.startswith(LEGACY_MARKERS)
    # Early installations used the same eight proxy exports without a tool marker.
    expected_tail = ['export ' + key + '="$http_proxy"' for key in KEYS[1:]] + [
        'export no_proxy="${no_proxy:-${NO_PROXY:-localhost,127.0.0.1,::1,[::1]}}"',
        'export NO_PROXY="$no_proxy"',
    ]
    if not commands or not re.fullmatch(r'export http_proxy="http://127\.0\.0\.1:[0-9]{1,5}"', commands[0]):
        return False
    if content.startswith(LEGACY_MARKERS):
        return all(line in expected_tail for line in commands[1:])
    return commands[1:] == expected_tail


def contains_legacy_source(line):
    lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|()')
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        # A multi-line command cannot be safely edited as a single source line.
        return bool(re.search(r'(?:^|[;&|]\s*)(?:source|\.)\s+.*?/\.config/proxy/7897\.sh\b', line.lstrip()))
    for i, (word, target) in enumerate(zip(words, words[1:])):
        if word in ('.', 'source') and target.endswith('/.config/proxy/7897.sh'):
            if (i == 0 or words[i - 1] in ('then', 'else', 'do', 'if', 'while', 'until', '{', '!', 'command', 'builtin')
                    or re.fullmatch(r'[;&|()]+', words[i - 1])):
                return True
    return False


def migrate_legacy(data, legacy):
    text = (data or b'').decode('utf-8')
    if '7897.sh' not in text:
        return data
    path = r'(?:\$\{HOME\}|\$HOME)/\.config/proxy/7897\.sh'
    path = rf'(?:{path}|"{path}"|~/\.config/proxy/7897\.sh)'
    readable = rf'\[[ \t]+-[rfe][ \t]+{path}[ \t]+\]'
    source = rf'(?:source|\.)[ \t]+{path}'
    guarded = rf'(?:{readable}[ \t]*&&[ \t]*)?{source}'
    conditional = rf'if[ \t]+{readable};[ \t]*then[ \t]+{source};[ \t]*fi'
    hook = re.compile(rf'^([ \t]*)(?:{guarded}|{conditional})(;?(?:[ \t]+#[^\r\n]*)?[ \t]*\r?)$', re.M)
    for line in text.splitlines():
        if '7897.sh' in line and not hook.fullmatch(line) and contains_legacy_source(line):
            raise RuntimeError('启动文件含自定义的旧代理 source 语句，请核对该行后重试。')
    if not hook.search(text):
        return data
    if not known_legacy_proxy(legacy):
        raise RuntimeError('启动文件引用了未知的旧 7897.sh；请手动处理该引用后重试，原文件未更改。')
    # A no-op keeps an enclosing multi-line shell conditional syntactically valid.
    return hook.sub(r'\g<1>:\g<2>', text).encode()


def local_git_endpoints(managed_shell, legacy, configs):
    """Only positively identified local upstreams may replace a personal Git override."""
    endpoints = set()
    for script in (managed_shell, legacy if known_legacy_proxy(legacy) else None):
        match = re.search(rb'''^export http_proxy=["']?([^\s"']+)["']?\s*$''', script or b'', re.M)
        if match:
            endpoints.add(match[1])
    for config in configs:
        upstream = (config or {}).get('upstream')
        if upstream:
            host = upstream['host']
            host = f'[{host}]' if ':' in host else host
            endpoints.add(f"{upstream['protocol']}://{host}:{upstream['port']}".encode())
    local = {url.lower() for url in endpoints if re.fullmatch(
        rb'(?:https?|socks5h?)://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]+', url, re.I)}
    return local | {url + b'/' for url in local}


class Manager:
    def __init__(self, home):
        self.home = Path(home).absolute()
        self.state = self.home / '.local/state/wsl-drive-toolbox'
        self.backups = self.state / 'backups'
        self.shell = self.home / '.config/wsl-drive-toolbox/proxy.sh'

    def check_paths(self, paths):
        for path in paths:
            if '..' in path.parts or (path != self.home and self.home not in path.parents):
                raise RuntimeError('配置路径超出当前用户 HOME。')
            for part in (path, *path.parents):
                if part.is_symlink():
                    raise RuntimeError('配置路径含符号链接，未写入：' + str(part))
                if part == self.home:
                    break
            if path.exists() and not path.is_file():
                raise RuntimeError('配置目标不是普通文件：' + str(path))

    @contextmanager
    def lock(self):
        lockpath = self.state / 'transaction.lock'
        self.check_paths([lockpath, self.backups / '.check'])
        self.state.mkdir(parents=True, exist_ok=True)
        with open(lockpath, 'a+b') as handle:
            try:
                if os.name == 'nt':
                    import msvcrt
                    if handle.tell() == 0:
                        handle.write(b'0')
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError('另一个配置操作正在进行，请稍后重试。') from error
            try:
                yield
            finally:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def transactions(self):
        return sorted(self.backups.glob('*/manifest.json')) if self.backups.exists() else []

    def migrate_git(self, before, desired, endpoints):
        if 'GIT_CONFIG_GLOBAL' in os.environ or not shutil.which('git'):
            return ''
        xdg = Path(os.environ.get('XDG_CONFIG_HOME') or self.home / '.config')
        paths = [self.home / '.gitconfig']
        if xdg.is_absolute() and self.home in (xdg / 'git/config').parents:
            paths.append(xdg / 'git/config')
        updates = {}
        direct_values = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                copy = Path(directory) / 'gitconfig'
                cmd = ['git', 'config', '--file', str(copy), '--no-includes']
                for path in paths:
                    self.check_paths([path])
                    original = read(path)
                    if not original:
                        continue
                    copy.write_bytes(original)
                    result = subprocess.run(cmd + ['--null', '--get-all', 'http.proxy'], capture_output=True, timeout=5)
                    if result.returncode == 1:  # Key absent in this file.
                        continue
                    result.check_returncode()
                    values = result.stdout.split(b'\0')[:-1]
                    if not values or any(value.lower() not in endpoints for value in values):
                        return 'Git 保留了独立的 http.proxy，可用「工具联网检查」确认路径。'
                    subprocess.run(cmd + ['--unset-all', 'http.proxy'], capture_output=True, check=True, timeout=5)
                    updates[path] = (original, copy.read_bytes())
                    direct_values.extend(values)
            if updates:
                # Removing an override could expose a lower-precedence system or included proxy.
                result = subprocess.run(['git', 'config', '--includes', '--null', '--get-all', 'http.proxy'],
                                        cwd=self.home, env=dict(os.environ, HOME=str(self.home)),
                                        capture_output=True, check=True, timeout=5)
                if sorted(result.stdout.split(b'\0')[:-1]) != sorted(direct_values):
                    return 'Git 的其他配置来源也设置了 http.proxy，已保留原设置。'
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return '未能检查个人 Git 配置，可用「工具联网检查」确认路径。'
        for path, (original, modified) in updates.items():
            before[path] = original
            desired[path] = modified
        return 'Git 已移除指向已知本地代理的 http.proxy，将跟随终端。' if updates else ''

    def apply(self, url, codex=False, verify=True, extra_files=None, gateway_mode=False, verify_connection=None):
        validate_url(url)
        with self.lock():
            for path in self.transactions():
                if json.loads(path.read_text())['status'] in ('prepared', 'recovery_required'):
                    raise RuntimeError('存在中断的配置事务；请先使用“回滚上次配置”。')
            previous_gateway = gateway.load_config(self.home) if gateway else None
            extra_files = dict(extra_files or {})
            if previous_gateway and not gateway_mode:
                disabled = dict(previous_gateway, enabled=False)
                extra_files[self.home / gateway.CONFIG] = json.dumps(disabled, indent=2).encode()
            files = [self.home / item for item in SHELL_FILES]
            files += [self.home / item for item in ('.bash_profile', '.bash_login') if (self.home / item).exists()]
            legacy = self.home / '.config/proxy/7897.sh'
            targets = files + [self.shell] + ([self.home / '.codex/.env'] if codex else [])
            targets += list(extra_files)
            self.check_paths(targets + [legacy])
            before = {path: read(path) for path in targets}
            if before[self.shell] and not before[self.shell].startswith(SHELL_MARK.encode()):
                raise RuntimeError('代理管理文件含未知内容，未覆盖。')
            legacy_content = read(legacy)
            desired = {path: managed_block(migrate_legacy(before[path], legacy_content)) for path in files}
            desired[self.shell] = shell_content(url, gateway_mode)
            desired.update(extra_files)
            if codex:
                env = self.home / '.codex/.env'
                desired[env] = edit_env(before[env], url)
            attempted_gateway = json.loads(extra_files[self.home / gateway.CONFIG]) if gateway and self.home / gateway.CONFIG in extra_files else previous_gateway
            git_notice = ''
            if gateway_mode:
                endpoints = local_git_endpoints(before[self.shell], legacy_content, (previous_gateway, attempted_gateway))
                git_notice = self.migrate_git(before, desired, endpoints)
            if desired == before:
                if gateway:
                    gateway.reconcile(self.home, previous_gateway)
                if verify_connection:
                    verify_connection()
                if verify:
                    verify_environment(self.home, url)
                message = '配置已经相同；新 shell 环境已验证。' if verify else '配置已经相同。'
                return {'message': message + git_notice, 'git_notice': git_notice}
            self.backups.mkdir(parents=True, exist_ok=True)
            folder = self.backups / (str(time.time_ns()) + '-' + secrets.token_hex(3))
            folder.mkdir(mode=0o700)
            entries = []
            for index, (path, data) in enumerate(before.items()):
                backup = str(index) + '.bak'
                if data is not None:
                    atomic_write(folder / backup, data)
                entries.append(dict(path=path.relative_to(self.home).as_posix(), backup=backup,
                                    before=digest(data), after=digest(desired[path])))
            manifest = dict(status='prepared', entries=entries)
            manifest_path = folder / 'manifest.json'
            self.save_manifest(manifest_path, manifest)
            changed = []
            try:
                for path, data in desired.items():
                    if read(path) != before[path]:
                        raise RuntimeError('配置被其他程序修改，停止覆盖。')
                    if data != before[path]:
                        atomic_write(path, data)
                        changed.append(path)
                if any(read(path) != data for path, data in desired.items()):
                    raise RuntimeError('配置写入后校验失败。')
                if gateway:
                    gateway.reconcile(self.home, previous_gateway)
                if verify_connection:
                    verify_connection()
                if verify:
                    verify_environment(self.home, url)
                manifest['status'] = 'applied'
                self.save_manifest(manifest_path, manifest)
            except Exception:
                complete = True
                for path in reversed(changed):
                    try:
                        if read(path) != desired[path]:
                            complete = False
                            continue
                        atomic_write(path, before[path])
                    except OSError:
                        complete = False
                if gateway:
                    try:
                        gateway.reconcile(self.home, attempted_gateway)
                    except (OSError, RuntimeError):
                        complete = False
                manifest['status'] = 'compensated' if complete else 'recovery_required'
                self.save_manifest(manifest_path, manifest)
                raise
            return {'message': ('显式代理已关闭' if url is None else '代理配置已写入') +
                    ('，新的登录/交互 shell 环境已验证。' if verify else '。') +
                    (' Codex .env 已同步；现有进程需要重新启动。' if codex else '') + git_notice,
                    'git_notice': git_notice}

    @staticmethod
    def save_manifest(path, manifest):
        atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))

    def rollback(self):
        with self.lock():
            pending = [(path, json.loads(path.read_text())) for path in reversed(self.transactions())]
            pending = [(path, state) for path, state in pending if state['status'] in ('applied', 'prepared', 'recovery_required')]
            if not pending:
                return {'message': '没有可回滚的配置。'}
            manifest_path, state = pending[0]
            restores = {}
            targets = [self.home / entry['path'] for entry in state['entries']]
            self.check_paths(targets)
            for entry, target in zip(state['entries'], targets):
                backup = manifest_path.parent / entry['backup']
                if backup.parent != manifest_path.parent or backup.is_symlink():
                    raise RuntimeError('备份路径无效。')
                original = read(backup) if entry['before'] is not None else None
                if digest(original) != entry['before']:
                    raise RuntimeError('备份校验失败，未恢复任何文件。')
                current = digest(read(target))
                allowed = (entry['after'],) if state['status'] == 'applied' else (entry['after'], entry['before'])
                if current not in allowed:
                    raise RuntimeError('配置存在外部修改冲突，保留现状，未恢复任何文件。')
                restores[target] = (read(target), original)
            previous_gateway = gateway.load_config(self.home) if gateway else None
            # Prepared also makes an interrupted rollback recoverable on the next click.
            state['status'] = 'prepared'
            self.save_manifest(manifest_path, state)
            for target, (expected, original) in restores.items():
                if read(target) != expected:
                    raise RuntimeError('回滚时出现外部修改冲突，已停止。')
                atomic_write(target, original)
                if read(target) != original:
                    raise RuntimeError('回滚写后校验失败。')
            if gateway:
                gateway.reconcile(self.home, previous_gateway)
            state['status'] = 'restored'
            self.save_manifest(manifest_path, state)
            return {'message': '已恢复上次操作前的内容及文件存在状态。现有终端环境不会自动改变。'}


def verify_environment(home, url):
    import pwd
    shell = pwd.getpwuid(os.getuid()).pw_shell
    if Path(shell).name not in ('bash', 'zsh'):
        raise RuntimeError('当前登录 shell 不受支持；自动配置仅支持 Bash 和 Zsh。')
    if os.environ.get('ZDOTDIR') and Path(os.environ['ZDOTDIR']) != home:
        raise RuntimeError('检测到自定义 ZDOTDIR，请手动加载代理文件。')
    command = 'printf "__WSL_TOOLBOX_ENV__"; ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(
        'import json,os;print(json.dumps({k:os.environ.get(k, "") for k in ' + repr(KEYS) + '}))')
    env = dict(os.environ, HOME=str(home))
    for key in KEYS:
        env.pop(key, None)
    for mode in ('-ic', '-lic'):
        result = subprocess.run([shell, mode, command], env=env, capture_output=True, timeout=12)
        output = result.stdout.decode('utf-8', 'replace')
        try:
            actual = json.loads(output.rsplit('__WSL_TOOLBOX_ENV__', 1)[1].splitlines()[0])
        except (IndexError, ValueError) as error:
            raise RuntimeError('无法确认新 shell 的实际代理环境，已撤销此次配置。') from error
        if result.returncode or any(actual.get(key) != (url or '') for key in KEYS):
            raise RuntimeError('新 shell 的代理环境被其他配置覆盖，已撤销此次配置。')


def probe_protocol(upstream, echo_port, timeout=2.2):
    """Validate a local nonce through the actual protocol, without DNS or Internet."""
    end = time.monotonic() + timeout
    try:
        with gateway.open_tunnel(upstream, '127.0.0.1', int(echo_port), timeout=timeout) as sock:
            token = ('wsl-toolbox-' + secrets.token_hex(24) + '\n').encode('ascii')
            sock.settimeout(max(.01, end - time.monotonic()))
            sock.sendall(token)
            echoed = gateway.receive_exact(sock, len(token), end)
            return (True, '本机随机回显验证通过') if echoed == token else (False, '回显内容不匹配')
    except (OSError, ValueError) as error:
        reason = str(error)
        if '认证' in reason or 'CONNECT' in reason:
            return False, reason
        return False, '端点不可达、协议不符或规则禁止本地回环'


def probe_http(host, port, echo_port, timeout=2.2):
    return probe_protocol(dict(host=host, port=port, protocol='http'), echo_port, timeout)


def reachable_hosts():
    hosts = ['127.0.0.1', '::1']
    try:
        result = subprocess.run(['ip', '-j', 'route', 'show', 'default'], capture_output=True, timeout=3, check=True)
        hosts += [route['gateway'] for route in json.loads(result.stdout) if route.get('gateway')]
    except (OSError, ValueError, subprocess.SubprocessError):
        # /proc remains available on minimal distributions without iproute2.
        try:
            import struct
            for line in Path('/proc/net/route').read_text().splitlines()[1:]:
                parts = line.split()
                if parts[1] == '00000000':
                    hosts.append(socket.inet_ntoa(struct.pack('<I', int(parts[2], 16))))
        except (OSError, ValueError, IndexError):
            pass
    return list(dict.fromkeys(hosts))


def tls_get(sock, host, timeout):
    """TLS over any stream, including an already TLS-encrypted HTTPS proxy."""
    deadline = time.monotonic() + timeout
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_side=False, server_hostname=host)

    def exchange(operation):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTPS deadline')
            sock.settimeout(remaining)
            try:
                result = operation()
            except ssl.SSLWantReadError:
                pending = outgoing.read()
                if pending:
                    sock.sendall(pending)
                data = sock.recv(65536)
                if not data:
                    raise OSError('TLS 连接提前关闭')
                incoming.write(data)
                continue
            pending = outgoing.read()
            if pending:
                sock.sendall(pending)
            return result

    exchange(tls.do_handshake)
    request = ('GET / HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\n\r\n').encode('ascii')
    exchange(lambda: tls.write(request))
    response = b''
    while b'\r\n' not in response and len(response) < 8192:
        part = exchange(lambda: tls.read(1024))
        if not part:
            raise OSError('HTTPS 响应提前关闭')
        response += part
    if not re.match(rb'HTTP/1\.[01] [1-5][0-9][0-9](?: |\r)', response):
        raise OSError('HTTPS 响应无效')


def network_test(upstream, timeout=12):
    end = time.monotonic() + timeout
    try:
        with gateway.open_tunnel(upstream, 'example.com', 443, timeout=timeout) as sock:
            tls_get(sock, 'example.com', max(.01, end - time.monotonic()))
        return dict(ok=True, reason='https://example.com 的 TLS/HTTPS 测试通过。', message='https://example.com 的 TLS/HTTPS 测试通过。')
    except (OSError, ValueError) as error:
        detail = str(error)
        if isinstance(error, ssl.SSLCertVerificationError):
            detail = '证书或主机名校验失败'
        elif not any(word in detail for word in ('认证', 'CONNECT', 'SOCKS5')):
            detail = '证书、节点、规则或网络问题'
        message = 'https://example.com 的 TLS/HTTPS 测试未通过（' + detail + '）。'
        return dict(ok=False, reason=message, message=message)


def https_test(url, timeout=12):
    parsed = urlsplit(url)
    return network_test(dict(host=parsed.hostname, port=parsed.port, protocol=parsed.scheme), timeout)['reason']


def probe_candidates(request):
    candidates = request['candidates']
    hosts = reachable_hosts()
    deadline = time.monotonic() + min(30, max(0, float(request.get('budget', 18))))

    def probe(candidate):
        address = candidate.get('address', candidate.get('host', '127.0.0.1'))
        if address == '0.0.0.0':
            order = [host for host in hosts if ':' not in host]
        elif address == '::':
            order = hosts
        else:
            order = [address]
        protocol = candidate.get('protocol', 'http')
        protocols = ('http', 'socks5', 'https') if protocol == 'auto' else (protocol,)
        reason = '检测时间预算已用完'
        for host in order:
            for protocol in protocols:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return dict(candidate, verified=False, reason=reason, pending=True)
                upstream = dict(host=host, port=candidate['port'], protocol=protocol)
                if protocol == 'http':
                    # Keep the legacy callable for controller compatibility and fixtures.
                    ok, reason = probe_http(host, candidate['port'], request['echo_port'], min(2.2, remaining))
                else:
                    ok, reason = probe_protocol(upstream, request['echo_port'], min(2.2, remaining))
                if ok:
                    return dict(candidate, host=host, protocol=protocol, verified=True, reason=reason)
        if request.get('allow_external') and str(candidate.get('source', '')).startswith(('system', 'manual')):
            for host in order:
                for protocol in protocols:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return dict(candidate, verified=False, reason=reason, pending=True)
                    network = network_test(dict(host=host, port=candidate['port'], protocol=protocol), min(8, remaining))
                    reason = network['reason']
                    if network['ok']:
                        return dict(candidate, host=host, protocol=protocol, verified=True, reason=reason, external=True)
        return dict(candidate, verified=False, reason=reason)

    workers = min(16, max(1, int(request.get('max_workers', 8))))
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = [executor.submit(probe, candidate) for candidate in candidates]
    done, pending = wait(futures, timeout=max(0, deadline - time.monotonic()))
    results = []
    for candidate, future in zip(candidates, futures):
        if future in done:
            try:
                results.append(future.result())
            except (OSError, ValueError, KeyError) as error:
                results.append(dict(candidate, verified=False, reason=str(error)))
        else:
            future.cancel()
            results.append(dict(candidate, verified=False, pending=True, reason='检测时间预算已用完'))
    executor.shutdown(wait=True)
    pending_count = sum(bool(item.get('pending')) for item in results)
    return dict(results=results, hosts=hosts, coverage=dict(total=len(results), completed=len(results) - pending_count, pending=pending_count))


def dispatch(request):
    action = request['action']
    manager = Manager(Path.home())
    if action == 'probe':
        return probe_candidates(request)
    if action == 'connect_gateway':
        if gateway is None:
            raise RuntimeError('缺少 WSL gateway payload。')
        upstream = gateway.validate_upstream(request['upstream'])
        plan = gateway.prepare(manager.home, upstream)
        network = {}
        def verify_connection():
            network.update(network_test(dict(host='127.0.0.1', port=int(plan['url'].rsplit(':', 1)[1]), protocol='http')))
            if not network['ok']:
                raise RuntimeError(network['reason'])
        result = manager.apply(plan['url'], codex=request.get('codex', False),
                               extra_files=plan['files'], gateway_mode=True, verify_connection=verify_connection)
        result.update(local_url=plan['url'], upstream=upstream, network=network)
        result['message'] += ' ' + network['reason']
        return result
    if action == 'gateway_status':
        return gateway.status(manager.home)
    if action == 'apps':
        try:
            from . import app_checks
        except ImportError:
            app_checks = sys.modules.get('toolbox_app_checks')
        if app_checks is None:
            raise RuntimeError('缺少 WSL 应用诊断 payload。')
        return app_checks.check(manager.home, gateway)
    if action == 'apply':
        return manager.apply(request.get('url'), codex=request.get('codex', False))
    if action == 'rollback':
        return manager.rollback()
    if action == 'https':
        return {'message': https_test(request['url'])}
    if action == 'status':
        content = read(manager.shell)
        match = re.search(rb'^export http_proxy=(.+)$', content or b'', re.M)
        url = match.group(1).decode().strip("'\"") if match else None
        return {'message': '当前用户已有工具管理的代理文件。' if url else '当前用户尚未配置工具管理的代理。',
                'configured': bool(url), 'url': url}
    raise ValueError('未知的 WSL 操作。')


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(sys.argv[1])), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=True))
        sys.exit(1)
