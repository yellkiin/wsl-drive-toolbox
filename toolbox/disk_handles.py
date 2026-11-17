"""Read-only Windows File-handle attribution in a disposable worker process.

Class 64 layouts follow PHNT ntexapi.h. Native name queries can block inside
filesystem drivers; only the helper calls them, and the parent enforces its
wall-clock budget. No source handle is closed and no process is stopped.
"""
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

U32 = C.c_uint32
U16 = C.c_uint16
UPTR = C.c_size_t
HANDLE = C.c_void_p


class SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX(C.Structure):
    _fields_ = [('Object', HANDLE), ('UniqueProcessId', UPTR), ('HandleValue', UPTR),
                ('GrantedAccess', U32), ('CreatorBackTraceIndex', U16),
                ('ObjectTypeIndex', U16), ('HandleAttributes', U32), ('Reserved', U32)]


class UNICODE_STRING(C.Structure):
    _fields_ = [('Length', U16), ('MaximumLength', U16), ('Buffer', HANDLE)]


def parse_snapshot(data):
    header = C.sizeof(UPTR) * 2
    if len(data) < header:
        raise ValueError('Truncated handle snapshot header')
    count = UPTR.from_buffer_copy(data).value
    size = C.sizeof(SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX)
    if count > (len(data) - header) // size:
        raise ValueError('Truncated handle snapshot entries')
    return (SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX * count).from_buffer_copy(data, header)


def decode_unicode_string(info):
    if not info.Buffer or not info.Length:
        return ''
    if info.Length % 2 or info.Length > info.MaximumLength:
        raise ValueError('Invalid native Unicode string length')
    return C.string_at(info.Buffer, info.Length).decode('utf-16-le', 'replace')


def build_aliases(disk, query_device):
    """Map actual volume / physical-drive DOS devices, never volume-number guesses."""
    aliases = []
    def add(root, display, kind):
        item = (root.rstrip('\\'), display.rstrip('\\'), kind)
        if item not in aliases:
            aliases.append(item)
    letters = list(disk.get('letters', []))
    for volume in disk.get('volumes', []):
        roots = volume.get('letters', [])
        letters.extend(roots)
        volume_id = volume.get('id', '').rstrip('\\')
        display = next(iter(roots), volume_id)
        if volume_id:
            device = volume_id[4:] if volume_id.startswith(('\\\\?\\', '\\??\\')) else volume_id
            for native in query_device(device):
                add(native, display, 'volume')
            add(volume_id, display, 'volume')
            if device.startswith('Volume{'):
                add('\\??\\' + device, display, 'volume')
    for letter in dict.fromkeys(letters):
        letter = letter.rstrip('\\')
        for native in query_device(letter):
            add(native, letter, 'volume')
        add(letter, letter, 'volume')
        add('\\??\\' + letter, letter, 'volume')
    number = disk.get('number')
    if isinstance(number, int) and number >= 0:
        device = f'PhysicalDrive{number}'
        display = '\\\\.\\' + device
        for native in query_device(device):
            add(native, display, 'physical_disk')
        add(display, display, 'physical_disk')
        add('\\??\\' + device, display, 'physical_disk')
    return sorted(aliases, key=lambda item: len(item[0]), reverse=True)


def match_name(name, aliases, directory=False):
    name = name.rstrip('\\')
    lower = name.casefold()
    for root, display, kind in aliases:
        if lower == root.casefold():
            return {'path': display + ('\\' if kind == 'volume' else ''), 'kind': kind}
        if kind == 'volume' and lower.startswith(root.casefold() + '\\'):
            return {'path': display + name[len(root):], 'kind': 'directory' if directory else 'file'}
    return None


def _coverage():
    return {'supported': os.name == 'nt', 'completed': False, 'partial': True,
            'timed_out': False, 'admin': False, 'debug_privilege': False,
            'total_handles': 0, 'file_handles': 0, 'scanned_handles': 0,
            'denied_processes': 0, 'denied_pids': [], 'duplicate_denied': 0,
            'unresolved_names': 0, 'unresolved_processes': 0, 'skipped_non_disk': 0}


def _worker_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--internal-handle-worker']
    return [sys.executable, '-m', 'toolbox.disk_handles', '--worker']


