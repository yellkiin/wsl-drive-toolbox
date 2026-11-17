#!/usr/bin/env python3
"""User-owned loopback HTTP gateway. No credentials, TLS interception or logs."""
from collections import OrderedDict
import json
import ipaddress
import os
from pathlib import Path
import secrets
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


_children = []

CONFIG = '.config/wsl-drive-toolbox/gateway.json'
HELPER = '.local/lib/wsl-drive-toolbox/gateway.py'
RUNTIME = '.local/state/wsl-drive-toolbox/gateway-runtime.json'


def receive_exact(sock, size, deadline=None):
    data = b''
    while len(data) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('代理验证超时')
            sock.settimeout(remaining)
        part = sock.recv(size - len(data))
        if not part:
            raise OSError('代理响应不完整')
        data += part
    return data


def receive_header(sock, deadline=None):
    data = b''
    while not data.endswith(b'\r\n\r\n'):
        if len(data) >= 65536:
            raise OSError('HTTP 响应头过长')
        data += receive_exact(sock, 1, deadline)
    return data


def validate_upstream(upstream):
    host, port = upstream.get('host'), upstream.get('port')
    protocol = upstream.get('protocol', 'http')
    if protocol not in ('http', 'https', 'socks5') or not isinstance(host, str) or not host or any(c.isspace() or c in '/@\r\n' for c in host):
        raise ValueError('代理协议或主机无效。')
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('代理端口无效。')
    return dict(host=host, port=port, protocol=protocol)


def connect_upstream(upstream, timeout):
    upstream = validate_upstream(upstream)
    sock = socket.create_connection((upstream['host'], upstream['port']), timeout=timeout)
    try:
        if upstream['protocol'] == 'https':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=upstream['host'])
        return sock
    except Exception:
        sock.close()
        raise


def authority(host, port):
    return ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def open_tunnel(upstream, host, port, timeout=8):
    """Connect through HTTP CONNECT or SOCKS5 with remote name resolution."""
    upstream = validate_upstream(upstream)
    if not host or any(c in host for c in '\r\n/@') or not 1 <= int(port) <= 65535:
        raise ValueError('目标地址无效。')
    deadline = time.monotonic() + timeout
    sock = connect_upstream(upstream, timeout)
    try:
        if upstream['protocol'] in ('http', 'https'):
            target = authority(host, port).encode('ascii')
            sock.sendall(b'CONNECT ' + target + b' HTTP/1.1\r\nHost: ' + target + b'\r\n\r\n')
            first = receive_header(sock, deadline).split(b'\r\n', 1)[0].split()
            if len(first) < 2 or first[0] not in (b'HTTP/1.0', b'HTTP/1.1') or first[1] != b'200':
                raise OSError('代理要求认证' if len(first) > 1 and first[1] == b'407' else 'HTTP CONNECT 被拒绝')
        else:
            sock.sendall(b'\x05\x01\x00')
            if receive_exact(sock, 2, deadline) != b'\x05\x00':
                raise OSError('SOCKS5 要求认证或不支持无认证连接')
            try:
                address = ipaddress.ip_address(host)
                target = bytes([1 if address.version == 4 else 4]) + address.packed
            except ValueError:
                name = host.encode('idna')
                if len(name) > 255:
                    raise ValueError('目标主机名过长')
                target = b'\x03' + bytes([len(name)]) + name
            sock.sendall(b'\x05\x01\x00' + target + struct.pack('!H', int(port)))
            response = receive_exact(sock, 4, deadline)
            if response[:3] != b'\x05\x00\x00':
                raise OSError('SOCKS5 CONNECT 被拒绝')
            size = {1: 4, 4: 16}.get(response[3])
            if response[3] == 3:
                size = receive_exact(sock, 1, deadline)[0]
            if size is None:
                raise OSError('SOCKS5 地址类型无效')
            receive_exact(sock, size + 2, deadline)
        return sock
    except Exception:
        sock.close()
        raise


def load_config(home):
    path = Path(home) / CONFIG
    return json.loads(path.read_text('utf-8')) if path.exists() else None


def runtime_config(home):
    path = Path(home) / RUNTIME
    return json.loads(path.read_text('utf-8')) if path.exists() else None


def prepare(home, upstream):
    home = Path(home)
    config = load_config(home)
    if config is None:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = dict(port=port, token=secrets.token_hex(24))
    config.update(upstream=validate_upstream(upstream), enabled=True)
    source = globals().get('__source__')
    if source is None:
        source = Path(__file__).read_text('utf-8')
    return dict(url='http://127.0.0.1:' + str(config['port']), files={
        home / CONFIG: json.dumps(config, ensure_ascii=True, indent=2).encode(),
        home / HELPER: source.encode('utf-8')})


