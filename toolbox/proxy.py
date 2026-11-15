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

from .wsl_agent import probe_http


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
[pscustomobject]@{listeners=$rows;system_proxy=[pscustomobject]@{
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


def _target(distro, user):
    if not distro or not user:
        raise ValueError('请明确选择 WSL 发行版和 Linux 用户；不会自动修改 root。')


def run_agent(distro, user, request, timeout=60):
    _target(distro, user)
    # PyInstaller includes toolbox/wsl_agent.py as package data under _MEIPASS.
    payload = Path(__file__).with_name('wsl_agent.py').read_bytes()
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
    if 'service' in name:
        return None
    if name.startswith(('verge-mihomo', 'verge-clash')):
        return 'Clash Verge'
    if ('mihomo' in name or name in ('clash', 'clash-meta', 'clash-premium', 'clash-core')) and any(
            word in source for word in ('clash-verge', 'clash_verge', 'clash verge')):
        return 'Clash Verge'
    return None


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


def system_endpoint(setting):
    if not setting.get('enabled') or setting.get('pac'):
        return None
    endpoints = []
    for item in (setting.get('server') or '').split(';'):
        if not item.strip():
            continue
        if '=' in item:
            key, item = item.split('=', 1)
            if key.strip().lower() not in ('http', 'https'):
                continue
        try:
            parsed = urlsplit(item.strip() if '://' in item else '//' + item.strip())
            if (parsed.username or parsed.password or not parsed.hostname or not parsed.port or
                    parsed.scheme not in ('', 'http', 'https')):
                return None
            endpoints.append((_normal_host(parsed.hostname), parsed.port))
        except ValueError:
            return None
    return endpoints[0] if endpoints and len(set(endpoints)) == 1 else None


def select_candidate(candidates, setting, explicit_family=False):
    if not candidates:
        return None, '没有通过 WSL HTTP CONNECT 验证的代理。'
    endpoint = system_endpoint(setting)
    matches = [row for row in candidates if endpoint and int(row['port']) == endpoint[1] and
               _bind_matches(row['address'], endpoint[0])]
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


def _candidate(row, app=None):
    address = row['address']
    host = '127.0.0.1' if address == '0.0.0.0' else '::1' if address == '::' else address
    return dict(row, app=app or application(row), host=host)


def _probe(distro, user, rows):
    if not rows:
        return [], []
    with WindowsEcho() as echo:
        result = run_agent(distro, user, dict(action='probe', candidates=rows, echo_port=echo.port),
                           timeout=max(25, 20 * len(rows)))
    candidates = [row for row in result['results'] if row['verified']]
    issues = [f"{row['app']} {row['address']}:{row['port']}：{row['reason']}。NAT 模式需要代理监听主机可达地址；不会自动开放防火墙。"
              for row in result['results'] if not row['verified']]
    return candidates, issues


def scan(distro, user):
    _target(distro, user)
    snapshot = windows_inventory()
    rows, issues = [], []
    unique = {identity(row): row for row in snapshot['listeners'] if application(row)}
    for row in unique.values():
        try:
            ensure_owner(row, snapshot)
            rows.append(_candidate(row))
        except RuntimeError as error:
            issues.append(str(error))
    candidates, failures = _probe(distro, user, rows)
    selected, reason = select_candidate(candidates, snapshot.get('system_proxy') or {})
    return dict(candidates=candidates, selected=selected, reason=reason, issues=issues + failures,
                system_proxy=snapshot.get('system_proxy') or {})


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
        host, port = candidate.get('host', '127.0.0.1'), int(candidate['port'])
        if not 1 <= port <= 65535:
            raise ValueError('端口必须在 1 到 65535 之间。')
        matches = [row for row in snapshot['listeners'] if int(row['port']) == port and _bind_matches(row['address'], host)]
        if len(matches) != 1:
            raise RuntimeError('手动端口没有唯一、可确认的 Windows 监听进程。')
        candidate = _candidate(matches[0], candidate.get('app', '手动'))
    ensure_owner(candidate, snapshot)
    valid, issues = _probe(distro, user, [candidate])
    if not valid:
        raise RuntimeError('应用前协议复查失败：' + ' '.join(issues))
    candidate = valid[0]
    ensure_owner(candidate, windows_inventory())
    host = '[' + candidate['host'] + ']' if ':' in candidate['host'] else candidate['host']
    result = run_agent(distro, user, dict(action='apply', url=f"http://{host}:{candidate['port']}", codex=codex))
    return dict(result, candidate=candidate)


def disable(distro, user, codex=False):
    return run_agent(distro, user, dict(action='apply', url=None, codex=codex))


def rollback(distro, user):
    return run_agent(distro, user, dict(action='rollback'))


def diagnose(distro, user):
    status = run_agent(distro, user, dict(action='status'))
    result = scan(distro, user)
    message = status['message'] + '\n' + result['reason']
    if result['selected']:
        candidate = result['selected']
        host = '[' + candidate['host'] + ']' if ':' in candidate['host'] else candidate['host']
        message += '\n' + run_agent(distro, user, dict(action='https', url=f"http://{host}:{candidate['port']}"), timeout=25)['message']
    else:
        message += '\n未选择唯一可用代理，未执行 https://example.com 的外网测试。'
    if result['issues']:
        message += '\n' + '\n'.join(result['issues'])
    return dict(message=message, candidates=result['candidates'], issues=result['issues'])


def open_terminal(distro, user):
    _target(distro, user)
    # No -e: WSL starts this user's real default shell and its startup files.
    subprocess.Popen(['wsl.exe', '--distribution', distro, '--user', user],
                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
