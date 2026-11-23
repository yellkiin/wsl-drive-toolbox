"""Windows proxy discovery and explicit, verified WSL configuration operations."""
import base64
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
from urllib.parse import urlsplit


class SelectionRequired(RuntimeError):
    def __init__(self, candidates, reason):
        super().__init__(reason)
        self.candidates = candidates
        self.reason = reason


INVENTORY_PS = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$map=@{}
Get-CimInstance Win32_Process | ForEach-Object {$map[[int]$_.ProcessId]=$_}
$rows=@(Get-NetTCPConnection -State Listen | ForEach-Object {
    $p=$map[[int]$_.OwningProcess]
    $chain=@(); $ancestor=$p
    for($i=0;$i -lt 5 -and $null -ne $ancestor;$i++){
        $chain+=($ancestor.Name+' '+$ancestor.ExecutablePath)
        $ancestor=$map[[int]$ancestor.ParentProcessId]
    }
    [pscustomobject]@{address=$_.LocalAddress;port=[int]$_.LocalPort;pid=[int]$_.OwningProcess;
        process=$p.Name;path=$p.ExecutablePath;created=[string]$p.CreationDate;ancestry=($chain -join ' | ')}
})
$r=Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
$addresses=@(Get-NetIPAddress -ErrorAction SilentlyContinue | ForEach-Object {$_.IPAddress})
[pscustomobject]@{listeners=$rows;local_addresses=$addresses;system_proxy=[pscustomobject]@{
    enabled=($r.ProxyEnable -eq 1);server=[string]$r.ProxyServer;pac=([string]$r.AutoConfigURL -ne '')
}} | ConvertTo-Json -Depth 5 -Compress
"""


def _decode(data):
    if isinstance(data, str):
        return data
    return data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else
                       'utf-16-le' if b'\x00' in data else 'utf-8-sig', errors='replace')


def _run(command, timeout=20, input=None):
    try:
        result = subprocess.run(command, input=input, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('系统命令无法完成或超时：' + command[0]) from error
    if result.returncode:
        # WSL diagnostics may include user paths; never return raw inventory/configuration.
        raise RuntimeError('系统命令失败：' + command[0] + '。请确认 WSL、Python 3 和所选用户可用。')
    return _decode(result.stdout)


def list_distros():
    names = [line.strip().lstrip('\ufeff') for line in _run(['wsl.exe', '--list', '--quiet']).splitlines() if line.strip()]
    running = set(line.strip() for line in _run(['wsl.exe', '--list', '--running', '--quiet']).splitlines())
    verbose = _run(['wsl.exe', '--list', '--verbose'])
    default = None
    for line in verbose.splitlines():
        match = re.match(r'^\s*\*\s*(.*?)\s{2,}\S+\s+[12]\s*$', line)
        if match:
            default = match.group(1).strip()
    return [dict(name=name, running=name in running, default=name == default) for name in names]


def list_users(distro):
    if not distro:
        raise ValueError('请先选择 WSL 发行版。')
    output = _run(['wsl.exe', '--distribution', distro, '--user', 'root', '--exec', 'getent', 'passwd'])
    users = []
    for line in output.splitlines():
        fields = line.split(':')
        if len(fields) == 7 and fields[2].isdigit():
            uid = int(fields[2])
            if 1000 <= uid < 65534 and fields[6].rsplit('/', 1)[-1] not in ('nologin', 'false'):
                users.append(fields[0])
    # Root remains an explicit option; a missing selection never resolves to it.
    return sorted(set(users)) + (['root'] if any(line.startswith('root:') for line in output.splitlines()) else [])


def default_user(distro):
    try:
        return _run(['wsl.exe', '--distribution', distro, '--exec', 'id', '-un']).strip()
    except RuntimeError:
        # Reading the default is optional when an explicit account is available.
        return ''


def _target(distro, user):
    if not distro or not user:
        raise ValueError('请明确选择 WSL 发行版和 Linux 用户；不会自动修改 root。')


def run_agent(distro, user, request, timeout=60):
    _target(distro, user)
    # PyInstaller includes toolbox/wsl_agent.py as package data under _MEIPASS.
    payload = b'import sys, types\n'
    for filename, name in (('gateway.py', 'toolbox_gateway'), ('app_checks.py', 'toolbox_app_checks')):
        path = Path(__file__).with_name(filename)
        if path.exists():
            source = path.read_bytes().decode('utf-8-sig')
            payload += (f'_module = types.ModuleType({name!r})\n'
                        f'_module.__source__ = {source!r}\n'
                        f'_module.__file__ = {filename!r}\n'
                        f'sys.modules[{name!r}] = _module\n'
                        f'exec(compile(_module.__source__, {filename!r}, "exec"), _module.__dict__)\n').encode('utf-8')
    payload += Path(__file__).with_name('wsl_agent.py').read_bytes()
    command = ['wsl.exe', '--distribution', distro, '--user', user, '--exec',
               'python3', '-', json.dumps(request, ensure_ascii=True)]
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('WSL 执行失败或超时；如刚执行写入，请先检查状态或回滚，不要重复点击。') from error
    try:
        response = json.loads(_decode(result.stdout))
    except ValueError as error:
        raise RuntimeError('WSL 未返回有效结果；请确认所选发行版安装了 Python 3。') from error
    if response.get('error'):
        raise RuntimeError(response['error'])
    if result.returncode:
        raise RuntimeError('WSL 操作未完成。')
    return response


def windows_inventory():
    encoded = base64.b64encode(INVENTORY_PS.encode('utf-16-le')).decode('ascii')
    data = json.loads(_run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]))
    return data


def application(record):
    name = (record.get('process') or '').lower().removesuffix('.exe')
    path = (record.get('path') or '').lower()
    ancestry = (record.get('ancestry') or '').lower()
    source = path + ' ' + ancestry
    if name == 'com.vortex.helper' or name == 'sakuracat' or name.startswith('sakuracat-'):
        return 'SakuraCat'
    if name.startswith(('verge-mihomo', 'verge-clash')):
        return 'Clash Verge'
    if ('mihomo' in name or name in ('clash', 'clash-meta', 'clash-premium', 'clash-core')) and any(
            word in source for word in ('clash-verge', 'clash_verge', 'clash verge')):
        return 'Clash Verge'
    return name or '未知进程'


def identity(record):
    return (record['address'], int(record['port']), int(record['pid']),
            record.get('created') or '', record.get('process') or '', record.get('path') or '')


def _normal_host(host):
    return '127.0.0.1' if host.lower() == 'localhost' else host.lower()


def _bind_matches(address, host):
    host = _normal_host(host)
    if address == host:
        return True
    try:
        # Windows IPv6 wildcard listeners may accept IPv4 too. Treat this as
        # overlapping for selection/identity; only a real probe proves access.
        return address == '::' or address == '0.0.0.0' and ipaddress.ip_address(host).version == 4
    except ValueError:
        return False


def ensure_owner(candidate, snapshot):
    rows = snapshot['listeners']
    match = [row for row in rows if identity(row) == identity(candidate)]
    if not match or not candidate.get('process') or not candidate.get('created'):
        raise RuntimeError('监听端点、进程身份或启动时间已变化/无法确认，请重新检测。')
    # Distinct specific addresses can share a port; overlapping bindings cannot
    # safely be attributed to one owner.
    for row in rows:
        if int(row['port']) != int(candidate['port']) or int(row['pid']) == int(candidate['pid']):
            continue
        if (_bind_matches(row['address'], candidate['address']) or
                _bind_matches(candidate['address'], row['address'])):
            raise RuntimeError('同一实际端点存在多个监听进程，请重新检测。')


PROTOCOLS = ('auto', 'http', 'https', 'socks5')


def _endpoint(value, default='auto'):
    parsed = urlsplit(value if '://' in value else '//' + value)
    if (parsed.username is not None or parsed.password is not None or not parsed.hostname or
            not parsed.port or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('代理地址必须为无认证的 host:port 或 HTTP/HTTPS/SOCKS5 URI。')
    protocol = parsed.scheme or default
    if protocol not in PROTOCOLS:
        raise ValueError('仅支持 auto、HTTP、HTTPS 和 SOCKS5 代理。')
    return dict(host=_normal_host(parsed.hostname), port=parsed.port, protocol=protocol)


def system_endpoints(setting):
    if not setting.get('enabled') or setting.get('pac'):
        return []
    endpoints = []
    for item in (setting.get('server') or '').split(';'):
        if not item.strip():
            continue
        key = 'http'
        if '=' in item:
            key, item = item.split('=', 1)
            key = key.strip().lower()
            if key not in ('http', 'https', 'socks', 'socks5'):
                continue
        try:
            # WinINET's https= describes the request scheme, not TLS transport.
            endpoint = _endpoint(item.strip(), 'socks5' if key in ('socks', 'socks5') else 'http')
            if endpoint not in endpoints:
                endpoints.append(endpoint)
        except ValueError:
            continue
    return endpoints


def system_endpoint(setting):
    endpoints = system_endpoints(setting)
    return (endpoints[0]['host'], endpoints[0]['port']) if len(endpoints) == 1 else None


def select_candidate(candidates, setting, explicit_family=False):
    candidates = [row for row in candidates if row.get('verified', True)]
    if not candidates:
        return None, '尚无通过 WSL 协议转发验证的代理；显式系统端点可选择后再验证外网。'
    endpoints = system_endpoints(setting)
    matches = [row for row in candidates if any(
        int(row['port']) == endpoint['port'] and
        _bind_matches(row.get('address', row['host']), endpoint['host']) and
        row.get('protocol', 'http') == endpoint['protocol'] for endpoint in endpoints)]
    if len(matches) == 1:
        return matches[0], '匹配 Windows 当前系统代理的地址和端口。'
    if len(candidates) == 1 and (explicit_family or not setting.get('enabled') and not setting.get('pac')):
        return candidates[0], '只有一个通过验证的候选。'
    return None, '存在 PAC、系统代理不匹配或多个可用端点，请明确选择。'


class WindowsEcho:
    """Short-lived Windows loopback echo owned by this operation."""
    def __enter__(self):
        self.stop = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.listener.settimeout(.2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
                with conn:
                    conn.settimeout(1)
                    data = b''
                    while len(data) < 256 and not data.endswith(b'\n'):
                        part = conn.recv(256 - len(data))
                        if not part:
                            break
                        data += part
                    if data:
                        conn.sendall(data)
            except OSError:
                continue

    def __exit__(self, *args):
        self.stop.set()
        self.listener.close()
        self.thread.join(1.5)


def _candidate(row, app=None, protocol='auto', source='listener'):
    address = row['address']
    host = '127.0.0.1' if address == '0.0.0.0' else '::1' if address == '::' else address
    return dict(row, app=app or application(row), host=host, protocol=protocol,
                source=source, endpoint_kind='local')


def _probe(distro, user, rows, allow_external=False, coverage=None, keep_explicit=False):
    if not rows:
        return [], []
    with WindowsEcho() as echo:
        result = run_agent(distro, user, dict(action='probe', candidates=rows, echo_port=echo.port,
                           allow_external=allow_external, budget=18, max_workers=8), timeout=30)
    if coverage is not None:
        reported = result.get('coverage', {})
        coverage['completed'] = coverage.get('completed', 0) + reported.get('completed', len(result['results']))
        coverage['pending'] = coverage.get('pending', 0) + reported.get('pending', 0)
    candidates = [row for row in result['results'] if row['verified'] or
                  keep_explicit and row.get('source') == 'system']
    issues = [f"{row['app']} {row.get('address', row['host'])}:{row['port']}：{row.get('reason', '尚未确认')}"
              for row in result['results'] if not row['verified']]
    return candidates, issues


def _is_local(host, snapshot):
    host = _normal_host(host)
    if host in snapshot.get('local_addresses', []):
        return True
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_unspecified:
            return True
    except ValueError:
        if host == socket.gethostname().lower():
            return True
    return any(row['address'] == host for row in snapshot['listeners'])


def _explicit(endpoint, snapshot, source='manual'):
    host, port = endpoint['host'], endpoint['port']
    local = _is_local(host, snapshot)
    matches = [row for row in snapshot['listeners'] if int(row['port']) == port and
               local and _bind_matches(row['address'], host)]
    if len(matches) == 1:
        row = _candidate(matches[0], '手动' if source == 'manual' else None,
                         endpoint['protocol'], source)
        ensure_owner(row, snapshot)
        return row
    if matches or local:
        raise RuntimeError('指定地址没有唯一、可确认的 Windows 监听进程。')
    return dict(endpoint, address=host, app='手动' if source == 'manual' else '系统代理',
                process='', source=source, endpoint_kind='remote')


def scan(distro, user, extended=False):
    _target(distro, user)
    snapshot = windows_inventory()
    rows, priority, issues = [], [], []
    setting = snapshot.get('system_proxy') or {}
    unique = {identity(row): row for row in snapshot['listeners']}
    for row in unique.values():
        try:
            ensure_owner(row, snapshot)
            ipaddress.ip_address(row['address'])
            rows.append(_candidate(row))
        except (RuntimeError, ValueError) as error:
            issues.append(str(error))
    for endpoint in system_endpoints(setting):
        try:
            priority.append(_explicit(endpoint, snapshot, 'system'))
        except RuntimeError as error:
            issues.append(str(error))
    explicit_ids = {identity(row) for row in priority if row.get('pid')}
    # Core names affect scheduling only; every other listener remains eligible.
    for row in rows:
        if identity(row) not in explicit_ids and any(hint in (row.get('process') or '').lower()
                for hint in ('mihomo', 'clash', 'sing-box', 'xray', 'v2ray', 'sakuracat', 'vortex')):
            priority.append(dict(row, source='core_hint'))
    priority_ids = {identity(row) for row in priority if row.get('pid')}
    remaining = [row for row in rows if identity(row) not in priority_ids]
    coverage = dict(total=len(priority) + len(remaining) + len(unique) - len(rows),
                    completed=0, pending=0)
    candidates, failures = _probe(distro, user, priority, coverage=coverage, keep_explicit=True)
    full = extended or not any(row.get('verified') for row in candidates)
    if full:
        found, more = _probe(distro, user, remaining, coverage=coverage)
        candidates += found
        failures += more
    coverage.update(scope='all_listeners' if full else 'priority',
                    unconfirmed=max(0, coverage['total'] - coverage['completed']))
    coverage['partial'] = bool(coverage['unconfirmed'] or coverage['pending'])
    if setting.get('pac'):
        issues.append('检测到 PAC 自动配置脚本，请手动指定无认证的代理地址。')
    if setting.get('enabled') and setting.get('server') and not system_endpoints(setting) and not setting.get('pac'):
        issues.append('系统代理地址使用了不支持的格式或需要认证，请手动指定无认证的代理地址。')
    if not any(row.get('verified') for row in candidates):
        issues.append('未确认可用代理；仅 TUN/VPN 模式须由其自身提供 WSL 路由，或启用显式 HTTP/SOCKS5 入口。NAT 模式需要可达监听地址；不会自动开放防火墙。')
    selected, reason = select_candidate(candidates, setting)
    safe_endpoints = []
    for endpoint in system_endpoints(setting):
        host = endpoint['host']
        host = '[' + host + ']' if ':' in host else host
        safe_endpoints.append(f"{endpoint['protocol']}://{host}:{endpoint['port']}")
    safe_setting = dict(enabled=bool(setting.get('enabled')), pac=bool(setting.get('pac')),
                        server=';'.join(safe_endpoints))
    return dict(candidates=candidates, selected=selected, reason=reason, issues=issues + failures,
                system_proxy=safe_setting, coverage=coverage)


def connect(distro, user, candidate=None, family=None, codex=False):
    _target(distro, user)
    if candidate is None:
        result = scan(distro, user)
        rows = [row for row in result['candidates'] if not family or row['app'] == family]
        if family:
            chosen, reason = select_candidate(rows, result.get('system_proxy') or {}, True)
        else:
            chosen, reason = result['selected'], result['reason']
        if not chosen:
            if rows:
                raise SelectionRequired(rows, reason)
            raise RuntimeError('没有可连接的已验证代理。' + ' '.join(result['issues']))
        candidate = chosen
    snapshot = windows_inventory()
    if not candidate.get('pid'):
        protocol = candidate.get('protocol', 'auto')
        host = candidate.get('host', '127.0.0.1')
        if '://' in host:
            endpoint = _endpoint(host)
        else:
            if protocol not in PROTOCOLS:
                raise ValueError('仅支持 auto、HTTP、HTTPS 和 SOCKS5 代理。')
            bracketed = '[' + host + ']' if ':' in host else host
            endpoint = _endpoint(f"{bracketed}:{candidate['port']}", protocol)
        candidate = _explicit(endpoint, snapshot, 'system' if candidate.get('source') == 'system' else 'manual')
    else:
        candidate = dict(candidate, protocol=candidate.get('protocol', 'auto'),
                         source=candidate.get('source', 'listener'))
    if candidate.get('endpoint_kind') != 'remote':
        ensure_owner(candidate, snapshot)
    valid, issues = _probe(distro, user, [candidate],
                          allow_external=candidate['source'] in ('system', 'manual'))
    if not valid:
        raise RuntimeError('应用前协议复查失败：' + ' '.join(issues))
    candidate = valid[0]
    if candidate.get('endpoint_kind') != 'remote':
        ensure_owner(candidate, windows_inventory())
    upstream = {key: candidate[key] for key in ('host', 'port', 'protocol')}
    result = run_agent(distro, user, dict(action='connect_gateway', upstream=upstream, codex=codex))
    return dict(result, candidate=candidate)


def disable(distro, user, codex=False):
    return run_agent(distro, user, dict(action='apply', url=None, codex=codex))


def rollback(distro, user):
    return run_agent(distro, user, dict(action='rollback'))


def diagnose(distro, user):
    status = run_agent(distro, user, dict(action='status'))
    gateway = run_agent(distro, user, dict(action='gateway_status'))
    message = status['message']
    url = status.get('url') or gateway.get('url')
    if status.get('configured') and url and gateway.get('running'):
        message += '\nWSL 当前入口：' + url
        message += '\n' + run_agent(distro, user, dict(action='https', url=url), timeout=25)['message']
    else:
        message += '\n当前没有运行中的工具管理入口，未执行外网测试。'
    return dict(message=message, gateway=gateway, configured=status.get('configured', False),
                candidates=[], issues=[])


def check_apps(distro, user):
    return run_agent(distro, user, dict(action='apps'), timeout=150)


def open_terminal(distro, user):
    _target(distro, user)
    # No -e: WSL starts this user's real default shell and its startup files.
    subprocess.Popen(['wsl.exe', '--distribution', distro, '--user', user],
                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
