"""Read-only application diagnostics inside WSL; never makes authenticated AI calls."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

PROXY_KEYS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY')


def safe_proxy(value):
    """Describe only an endpoint, excluding authentication and URL parameters."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname:
            return '[非标准代理值]'
        host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
        return parsed.scheme + '://' + host + (':' + str(parsed.port) if parsed.port else '')
    except ValueError:
        return '[非标准代理值]'


def environment_warnings(env, expected):
    warnings = []
    for key in PROXY_KEYS:
        if env.get(key) and env[key] != expected:
            warnings.append(key + ' 指向其他入口：' + safe_proxy(env[key]))
    for key in ('no_proxy', 'NO_PROXY'):
        if '*' in [p.strip() for p in env.get(key, '').split(',')]:
            warnings.append(key + '=* 会让支持该设置的软件绕过全部代理。')
    for key in ('PIP_PROXY', 'npm_config_proxy', 'npm_config_https_proxy'):
        if env.get(key) and env[key] != expected:
            warnings.append(key + ' 有单独覆盖：' + safe_proxy(env[key]))
    return warnings


def codex_overrides(home, env, expected):
    location = Path(env.get('CODEX_HOME') or Path(home) / '.codex') / '.env'
    warnings = []
    try:
        with location.open(encoding='utf-8') as stream:
            for line in stream:
                match = re.match(r'^\s*(?:export\s+)?([A-Za-z_]+)\s*=\s*(.*)$', line)
                if not match or match[1] not in PROXY_KEYS + ('no_proxy', 'NO_PROXY'):
                    continue
                try:
                    parts = shlex.split(match[2], comments=True)
                except ValueError:
                    warnings.append('Codex .env 的代理项格式无法确认。')
                    continue
                value = parts[0] if parts else ''
                if match[1] in ('no_proxy', 'NO_PROXY'):
                    if '*' in [p.strip() for p in value.split(',')]:
                        warnings.append('Codex .env 的 ' + match[1] + '=* 会绕过代理。')
                elif value != expected:
                    warnings.append('Codex .env 覆盖 ' + match[1] + '：' + (safe_proxy(value) if value else '[空值]'))
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        warnings.append('无法读取 Codex .env 的代理项。')
    if env.get('CODEX_HOME') and location.parent != Path(home) / '.codex':
        warnings.append('Codex 使用自定义 CODEX_HOME；默认 .codex 同步选项不覆盖此位置。')
    return warnings


def route_status(returncode, before, after, target):
    if returncode:
        return 'failed'
    return 'ok' if after.get(target, 0) > before.get(target, 0) else 'bypass'


def node_supports_env_proxy(version):
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match:
        return False
    major, minor, _ = map(int, match.groups())
    return major >= 24 or major == 22 and minor >= 21


def _run(args, env, home, timeout=15):
    try:
        return subprocess.run(args, env=env, cwd=str(home), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, '', 'timeout')
    except OSError:
        return subprocess.CompletedProcess(args, 127, '', 'unavailable')


def shell_environment(home):
    import pwd
    shell = pwd.getpwuid(os.getuid()).pw_shell
    if Path(shell).name not in ('bash', 'zsh'):
        raise RuntimeError('应用诊断目前支持 Bash/Zsh 登录环境。')
    # Keep the effective environment in memory only; never log or return it to the UI.
    code = 'import json,os;print("__TOOLBOX_ENV__"+json.dumps(dict(os.environ)))'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
    result = _run([shell, '-lic', command], dict(os.environ, HOME=str(home)), home, timeout=15)
    try:
        captured = json.loads(result.stdout.rsplit('__TOOLBOX_ENV__', 1)[1].splitlines()[0])
    except (ValueError, IndexError):
        raise RuntimeError('无法读取新 shell 的实际代理环境。')
    if result.returncode:
        raise RuntimeError('新 shell 未正常启动，应用诊断停止。')
    captured.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    return captured


def pip_command(env):
    for name in ('pip', 'pip3'):
        executable = shutil.which(name, path=env.get('PATH'))
        if executable:
            return [executable]
    executable = shutil.which('python3', path=env.get('PATH'))
    return [executable, '-m', 'pip'] if executable else None


def _failure_reason(result):
    error = (result.stderr or '').lower()
    if result.returncode == 124 or 'timed out' in error or 'timeout' in error:
        return '请求超时。'
    if any(word in error for word in ('certificate', 'ssl', 'tls')):
        return 'TLS/证书验证未通过。'
    if any(word in error for word in ('resolve', 'enotfound', 'eai_again')):
        return '域名解析未通过。'
    if any(word in error for word in ('refused', 'could not connect', 'connect failed')):
        return '目标或代理连接失败。'
    return '网络命令未成功（退出码 ' + str(result.returncode) + '）。'