def _warnings(coverage):
    warnings = ['句柄诊断不包含完整的内核或映射文件归因；最终以 Windows 安全移除结果为准。']
    if coverage.get('timed_out'):
        warnings.append('句柄扫描超时，已保留此前发现的占用；结果不完整。')
    if coverage.get('denied_processes') or coverage.get('duplicate_denied'):
        warnings.append(f"{coverage.get('denied_processes', 0)} 个进程无法读取句柄，{coverage.get('duplicate_denied', 0)} 个句柄无法复制；受保护进程可能仍有占用。")
    if coverage.get('unresolved_names') or coverage.get('unresolved_processes'):
        warnings.append(f"{coverage.get('unresolved_names', 0)} 个文件句柄名称未解析，{coverage.get('unresolved_processes', 0)} 个进程身份未解析。")
    if not coverage.get('admin'):
        warnings.append('当前扫描未使用管理员权限；管理员扫描可能读取更多服务进程。')
    return warnings


def scan(disk, timeout=18):
    """Bound the entire native scan and retain flushed matches on timeout.

    File IPC also works in PyInstaller windowed builds without stdio handles.
    """
    result = {'processes': [], 'coverage': _coverage(), 'warnings': []}
    records = {}
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        with tempfile.TemporaryDirectory(prefix='wsl-handles-') as directory:
            request, output = Path(directory) / 'request.json', Path(directory) / 'progress.jsonl'
            request.write_text(json.dumps(disk, ensure_ascii=True), encoding='utf-8')
            output.write_text('', encoding='utf-8')
            worker = subprocess.Popen(_worker_command() + [str(request), str(output)],
                                      cwd=str(Path(__file__).resolve().parent.parent),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, env=env, creationflags=flags)
            deadline = time.monotonic() + max(0.05, timeout)
            pending = ''
            with output.open('r', encoding='utf-8') as stream:
                def drain():
                    nonlocal pending
                    pending += stream.read()
                    complete = pending.split('\n')
                    pending = complete.pop()
                    for line in complete:
                        _consume_line(line, result, records)
                try:
                    while True:
                        drain()
                        if worker.poll() is not None:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            result['coverage']['timed_out'] = True
                            break
                        time.sleep(min(0.025, remaining))
                finally:
                    if worker.poll() is None:
                        worker.kill()
                    worker.wait(timeout=3)
                    drain()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result['warnings'].append(f'句柄扫描未完成：{error}')
    result['processes'] = list(records.values())
    coverage = result['coverage']
    if coverage['timed_out'] or not coverage.get('completed'):
        coverage['partial'] = True
        if not coverage['timed_out']:
            result['warnings'].append('句柄扫描进程未完成；结果不完整。')
    result['warnings'] = list(dict.fromkeys(result['warnings'] + _warnings(coverage)))
    return result


def _consume_line(line, result, records):
    try:
        item = json.loads(line)
    except (ValueError, TypeError):
        return
    if item.get('type') == 'process':
        process = item['process']
        records[process['pid']] = process
    elif item.get('type') == 'progress':
        timed_out = result['coverage'].get('timed_out', False)
        result['coverage'].update(item['coverage'])
        result['coverage']['timed_out'] |= timed_out
    elif item.get('type') == 'result':
        payload = item['result']
        timed_out = result['coverage']['timed_out']
        result['coverage'].update(payload['coverage'])
        result['coverage']['timed_out'] |= timed_out
        result['warnings'].extend(payload.get('warnings', []))
        for process in payload.get('processes', []):
            records[process['pid']] = process


