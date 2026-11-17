"""Narrow file-based IPC for user-approved elevated disk tasks."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def dispatch(request):
    from . import disks
    action = request.get('action')
    if action == 'inspect':
        return disks.inspect_disk(request['disk'])
    if action == 'cleanup':
        return disks.close_and_eject(request['disk'], request['inspection'],
                                     selected_services=request.get('selected_services', []))
    raise ValueError('不支持的硬盘操作。')


def result_path(request_path):
    return Path(request_path).with_suffix('.result.json')


def read_result(request_path):
    return json.loads(result_path(request_path).read_text(encoding='utf-8'))


def task_file(request_path):
    """Internal entry. The output path is derived, never supplied as an operation."""
    request_path = Path(request_path)
    try:
        result = {'result': dispatch(json.loads(request_path.read_text(encoding='utf-8')))}
        code = 0
    except Exception as error:
        result, code = {'error': str(error)}, 1
    result_path(request_path).write_text(json.dumps(result, ensure_ascii=True), encoding='utf-8')
    return code


def _elevate_and_wait(request_path):
    from ctypes import wintypes as W
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', W.DWORD), ('fMask', W.ULONG), ('hwnd', W.HWND),
                    ('lpVerb', W.LPCWSTR), ('lpFile', W.LPCWSTR), ('lpParameters', W.LPCWSTR),
                    ('lpDirectory', W.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', W.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', W.LPCWSTR), ('hkeyClass', W.HKEY),
                    ('dwHotKey', W.DWORD), ('hIcon', W.HANDLE), ('hProcess', W.HANDLE)]
    args = ['--internal-disk-task', str(request_path)]
    if not getattr(sys, 'frozen', False):
        args.insert(0, str(Path(__file__).resolve().parent.parent / 'start.py'))
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ole = ctypes.WinDLL('ole32')
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = W.BOOL
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.WaitForSingleObject.restype = W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    info = ShellExecuteInfo()
    info.cbSize, info.fMask = ctypes.sizeof(info), 0x40 | 0x100
    info.lpVerb, info.lpFile = 'runas', sys.executable
    info.lpParameters, info.nShow = subprocess.list2cmdline(args), 0
    info.lpDirectory = str(Path(request_path).parent)
    initialized = ole.CoInitializeEx(None, 2) in (0, 1)
    try:
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            code = ctypes.get_last_error()
            if code == 1223:
                raise RuntimeError('已取消管理员授权，未执行该操作。')
            raise OSError(code, '无法启动管理员硬盘助手')
        try:
            # Each task's native operations have their own deadlines. Never abandon a
            # live cleanup helper and let the user accidentally start a second one.
            if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) != 0:
                raise RuntimeError('无法确认管理员助手是否结束；请查看系统状态。')
        finally:
            kernel.CloseHandle(info.hProcess)
    finally:
        if initialized:
            ole.CoUninitialize()


def run_elevated(request):
    if os.name != 'nt':
        raise RuntimeError('管理员硬盘扫描仅支持 Windows。')
    with tempfile.TemporaryDirectory(prefix='wsl-disk-task-') as folder:
        path = Path(folder) / 'request.json'
        path.write_text(json.dumps(request, ensure_ascii=True), encoding='utf-8')
        # Preserve the invoking user's ACL when the elevated helper writes back.
        result_path(path).write_text('{}', encoding='utf-8')
        _elevate_and_wait(path)
        try:
            result = read_result(path)
        except (OSError, ValueError) as error:
            raise RuntimeError('管理员助手没有返回有效结果。') from error
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result['result']