def control(config, action, timeout=.4):
    with socket.create_connection(('127.0.0.1', config['port']), timeout=timeout) as sock:
        sock.sendall(('GET /__wsl_toolbox/' + config['token'] + '/' + action + ' HTTP/1.1\r\nHost: localhost\r\n\r\n').encode())
        head = receive_header(sock)
        if not head.startswith(b'HTTP/1.1 200 '):
            raise OSError('非本工具的网关')
        size = next(int(line.split(b':', 1)[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:'))
        data = json.loads(receive_exact(sock, size))
        if data.get('identity') != config['token']:
            raise OSError('非本工具的网关')
        return data


def status(home):
    config = load_config(home) or runtime_config(home)
    url = 'http://127.0.0.1:' + str(config['port']) if config else None
    if config:
        try:
            result = control(config, 'status')
            return dict(running=True, url=url, counts=result['counts'])
        except (OSError, ValueError, StopIteration):
            pass
    return dict(running=False, url=url, counts={})


def stop_config(config):
    if config:
        try:
            control(config, 'stop')
            for _ in range(40):
                try:
                    control(config, 'status', timeout=.1)
                except (OSError, ValueError, StopIteration):
                    return
                time.sleep(.05)
            raise RuntimeError('本地代理入口未能停止。')
        except (OSError, ValueError, StopIteration):
            pass


def stop(home):
    stop_config(load_config(home) or runtime_config(home))
    for child in _children[:]:
        if child.poll() is not None:
            _children.remove(child)
    return status(home)


def ensure(home):
    home = Path(home)
    config = load_config(home)
    if not config or not config.get('enabled', True):
        return dict(running=False, url=None, counts={})
    current = status(home)
    if current['running']:
        return current
    # Match the server's reuse setting so Linux TIME_WAIT is not a false conflict.
    with socket.socket() as sock:
        if os.name != 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', config['port']))
        except OSError as error:
            current = status(home)
            if current['running']:
                return current
            raise RuntimeError('固定本地代理端口已被其他程序占用。') from error
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs['start_new_session'] = True
    process = subprocess.Popen([sys.executable, str(home / HELPER), 'serve', str(home)], **kwargs)
    _children.append(process)
    for _ in range(60):
        current = status(home)
        if current['running']:
            return current
        if process.poll() is not None:
            current = status(home)
            if current['running']:
                return current
            break
        time.sleep(.05)
    raise RuntimeError('无法启动本地代理入口；端口可能被占用或 Python helper 不可用。')


def reconcile(home, previous=None):
    current = load_config(home)
    previous = previous or runtime_config(home)
    if previous and (not current or previous['token'] != current['token'] or previous['port'] != current['port']):
        stop_config(previous)
    if current and current.get('enabled', True):
        return ensure(home)
    if current:
        stop_config(current)
    return status(home)


def relay(left, right):
    peers = {left: right, right: left}
    while peers:
        buffered = [sock for sock in peers if isinstance(sock, ssl.SSLSocket) and sock.pending()]
        readable = buffered or select.select(list(peers), [], [])[0]
        if not readable:
            return
        for sock in readable:
            data = sock.recv(65536)
            if data:
                peers[sock].sendall(data)
            else:
                other = peers.pop(sock)
                try:
                    other.shutdown(socket.SHUT_WR)
                except OSError:
                    pass


class GatewayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, home):
        self.home = Path(home)
        config = load_config(home)
        self.token = config['token']
        self.counts = OrderedDict()
        self.count_lock = threading.Lock()
        super().__init__(('127.0.0.1', config['port']), GatewayHandler)
        runtime = self.home / RUNTIME
        runtime.parent.mkdir(parents=True, exist_ok=True)
        temporary = runtime.with_name(runtime.name + '.' + secrets.token_hex(6))
        try:
            with open(temporary, 'x', encoding='utf-8') as handle:
                os.chmod(temporary, 0o600)
                json.dump(dict(port=config['port'], token=config['token']), handle)
            os.replace(temporary, runtime)
        finally:
            if temporary.exists():
                temporary.unlink()

    def count(self, host):
        host = host.lower()
        with self.count_lock:
            self.counts[host] = self.counts.pop(host, 0) + 1
            if len(self.counts) > 256:
                self.counts.popitem(last=False)


class GatewayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        committed = False
        try:
            request = receive_header(self.request)
            lines = request.decode('iso-8859-1').split('\r\n')
            method, target, version = lines[0].split(' ')
            prefix = '/__wsl_toolbox/' + self.server.token + '/'
            if method == 'GET' and target in (prefix + 'status', prefix + 'stop'):
                with self.server.count_lock:
                    body = json.dumps(dict(identity=self.server.token, counts=dict(self.server.counts))).encode()
                self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                if target.endswith('/stop'):
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            config = load_config(self.server.home)
            if not config or not config.get('enabled', True):
                raise OSError('网关已关闭')
            upstream = config['upstream']
            if method == 'CONNECT':
                parsed = urlsplit('//' + target)
                host, port = parsed.hostname, parsed.port
                remote = open_tunnel(upstream, host, port)
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                committed = True
            else:
                parsed = urlsplit(target)
                if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError('HTTP 代理要求 absolute-form 地址')
                host, port = parsed.hostname, parsed.port or 80
                if upstream['protocol'] == 'socks5':
                    remote = open_tunnel(upstream, host, port)
                    target = parsed.path or '/'
                    if parsed.query:
                        target += '?' + parsed.query
                else:
                    remote = connect_upstream(upstream, 8)
                headers = [line for line in lines[1:] if line and line.split(':', 1)[0].lower() not in ('proxy-authorization', 'proxy-connection', 'connection')]
                remote.sendall((' '.join((method, target, version)) + '\r\n' + '\r\n'.join(headers) + '\r\nConnection: close\r\n\r\n').encode('iso-8859-1'))
            self.server.count(host)
            committed = True
            with remote:
                relay(self.request, remote)
        except (OSError, ValueError, KeyError, TypeError):
            if not committed:
                try:
                    self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass


def serve(home):
    with GatewayServer(home) as server:
        server.serve_forever(poll_interval=.05)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'serve':
        serve(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'ensure':
        try:
            ensure(Path(sys.argv[2]))
        except Exception as error:
            print('WSL proxy: ' + str(error), file=sys.stderr)
            sys.exit(1)