def _configuration_overrides(name, env, home, expected):
    commands = {
        'Git': [['git', 'config', '--get-regexp', r'^(http\..*proxy|remote\..*\.proxy)$']],
        'pip': [(pip_command(env) or []) + ['config', 'get', 'global.proxy']],
        'npm': [['npm', 'config', 'get', 'proxy'], ['npm', 'config', 'get', 'https-proxy']],
    }
    values = []
    for args in commands.get(name, []):
        result = _run(args, env, home, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                value = line.split(None, 1)[-1] if name == 'Git' else line.strip()
                if value and value not in ('null', 'undefined', expected):
                    values.append(name + ' 有独立代理配置：' + safe_proxy(value))
    return values


def check(home, gateway):
    """Run bounded public read-only queries with the actual new-shell environment."""
    home = Path(home)
    env = shell_environment(home)
    state = gateway.status(home)
    expected = state.get('url', '')
    if not state.get('running') or not expected:
        return {'message': '本机代理入口未运行；请先连接 WSL。', 'tools': [], 'warnings': []}
    warnings = environment_warnings(env, expected)
    pip = pip_command(env)
    specs = [
        ('curl', 'curl', ['curl', '-q', '--max-time', '10', '--silent', '--show-error', '--fail', '--output', '/dev/null', 'https://example.com/'], 'example.com'),
        ('Git', 'git', ['git', '-c', 'credential.helper=', 'ls-remote', 'https://github.com/git/git.git', 'HEAD'], 'github.com'),
        ('pip', None, (pip or []) + ['index', 'versions', 'pip', '--index-url', 'https://pypi.org/simple', '--retries', '0', '--timeout', '8', '--disable-pip-version-check', '--no-cache-dir'], 'pypi.org'),
        ('npm', 'npm', ['npm', 'ping', '--registry=https://registry.npmjs.org/', '--fetch-retries=0', '--fetch-timeout=10000'], 'registry.npmjs.org'),
        ('Node fetch', 'node', ['node', '-e', "fetch('https://example.com/',{signal:AbortSignal.timeout(10000)}).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"], 'example.com'),
    ]
    rows = []
    for name, executable, args, host in specs:
        installed = bool(shutil.which(executable, path=env.get('PATH'))) if executable else bool(pip) and _run(pip + ['--version'], env, home, 5).returncode == 0
        if not installed:
            rows.append({'name': name, 'status': 'not_installed', 'detail': '未安装，未执行网络测试。'})
            continue
        if name == 'Node fetch':
            version = _run(['node', '--version'], env, home, 5).stdout
            if not node_supports_env_proxy(version) or env.get('NODE_USE_ENV_PROXY') != '1':
                rows.append({'name': name, 'status': 'configuration', 'detail': '需要 Node 22.21+ 或 24+，并设置 NODE_USE_ENV_PROXY=1；未推测其他 Node 程序的行为。'})
                continue
        warnings.extend(_configuration_overrides(name, env, home, expected))
        before = gateway.status(home).get('counts', {})
        result = _run(args, env, home, timeout=15)
        after = gateway.status(home).get('counts', {})
        status = route_status(result.returncode, before, after, host)
        detail = ('请求成功，本机入口观察到 ' + host + ' 的连接。' if status == 'ok' else
                  '请求成功，但未观察到本机入口连接；可能使用直连、缓存或其他代理。' if status == 'bypass' else _failure_reason(result))
        rows.append({'name': name, 'status': status, 'detail': detail})
    codex = shutil.which('codex', path=env.get('PATH'))
    if codex:
        version = _run([codex, '--version'], env, home, 8)
        match = re.search(r'\d+\.\d+\.\d+(?:[-+.][\w.]+)?', version.stdout)
        overrides = codex_overrides(home, env, expected)
        warnings.extend(overrides)
        detail = '版本 ' + (match[0] if match else '未知') + '；' + ('发现单独代理覆盖，请核对下面提示。' if overrides else '通用环境和 .env 代理项检查完成。')
        detail += '未运行 doctor 或模型 API，不代表登录、模型流或沙箱联网已验证。'
        rows.append({'name': 'Codex CLI', 'status': 'configuration', 'detail': detail})
    else:
        rows.append({'name': 'Codex CLI', 'status': 'not_installed', 'detail': '当前 WSL PATH 未找到 Codex；不检查 Windows 桌面客户端。'})
    passed = sum(r['status'] == 'ok' for r in rows)
    message = str(passed) + ' 项工具请求成功且观察到本机代理连接。'
    message += '\n首次采用固定入口需重开原有应用；之后新连接可跟随上游切换。'
    return {'message': message, 'tools': rows, 'warnings': list(dict.fromkeys(warnings)), 'local_url': expected}
