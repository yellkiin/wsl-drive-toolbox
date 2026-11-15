"""Windows storage inventory and conservative native safe removal.

All handles opened for diagnostics are closed before requesting PnP removal.
No device disabling, forced dismount, handle closure, or process killing.
"""
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


def select_eject_node(nodes):
    """Only storage nodes or their USB function, never a hub/controller."""
    for node in nodes:
        identity = node["id"].upper()
        service = node["service"].lower()
        if "hub" in service or "ROOT_HUB" in identity or identity.startswith("PCI\\"):
            break
        if (node["caps"] & 4 and service in ("disk", "usbstor", "uaspstor")
                and identity.startswith(("USBSTOR\\", "USB\\VID_", "SCSI\\DISK"))):
            return node["id"]
    return None


def _unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def parse_mounts(distro, text, letters, paths=()):
    mounts = []
    roots = [p.replace("/", "\\").rstrip("\\").upper() for p in (*letters, *paths)
             if re.match(r"^[A-Za-z]:", p)]
    def related(path):
        path = path.replace("/", "\\").rstrip("\\").upper()
        return any(path == root or path.startswith(root + "\\") for root in roots)
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 5 or len(filesystem) < 3:
            continue
        source = _unescape_mount(filesystem[1])
        if filesystem[0] not in ("9p", "drvfs"):
            continue
        # WSL1 may put the Windows path in mount options rather than source.
        candidates = [source] + re.findall(r"(?:path=)([A-Za-z]:[^;,\s]*)", _unescape_mount(filesystem[2]))
        if any(related(path) for path in candidates):
            mounts.append({"distro": distro, "target": _unescape_mount(fields[4]),
                           "source": next(p for p in candidates if related(p))})
    return mounts


def _run(args, timeout=30):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"系统命令未完成：{error}") from error
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").replace("\x00", "").strip()
        if message.startswith("#< CLIXML"):
            try:
                root = ET.fromstring(message.partition("\n")[2].strip())
                message = "".join(n.text or "" for n in root.iter() if n.get("S") == "Error")
                message = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m[1], 16)), message).strip()
            except ET.ParseError:
                pass
        raise RuntimeError(message or f"系统命令返回 {result.returncode}")
    return result.stdout


def _ps(script):
    prefix = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode("ascii")
    return json.loads(_run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], 45).decode("utf-8-sig"))


def _as_list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