class _Native:
    def __init__(self):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.nt = C.WinDLL('ntdll')
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        signatures = {
            'GetCurrentProcess': ([], HANDLE), 'OpenProcess': ([U32, C.c_int, U32], HANDLE),
            'CloseHandle': ([HANDLE], C.c_int),
            'CreateFileW': ([C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE], HANDLE),
            'DuplicateHandle': ([HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), U32, C.c_int, U32], C.c_int),
            'GetFileType': ([HANDLE], U32),
            'QueryDosDeviceW': ([C.c_wchar_p, C.c_void_p, U32], U32),
            'GetProcessTimes': ([HANDLE, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p], C.c_int),
            'QueryFullProcessImageNameW': ([HANDLE, U32, C.c_void_p, C.POINTER(U32)], C.c_int),
            'GetFileInformationByHandleEx': ([HANDLE, C.c_int, C.c_void_p, U32], C.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.nt.NtQuerySystemInformation.argtypes = [U32, C.c_void_p, U32, C.POINTER(U32)]
        self.nt.NtQuerySystemInformation.restype = C.c_int32
        self.nt.NtQueryObject.argtypes = [HANDLE, U32, C.c_void_p, U32, C.POINTER(U32)]
        self.nt.NtQueryObject.restype = C.c_int32
        self.current = self.kernel.GetCurrentProcess()

    def privileges(self):
        class LUID(C.Structure):
            _fields_ = [('LowPart', U32), ('HighPart', C.c_int32)]
        class TOKEN_PRIVILEGES(C.Structure):
            _fields_ = [('PrivilegeCount', U32), ('Luid', LUID), ('Attributes', U32)]
        adv = self.advapi
        adv.OpenProcessToken.argtypes = [HANDLE, U32, C.POINTER(HANDLE)]
        adv.OpenProcessToken.restype = C.c_int
        adv.GetTokenInformation.argtypes = [HANDLE, U32, C.c_void_p, U32, C.POINTER(U32)]
        adv.GetTokenInformation.restype = C.c_int
        adv.LookupPrivilegeValueW.argtypes = [C.c_wchar_p, C.c_wchar_p, C.POINTER(LUID)]
        adv.LookupPrivilegeValueW.restype = C.c_int
        adv.AdjustTokenPrivileges.argtypes = [HANDLE, C.c_int, C.c_void_p, U32, C.c_void_p, C.c_void_p]
        adv.AdjustTokenPrivileges.restype = C.c_int
        token = HANDLE()
        if not adv.OpenProcessToken(self.current, 0x28, C.byref(token)):
            return False, False
        try:
            elevated, required = U32(), U32()
            admin = bool(adv.GetTokenInformation(token, 20, C.byref(elevated), 4, C.byref(required)) and elevated.value)
            if not admin:
                return False, False
            privileges = TOKEN_PRIVILEGES(1, LUID(), 2)
            if not adv.LookupPrivilegeValueW(None, 'SeDebugPrivilege', C.byref(privileges.Luid)):
                return admin, False
            C.set_last_error(0)
            changed = adv.AdjustTokenPrivileges(token, False, C.byref(privileges), 0, None, None)
            return admin, bool(changed and C.get_last_error() == 0)
        finally:
            self.kernel.CloseHandle(token)

    def query_device(self, device):
        size = 1024
        while size <= 65536:
            buffer = C.create_unicode_buffer(size)
            count = self.kernel.QueryDosDeviceW(device, buffer, size)
            if count:
                return buffer[:count].rstrip('\x00').split('\x00')
            if C.get_last_error() != 122:
                return []
            size *= 2
        return []

    def snapshot(self):
        # Frozen modules live inside PYZ; their synthetic __file__ is not a file.
        calibration = sys.executable if getattr(sys, 'frozen', False) else __file__
        known = self.kernel.CreateFileW(str(Path(calibration).resolve()), 0x80000000, 7, None, 3, 0, None)
        if known in (None, C.c_void_p(-1).value):
            raise OSError('Cannot open File-type calibration handle')
        try:
            size = 1024 * 1024
            while size <= 256 * 1024 * 1024:
                data, needed = C.create_string_buffer(size), U32()
                status = self.nt.NtQuerySystemInformation(64, data, size, C.byref(needed))
                if status == 0:
                    rows = parse_snapshot(data.raw[:needed.value or size])
                    for row in rows:
                        if row.UniqueProcessId == os.getpid() and row.HandleValue == known:
                            return rows, row.ObjectTypeIndex
                    raise OSError('File object type calibration absent from snapshot')
                if status & 0xffffffff not in (0xC0000004, 0xC0000023):
                    raise OSError(f'NtQuerySystemInformation failed: 0x{status & 0xffffffff:08X}')
                size = max(size * 2, needed.value + 65536)
            raise OSError('Handle snapshot exceeds memory limit')
        finally:
            self.kernel.CloseHandle(known)

    def process_record(self, pid, process):
        times = [C.c_uint64() for _ in range(4)]
        if not self.kernel.GetProcessTimes(process, *(C.byref(t) for t in times)):
            return None
        path, count = C.create_unicode_buffer(32768), U32(32768)
        executable = path.value if self.kernel.QueryFullProcessImageNameW(process, 0, path, C.byref(count)) else ''
        return {'pid': pid, 'created': times[0].value, 'name': executable.rsplit('\\', 1)[-1] or f'PID {pid}',
                'executable': executable, 'evidence': [], 'handle_count': 0}

    def object_name(self, handle):
        size = 1024
        while size <= 1024 * 1024:
            data, needed = C.create_string_buffer(size), U32()
            status = self.nt.NtQueryObject(handle, 1, data, size, C.byref(needed))
            if status == 0:
                return decode_unicode_string(UNICODE_STRING.from_buffer(data))
            if status & 0xffffffff not in (0xC0000004, 0xC0000023, 0x80000005):
                return None
            size = max(size * 2, needed.value)
        return None

    def is_directory(self, handle):
        class FILE_STANDARD_INFO(C.Structure):
            _fields_ = [('AllocationSize', C.c_int64), ('EndOfFile', C.c_int64),
                        ('NumberOfLinks', U32), ('DeletePending', C.c_ubyte), ('Directory', C.c_ubyte)]
        info = FILE_STANDARD_INFO()
        return bool(self.kernel.GetFileInformationByHandleEx(handle, 1, C.byref(info), C.sizeof(info)) and info.Directory)


def _scan_native(disk, emit):
    coverage = _coverage()
    result = {'processes': [], 'coverage': coverage, 'warnings': []}
    if os.name != 'nt':
        result['warnings'].append('原生文件句柄扫描仅支持 Windows。')
        return result
    native = _Native()
    coverage['admin'], coverage['debug_privilege'] = native.privileges()
    aliases = build_aliases(disk, native.query_device)
    rows, file_type = native.snapshot()
    coverage['total_handles'] = len(rows)
    grouped = {}
    for row in rows:
        if row.ObjectTypeIndex == file_type and row.UniqueProcessId != os.getpid():
            grouped.setdefault(row.UniqueProcessId, []).append(row)
    coverage['file_handles'] = sum(map(len, grouped.values()))
    last_progress = 0.0
    def progress():
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress >= 0.1:
            emit({'type': 'progress', 'coverage': coverage})
            last_progress = now
    progress()
    for pid, entries in grouped.items():
        process = native.kernel.OpenProcess(0x40 | 0x1000, False, pid)
        if not process:
            coverage['denied_pids'].append(pid)
            coverage['denied_processes'] += 1
            progress()
            continue
        try:
            record = native.process_record(pid, process)
            if record is None:
                coverage['unresolved_processes'] += 1
                continue
            for entry in entries:
                # Publish recent counters before potentially blocking filesystem queries.
                duplicate = HANDLE()
                if not native.kernel.DuplicateHandle(process, HANDLE(entry.HandleValue), native.current,
                                                     C.byref(duplicate), 0, False, 2):
                    coverage['duplicate_denied'] += 1
                    continue
                try:
                    coverage['scanned_handles'] += 1
                    if native.kernel.GetFileType(duplicate) != 1:  # FILE_TYPE_DISK only; pipes/characters can hang.
                        coverage['skipped_non_disk'] += 1
                        continue
                    progress()
                    name = native.object_name(duplicate)
                    if not name:
                        coverage['unresolved_names'] += 1
                        continue
                    match = match_name(name, aliases)
                    if match:
                        if match['kind'] == 'file' and native.is_directory(duplicate):
                            match['kind'] = 'directory'
                        evidence = dict(source='handle', **match)
                        record['handle_count'] += 1
                        if evidence not in record['evidence']:
                            record['evidence'].append(evidence)
                        emit({'type': 'process', 'process': record})
                finally:
                    native.kernel.CloseHandle(duplicate)
            if record['evidence']:
                result['processes'].append(record)
        finally:
            native.kernel.CloseHandle(process)
    coverage['completed'] = True
    coverage['partial'] = bool(coverage['denied_processes'] or coverage['duplicate_denied']
                               or coverage['unresolved_names'] or coverage['unresolved_processes'])
    return result


def worker_main(request_path=None, output_path=None):
    output = open(output_path, 'w', encoding='utf-8') if output_path else sys.stdout
    def emit(item):
        print(json.dumps(item, ensure_ascii=True), file=output, flush=True)
    try:
        if request_path:
            disk = json.loads(Path(request_path).read_text(encoding='utf-8'))
        else:
            disk = json.loads(sys.stdin.readline())
        result = _scan_native(disk, emit)
        emit({'type': 'result', 'result': result})
        return 0
    except Exception as error:
        emit({'type': 'result', 'result': {'processes': [], 'coverage': _coverage(),
                                          'warnings': [f'句柄扫描未完成：{error}']}})
        return 1
    finally:
        if output_path:
            output.close()


if __name__ == '__main__':
    raise SystemExit(worker_main(*(sys.argv[2:4] if len(sys.argv) >= 4 else [])))