class WindowsDisks:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("硬盘安全移除功能仅支持 Windows。")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.cm = C.WinDLL("cfgmgr32")
        self.user = C.WinDLL("user32", use_last_error=True)
        self.rm = C.WinDLL("rstrtmgr")
        self.kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        self.kernel.CreateFileW.restype = W.HANDLE
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD,
                                                C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        self.kernel.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
        self.kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]

    def _volume_disks(self, volume):
        handle = self.kernel.CreateFileW(volume.rstrip("\\"), 0, 7, None, 3, 0, None)
        if handle == W.HANDLE(-1).value:
            raise OSError(C.get_last_error(), "不能读取卷的物理映射")
        try:
            data, returned = C.create_string_buffer(65536), W.DWORD()
            if not self.kernel.DeviceIoControl(handle, 0x560000, None, 0, data, len(data), C.byref(returned), None):
                raise OSError(C.get_last_error(), "不能读取卷的物理映射")
            count = int.from_bytes(data.raw[:4], "little")
            if not count or 8 + count * 24 > returned.value:
                raise RuntimeError("卷物理映射不完整")
            return sorted({int.from_bytes(data.raw[8 + i * 24:12 + i * 24], "little") for i in range(count)})
        finally:
            self.kernel.CloseHandle(handle)

    def _path_volume(self, path):
        mount, volume = C.create_unicode_buffer(32768), C.create_unicode_buffer(1024)
        if not self.kernel.GetVolumePathNameW(str(path), mount, len(mount)):
            raise OSError(C.get_last_error(), "不能确定路径所在卷")
        if not self.kernel.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume)):
            raise OSError(C.get_last_error(), "不能确定卷标识")
        return volume.value.upper()

    def _locate(self, identity):
        dev = W.DWORD()
        if self.cm.CM_Locate_DevNodeW(C.byref(dev), C.c_wchar_p(identity), 0):
            raise RuntimeError("设备节点已消失，请重新检测。")
        return dev.value

    def _node_property(self, dev, prop, numeric=False):
        data = C.create_unicode_buffer(2048)
        length, kind = W.DWORD(C.sizeof(data)), W.DWORD()
        code = self.cm.CM_Get_DevNode_Registry_PropertyW(dev, prop, C.byref(kind), data, C.byref(length), 0)
        if code:
            raise RuntimeError(f"不能读取设备属性 (0x{code:08X})")
        return C.cast(data, C.POINTER(W.DWORD))[0] if numeric else data.value

    def _ancestors(self, identity):
        dev = self._locate(identity)
        result = []
        for _ in range(12):
            name = C.create_unicode_buffer(1024)
            if self.cm.CM_Get_Device_IDW(dev, name, len(name), 0):
                raise RuntimeError("不能验证设备父子拓扑")
            node = {"id": name.value, "caps": self._node_property(dev, 0x10, True),
                    "service": self._node_property(dev, 0x05)}
            result.append(node)
            if "hub" in node["service"].lower() or name.value.upper().startswith("PCI\\"):
                break
            parent = W.DWORD()
            if self.cm.CM_Get_Parent(C.byref(parent), dev, 0):
                break
            dev = parent.value
        return result

    def inventory(self):
        raw = _ps(r"""
$physical = @(Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,Size,PNPDeviceID,InterfaceType)
$storage = @(Get-Disk | Select-Object Number,BusType,IsBoot,IsSystem)
$partitions = @(Get-Partition | Select-Object DiskNumber,AccessPaths)
$volumes = @(Get-CimInstance Win32_Volume | Where-Object {$_.DriveType -in 2,3} | Select-Object DeviceID,DriveLetter)
$pages = @(Get-CimInstance Win32_PageFileUsage | Select-Object -ExpandProperty Name)
@{physical=$physical; storage=$storage; partitions=$partitions; volumes=$volumes; pages=$pages} | ConvertTo-Json -Depth 6 -Compress
""")
        storage = {int(d["Number"]): d for d in raw["storage"]}
        volumes, unknown = [], set()
        for item in raw["volumes"]:
            partitions = [p for p in raw["partitions"] if item["DeviceID"].upper()
                          in [a.upper() for a in _as_list(p["AccessPaths"])]]
            paths = {path for partition in partitions for path in _as_list(partition["AccessPaths"])}
            try:
                physical = self._volume_disks(item["DeviceID"])
            except (OSError, RuntimeError):
                # Use partition association only to mark the affected disk unknown.
                related = {int(p["DiskNumber"]) for p in partitions}
                unknown.update(related or storage.keys())
                continue
            letter = item["DriveLetter"]
            volumes.append({"id": item["DeviceID"].upper(), "letters": [letter.upper()] if letter else [],
                            "paths": sorted(paths), "disks": physical})
        protected_volumes, protection_known = {}, True
        app_paths = [Path(__file__).resolve(), Path(sys.executable).resolve(), Path.cwd()]
        if os.environ.get("LOCALAPPDATA"):
            app_paths.append(Path(os.environ["LOCALAPPDATA"]))
        for reason, paths in (("程序或状态目录所在盘", app_paths), ("分页文件所在盘", raw["pages"])):
            for path in paths:
                try:
                    protected_volumes.setdefault(self._path_volume(path), set()).add(reason)
                except OSError:
                    protection_known = False
        protection_known = protection_known and not (protected_volumes.keys() - {v["id"] for v in volumes})
        disks = []
        chains = {}
        for item in raw["physical"]:
            try:
                chains[item["PNPDeviceID"]] = self._ancestors(item["PNPDeviceID"])
            except RuntimeError:
                chains[item["PNPDeviceID"]] = []
        for item in raw["physical"]:
            number, identity = int(item["Index"]), item["PNPDeviceID"]
            info = storage.get(number)
            mapped = [v for v in volumes if number in v["disks"]]
            chain = chains[identity]
            external = bool((info and info["BusType"] == 7) or any(
                n["id"].upper().startswith(("USBSTOR\\", "USB\\VID_")) for n in chain))
            eject_id = select_eject_node(chain)
            # A removable bridge must not also remove a sibling physical disk.
            if eject_id and any(other != identity and (not nodes or any(n["id"] == eject_id for n in nodes))
                                for other, nodes in chains.items()):
                eject_id = None
            protected = set()
            if info and info["IsSystem"]:
                protected.add("Windows 系统盘")
            if info and info["IsBoot"]:
                protected.add("Windows 启动盘")
            for volume in mapped:
                protected.update(protected_volumes.get(volume["id"], ()))
            disks.append({"id": identity, "pnp_id": identity, "number": number, "name": item["Model"],
                          "size": int(item["Size"] or 0), "letters": sorted(l for v in mapped for l in v["letters"]),
                          "volumes": mapped, "external": external, "eject_id": eject_id,
                          "protection_known": bool(protection_known and info is not None and number not in unknown
                                                   and info["IsBoot"] is not None and info["IsSystem"] is not None),
                          "protected": sorted(protected)})
        return disks

    def _running_distros(self):
        data = _run(["wsl.exe", "--list", "--running", "--quiet"])
        return [name.strip() for name in data.decode("utf-16-le").lstrip("\ufeff").splitlines() if name.strip()]

    def _wsl(self, distro, *command):
        return _run(["wsl.exe", "--distribution", distro, "--user", "root", "--exec", *command])

    def _installed_distros(self):
        import winreg
        result = []
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss")
        except FileNotFoundError:
            return result
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                    name = winreg.QueryValueEx(key, "DistributionName")[0]
                    base = os.path.expandvars(winreg.QueryValueEx(key, "BasePath")[0])
                    result.append({"name": name, "base": base})
        return result

    def inspect(self, disk):
        result = {"processes": [], "distros": [], "mounts": [],
                  "warnings": ["占用诊断为尽力识别；Windows 可能存在未列出的文件句柄，最终以系统移除结果为准。"]}
        known_files = []
        try:
            running = self._running_distros()
            volume_ids = {v["id"] for v in disk["volumes"]}
            for distro in self._installed_distros():
                if self._path_volume(distro["base"]) in volume_ids:
                    known_files.extend(str(p) for p in Path(distro["base"]).glob("*.vhdx"))
                    if distro["name"] in running:
                        result["distros"].append(distro["name"])
            for name in running:
                try:
                    text = self._wsl(name, "cat", "/proc/self/mountinfo").decode("utf-8", "replace")
                    paths = [p for volume in disk["volumes"] for p in volume.get("paths", [])]
                    result["mounts"].extend(parse_mounts(name, text, disk["letters"], paths))
                except RuntimeError as error:
                    result["warnings"].append(f"{name} 的挂载检查失败：{error}")
        except (OSError, RuntimeError) as error:
            result["warnings"].append(f"WSL 检查未完成：{error}")
        processes = {}
        try:
            items = _as_list(_ps("@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath) | ConvertTo-Json -Compress"))
            for item in items:
                path = item["ExecutablePath"]
                if path:
                    try:
                        related = self._path_volume(path) in {v["id"] for v in disk["volumes"]}
                    except OSError:
                        related = False
                    if related:
                        known_files.append(path)
                        process = self._process_record(item["ProcessId"], item["Name"])
                        if process:
                            processes[process["pid"]] = process
            # Register known files only; Restart Manager does not recursively scan directories.
            for process in self._restart_manager(sorted(set(known_files))):
                if (process["name"].lower() not in ("system", "vmmem", "vmmemwsl", "wslservice", "wslservice.exe")
                        and process.get("service", "").lower() not in ("wslservice", "lxssmanager")):
                    processes[process["pid"]] = process
        except (OSError, RuntimeError) as error:
            result["warnings"].append(f"程序占用检查未完成：{error}")
        result["processes"] = list(processes.values())
        return result

    def _process_record(self, pid, name):
        handle = self.kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
            if not self.kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                return None
            return {"pid": int(pid), "name": name, "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
        finally:
            self.kernel.CloseHandle(handle)

    def _restart_manager(self, files):
        if not files:
            return []
        class UniqueProcess(C.Structure):
            _fields_ = [("pid", W.DWORD), ("created", W.FILETIME)]
        class ProcessInfo(C.Structure):
            _fields_ = [("process", UniqueProcess), ("name", W.WCHAR * 256), ("service", W.WCHAR * 64),
                        ("kind", C.c_int), ("status", W.ULONG), ("session", W.DWORD), ("restartable", W.BOOL)]
        session, key = W.DWORD(), C.create_unicode_buffer(33)
        if self.rm.RmStartSession(C.byref(session), 0, key):
            raise RuntimeError("Restart Manager 会话创建失败")
        try:
            names = (W.LPCWSTR * len(files))(*files)
            code = self.rm.RmRegisterResources(session, len(files), names, 0, None, 0, None)
            if code:
                raise RuntimeError(f"Restart Manager 文件登记失败 ({code})")
            needed, count, reboot = W.UINT(), W.UINT(), W.DWORD()
            code = self.rm.RmGetList(session, C.byref(needed), C.byref(count), None, C.byref(reboot))
            for _ in range(3):
                if code != 234:
                    break
                count.value = needed.value
                items = (ProcessInfo * count.value)()
                code = self.rm.RmGetList(session, C.byref(needed), C.byref(count), items, C.byref(reboot))
                if not code:
                    return [{"pid": p.process.pid, "name": p.name, "service": p.service,
                             "created": (p.process.created.dwHighDateTime << 32) | p.process.created.dwLowDateTime}
                            for p in items[:count.value]]
            if code:
                raise RuntimeError(f"Restart Manager 查询失败 ({code})")
            return []
        finally:
            self.rm.RmEndSession(session)

    def close_process(self, process):
        # Keep a handle to the verified original process while posting WM_CLOSE.
        handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, process["pid"])
        if not handle:
            return C.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID no longer exists.
        try:
            current = self._process_record(process["pid"], process["name"])
            if current is None or current["created"] != process["created"]:
                return False
            windows = []
            callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
            @callback_type
            def collect(hwnd, _):
                pid = W.DWORD()
                self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
                if pid.value == process["pid"]:
                    windows.append(hwnd)
                return True
            self.user.EnumWindows(collect, 0)
            if not windows:
                return False
            for hwnd in windows:
                self.user.PostMessageW(hwnd, 0x0010, 0, 0)
            return self.kernel.WaitForSingleObject(handle, 10000) == 0
        finally:
            self.kernel.CloseHandle(handle)

    def release_mount(self, mount):
        try:
            self._wsl(mount["distro"], "sync")
            self._wsl(mount["distro"], "umount", "--", mount["target"])
            return True
        except RuntimeError:
            return False

    def stop_distro(self, name):
        if name in self._running_distros():
            self._wsl(name, "sync")
            _run(["wsl.exe", "--terminate", name])

    def request_eject(self, disk):
        dev = self._locate(disk["eject_id"])
        veto, name = W.DWORD(), C.create_unicode_buffer(260)
        code = self.cm.CM_Request_Device_EjectW(dev, C.byref(veto), name, len(name), 0)
        if code == 0:
            return {"success": True, "message": "Windows 已允许安全移除，可以拔出硬盘。"}
        reason = f"类型 {veto.value}" + (f"：{name.value}" if name.value else "")
        return {"success": False, "message": f"Windows 未允许安全移除 (0x{code:08X})；{reason}", "veto": reason}
