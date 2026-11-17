"""Windows storage inventory and conservative native safe removal.

All handles opened for diagnostics are closed before requesting PnP removal.
No device disabling, forced dismount, handle closure, or process killing.
"""
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import ntpath
import nturl2path
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
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


def _handle_scan(disk):
    from .disk_handles import scan
    return scan(disk)


def is_explorer(process):
    """Recognize the native image even when RM supplies a friendly name."""
    return (ntpath.basename(str(process.get("executable") or "")).casefold() == "explorer.exe"
            or str(process.get("name") or "").casefold() in ("explorer.exe", "explorer"))


def service_record(item, process, peers=1):
    """PID association is evidence of a host, not individual shared-service I/O."""
    kind = str(item.get("ServiceType") or "")
    command = os.path.expandvars(str(item.get("PathName") or "")).strip()
    match = re.match(r'^"([^"]+)"|^(.+?\.exe)(?:\s|$)', command, re.I)
    binary = (match.group(1) or match.group(2)) if match else ""
    windows = ntpath.normcase(os.environ.get("SystemRoot", r"C:\Windows")).rstrip("\\")
    normalized = ntpath.normcase(ntpath.normpath(binary)) if binary else ""
    for prefix in ("\\\\?\\", "\\??\\"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    if normalized.startswith("\\systemroot\\"):
        normalized = windows + normalized[len("\\systemroot"):]
    security = re.search(r"defend|antivirus|security|sense|windefend|wscsvc|sppsvc|mpssvc|bfe|"
                         r"avast|avira|kaspersky|eset|ekrn|mbam|malware|sophos|sentinel|crowdstrike|"
                         r"csfalcon|norton|symantec|mcafee|webroot|wrsvc|cylance|forti|wslservice|lxssmanager",
                         " ".join((str(item.get("Name", "")), str(item.get("DisplayName", "")), normalized)), re.I)
    reason = ""
    if item.get("State") != "Running":
        reason = "服务当前未运行"
    elif not process.get("created"):
        reason = "无法验证宿主进程创建时间"
    elif kind != "Own Process" or peers != 1:
        reason = "共享宿主仅能关联到进程，不能确认单个服务持有句柄" if ("share" in kind.lower() or peers > 1) else "驱动或非普通独立进程服务"
    elif not item.get("AcceptStop"):
        reason = "服务不接受正常停止控制"
    elif not re.match(r"^[a-z]:\\", normalized):
        reason = "无法验证服务可执行文件位置"
    elif normalized == windows or normalized.startswith(windows + "\\") or security:
        reason = "Windows 核心或安全服务不提供自动停止"
    return {"name": str(item.get("Name", "")), "display_name": str(item.get("DisplayName") or item.get("Name", "")),
            "pid": process["pid"], "created": process.get("created"), "can_stop": not reason,
            "reason": reason or "普通第三方独立进程服务；停止前仍需验证实时状态及保护属性",
            "service_type": kind, "state": item.get("State"), "start_mode": item.get("StartMode"),
            "association": "own_process" if kind == "Own Process" and peers == 1 else "shared_host"}


class _ServiceStatus(C.Structure):
    _fields_ = [(name, W.DWORD) for name in ("service_type", "state", "controls", "exit_code",
                "specific_exit", "checkpoint", "wait_hint", "pid", "flags")]


class _ServiceConfig(C.Structure):
    _fields_ = [("service_type", W.DWORD), ("start_type", W.DWORD), ("error_control", W.DWORD),
                ("binary", W.LPWSTR), ("group", W.LPWSTR), ("tag", W.DWORD),
                ("dependencies", W.LPWSTR), ("account", W.LPWSTR), ("display", W.LPWSTR)]


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
        self.kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        self.kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.user.IsWindow.argtypes = [W.HWND]

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
                  "services": [], "recent_blocks": [], "coverage": {"partial": True},
                  "warnings": []}
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

        def merge(process, evidence=()):
            # Never combine a recycled PID's resources with an older process.
            key = (process["pid"], process.get("created"))
            row = processes.setdefault(key, dict(process, evidence=[], services=[]))
            for entry in [*process.get("evidence", []), *evidence]:
                if entry not in row["evidence"]:
                    row["evidence"].append(entry)
            for field in ("executable", "handle_count", "service"):
                if process.get(field) is not None:
                    row[field] = process[field]
            return row

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
                            merge(dict(process, executable=path), [{"source": "image", "path": path, "kind": "executable"}])
            # Register known files only; Restart Manager does not recursively scan directories.
            for process in self._restart_manager(sorted(set(known_files))):
                if (process["name"].lower() not in ("system", "vmmem", "vmmemwsl", "wslservice", "wslservice.exe")
                        and process.get("service", "").lower() not in ("wslservice", "lxssmanager")):
                    merge(process, [{"source": "restart_manager", "kind": "known_resource"}])
        except (OSError, RuntimeError) as error:
            result["warnings"].append(f"程序占用检查未完成：{error}")
        try:
            handles = _handle_scan(disk)
            result["coverage"] = handles.get("coverage", {"partial": True})
            result["warnings"].extend(handles.get("warnings", []))
            for process in handles.get("processes", []):
                merge(process)
        except (OSError, RuntimeError, ImportError) as error:
            result["warnings"].append(f"文件句柄检查未完成：{error}")
        try:
            services = self._service_inventory()
        except (OSError, RuntimeError) as error:
            services = None
            result["warnings"].append(f"服务关联检查未完成：{error}；已禁止自动关闭无法分类的宿主。")
        for process in processes.values():
            related = [s for s in services or [] if int(s.get("ProcessId") or 0) == process["pid"]]
            process["services"] = [service_record(s, process, len(related)) for s in related]
            result["services"].extend(process["services"])
            special = (process["pid"] <= 4 or process["name"].casefold() in
                       ("system", "registry", "vmmem", "vmmemwsl", "wslservice", "wslservice.exe",
                        "svchost.exe", "services.exe", "lsass.exe", "wininit.exe", "csrss.exe"))
            # The inventory can race process creation, so absence of a service
            # row alone is not permission to close a known Restart Manager host.
            process["can_close"] = bool(services is not None and not related and not special
                                         and not process.get("service") and process.get("created"))
            process["close_reason"] = ("服务宿主请使用明确选择的服务正常停止" if related or process.get("service")
                                       else "进程身份或服务分类无法安全验证" if not process["can_close"] else "正常窗口关闭")
        result["processes"] = list(processes.values())
        candidates = [s["name"] for s in result["services"] if s["can_stop"]]
        protections = self._service_protections(candidates) if candidates else {}
        for service in result["services"]:
            if not service["can_stop"]:
                continue
            protected = protections.get(service["name"], {}).get("protected")
            if protected is None:
                service.update(can_stop=False, reason="无法验证服务保护属性；管理员扫描可能补充权限，当前不提供停止")
            elif protected:
                service.update(can_stop=False, reason="受保护的系统或安全服务不能自动停止")
        try:
            result["recent_blocks"] = self._recent_blocks(disk)
        except (OSError, RuntimeError) as error:
            result["warnings"].append(f"历史移除阻止事件读取失败：{error}")
        return result

    def _service_inventory(self):
        # One inventory, rather than one CIM process/service query per PID.
        return [s for s in _as_list(_ps("@(Get-CimInstance Win32_Service | Select-Object Name,DisplayName,ProcessId,State,AcceptStop,ServiceType,PathName,StartMode) | ConvertTo-Json -Compress"))
                if s.get("ServiceType") and s.get("Name")]

    def _recent_blocks(self, disk):
        rows = _as_list(_ps(r"""
$events = @(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-PnP';Id=225;StartTime=(Get-Date).AddDays(-7)} -MaxEvents 80 -ErrorAction SilentlyContinue)
ConvertTo-Json -InputObject @($events | ForEach-Object {
 $xml=[xml]$_.ToXml(); $row=@{TimeCreated=$_.TimeCreated.ToString('o')}
 foreach($entry in $xml.Event.EventData.Data) {
  if($entry.Name -in @('ProcessId','ProcessName','DeviceInstance','VetoingDevices')) {$row[$entry.Name]=[string]$entry.'#text'}
 }
 $row
}) -Depth 4 -Compress
"""))
        identities = [str(disk.get(key) or "").casefold() for key in ("pnp_id", "eject_id") if disk.get(key)]
        result = []
        for row in rows:
            values = [str(v).casefold() for field in ("DeviceInstance", "VetoingDevices") for v in _as_list(row.get(field))]
            if not any(re.search(r"(?<![\w\\&])" + re.escape(identity) + r"(?![\w\\&])", value)
                       for identity in identities for value in values):
                continue
            try:
                pid = int(str(row.get("ProcessId", "0")), 0)
            except ValueError:
                pid = 0
            result.append({"time": row.get("TimeCreated", ""), "process_id": pid,
                           "process_name": row.get("ProcessName", ""), "device_instance": row.get("DeviceInstance", ""),
                           "vetoing_devices": row.get("VetoingDevices", ""), "historical": True})
            if len(result) >= 10:
                break
        return result

    def _process_record(self, pid, name, handle=None):
        owned = handle is None
        if owned:
            handle = self.kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME(), W.FILETIME()
            if not self.kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
                return None
            record = {"pid": int(pid), "name": name, "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
            image, size = C.create_unicode_buffer(32768), W.DWORD(32768)
            if self.kernel.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
                record["executable"] = image.value
            return record
        finally:
            if owned:
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

    def close_explorer(self, process, disk, timeout=10):
        """Close only verified folder windows on this disk, preserving the shell.

        Explorer's desktop and unrelated folder windows often share a PID.
        Closing the selected folder window also closes its other tabs, as the
        caller's confirmation explains; no process-exit wait is appropriate.
        """
        handle = self.kernel.OpenProcess(0x1000, False, process["pid"])
        if not handle:
            return {"success": False, "message": "无法验证文件资源管理器进程身份，请重新检测。"}
        try:
            current = self._process_record(process["pid"], process["name"])
            if not current or current["created"] != process["created"]:
                return {"success": False, "message": "文件资源管理器进程身份已改变，请重新检测。"}
            windows = _as_list(_ps(r"""
$shell = New-Object -ComObject Shell.Application
ConvertTo-Json -InputObject @($shell.Windows() | ForEach-Object {
 try { @{hwnd=[long]$_.HWND; location_url=[string]$_.LocationURL} } catch {}
}) -Compress
"""))
            target_volumes = {v["id"].upper() for v in disk["volumes"]}
            targets = set()
            for window in windows:
                try:
                    url = urlsplit(str(window.get("location_url") or ""))
                    if url.scheme.casefold() != "file":
                        continue
                    path = nturl2path.url2pathname(("//" + url.netloc if url.netloc and url.netloc.casefold() != "localhost" else "") + url.path)
                    if self._path_volume(path).upper() not in target_volumes:
                        continue
                    hwnd = int(window.get("hwnd") or 0)
                    if not hwnd or not self.user.IsWindow(hwnd):
                        continue
                    owner = W.DWORD()
                    self.user.GetWindowThreadProcessId(hwnd, C.byref(owner))
                    if owner.value == process["pid"]:
                        targets.add(hwnd)
                except (OSError, ValueError, TypeError):
                    continue
            if not targets:
                return {"success": False, "message": "未找到可安全关闭的目标盘文件资源管理器窗口；预览、后台或隐藏标签的占用请手动释放后重试。"}
            current = self._process_record(process["pid"], process["name"])
            if not current or current["created"] != process["created"]:
                return {"success": False, "message": "窗口检查期间文件资源管理器身份已改变，未发送关闭请求。"}
            failed = []
            pending = set()
            for hwnd in sorted(targets):
                owner = W.DWORD()
                thread_id = self.user.GetWindowThreadProcessId(hwnd, C.byref(owner))
                if not self.user.IsWindow(hwnd):
                    continue
                if not thread_id or not owner.value:
                    failed.append(hwnd)
                    continue
                if owner.value != process["pid"]:
                    continue
                if self.user.PostMessageW(hwnd, 0x0010, 0, 0):
                    pending.add(hwnd)
                else:
                    failed.append(hwnd)
            deadline = time.monotonic() + timeout
            while pending:
                for hwnd in list(pending):
                    owner = W.DWORD()
                    thread_id = self.user.GetWindowThreadProcessId(hwnd, C.byref(owner))
                    if not self.user.IsWindow(hwnd):
                        pending.remove(hwnd)
                    elif not thread_id or not owner.value:
                        failed.append(hwnd)
                        pending.remove(hwnd)
                    elif owner.value != process["pid"]:
                        pending.remove(hwnd)
                if not pending or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            if failed or pending:
                return {"success": False, "message": "部分目标盘文件资源管理器窗口拒绝关闭或尚未关闭；请完成窗口提示后重试。"}
            return {"success": True, "message": f"已正常关闭 {len(targets)} 个目标盘文件资源管理器窗口及其标签。"}
        except (OSError, RuntimeError) as error:
            return {"success": False, "message": f"文件资源管理器窗口检查未完成：{error}"}
        finally:
            self.kernel.CloseHandle(handle)

    def close_process(self, process):
        if is_explorer(process):
            return False  # Requires disk context and close_explorer's window filter.
        # Keep a handle to the verified original process while posting WM_CLOSE.
        handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, process["pid"])
        if not handle:
            return C.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID no longer exists.
        try:
            current = self._process_record(process["pid"], process["name"], handle=handle)
            if (current is None or current["created"] != process["created"]
                    or not current.get("executable") or is_explorer(current)):
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

    def _service_api(self):
        if hasattr(self, "scm"):
            return self.scm
        api = C.WinDLL("advapi32", use_last_error=True)
        api.OpenSCManagerW.argtypes = [W.LPCWSTR, W.LPCWSTR, W.DWORD]
        api.OpenSCManagerW.restype = W.HANDLE
        api.OpenServiceW.argtypes = [W.HANDLE, W.LPCWSTR, W.DWORD]
        api.OpenServiceW.restype = W.HANDLE
        api.CloseServiceHandle.argtypes = [W.HANDLE]
        api.QueryServiceStatusEx.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        api.QueryServiceConfigW.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        api.QueryServiceConfig2W.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        api.ControlService.argtypes = [W.HANDLE, W.DWORD, C.c_void_p]
        self.scm = api
        return api

    def _service_protections(self, names):
        """Read only the launch-protection metadata of otherwise eligible hosts."""
        result = {name: {"protected": None} for name in names}
        try:
            api = self._service_api()
            manager = api.OpenSCManagerW(None, None, 1)
            if not manager:
                return result
            try:
                for name in names:
                    handle = api.OpenServiceW(manager, name, 0x0001)
                    if not handle:
                        continue
                    try:
                        value, needed = W.DWORD(), W.DWORD()
                        if api.QueryServiceConfig2W(handle, 12, C.byref(value), C.sizeof(value), C.byref(needed)):
                            result[name]["protected"] = value.value
                    finally:
                        api.CloseServiceHandle(handle)
            finally:
                api.CloseServiceHandle(manager)
        except (OSError, RuntimeError):
            pass
        return result

    def stop_service(self, service, timeout=10):
        """Send only SERVICE_CONTROL_STOP to a verified ordinary own-process service.

        ControlService itself rejects active dependents. We never enumerate and
        stop a dependent tree, change configuration, or terminate its process.
        """
        def refused(message):
            return {"success": False, "message": message}

        if not service.get("can_stop") or not service.get("created"):
            return refused("此服务未通过可停止性与身份检查，请重新检测。")
        api = self._service_api()
        manager = api.OpenSCManagerW(None, None, 1)
        if not manager:
            return refused(f"无法访问服务控制管理器 ({C.get_last_error()})。")
        handle = process_handle = None
        try:
            handle = api.OpenServiceW(manager, service["name"], 0x0001 | 0x0004 | 0x0020)
            if not handle:
                return refused(f"无法正常停止服务：访问被拒绝或服务不可用 ({C.get_last_error()})。")
            needed = W.DWORD()

            def status():
                value = _ServiceStatus()
                if not api.QueryServiceStatusEx(handle, 0, C.byref(value), C.sizeof(value), C.byref(needed)):
                    raise OSError(C.get_last_error(), "无法读取实时服务状态")
                return value

            live = status()
            if live.state != 4 or live.pid != service["pid"]:
                return refused("服务实时运行状态或 PID 已改变，请重新检测。")
            if live.service_type != 0x10 or not live.controls & 1:
                return refused("共享、驱动、交互式或不接受停止控制的服务不能自动停止。")
            api.QueryServiceConfigW(handle, None, 0, C.byref(needed))
            if not needed.value or needed.value > 65536:
                return refused("无法验证服务配置。")
            buffer = C.create_string_buffer(needed.value)
            if not api.QueryServiceConfigW(handle, buffer, len(buffer), C.byref(needed)):
                return refused("无法读取服务配置；未发送停止请求。")
            config = C.cast(buffer, C.POINTER(_ServiceConfig)).contents
            checked = service_record({"Name": service["name"], "DisplayName": config.display,
                "State": "Running", "AcceptStop": True,
                "ServiceType": "Own Process" if config.service_type == 0x10 else "Unknown",
                "PathName": config.binary}, service)
            if not checked["can_stop"]:
                return refused(checked["reason"])
            protected = W.DWORD()
            if not api.QueryServiceConfig2W(handle, 12, C.byref(protected), C.sizeof(protected), C.byref(needed)):
                return refused("无法验证服务启动保护属性；未发送停止请求。")
            if protected.value:
                return refused("受保护的系统或安全服务不能自动停止。")
            process_handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, service["pid"])
            if not process_handle:
                return refused("无法验证服务进程身份；可能需要管理员权限。")
            current = self._process_record(service["pid"], service["name"])
            if not current or current["created"] != service["created"]:
                return refused("服务进程创建时间已改变，拒绝停止复用的 PID。")
            live = status()
            if live.state != 4 or live.pid != service["pid"] or live.service_type != 0x10 or not live.controls & 1:
                return refused("服务在验证期间发生变化，请重新检测。")
            response = _ServiceStatus()
            if not api.ControlService(handle, 1, C.byref(response)):
                return refused(f"服务拒绝正常停止 ({C.get_last_error()})；未停止依赖服务或强制结束进程。")
            deadline = time.monotonic() + timeout
            while True:
                live = status()
                if live.state == 1:
                    return {"success": True, "message": "服务已报告正常停止。"}
                if live.state == 4 and live.pid != service["pid"]:
                    return refused("服务已由新进程承载；请重新检测。")
                if time.monotonic() >= deadline:
                    return refused("服务未在等待时间内停止；未强制结束进程，请稍后复查。")
                time.sleep(0.2)
        except (OSError, RuntimeError) as error:
            return refused(str(error))
        finally:
            if process_handle:
                self.kernel.CloseHandle(process_handle)
            if handle:
                api.CloseServiceHandle(handle)
            api.CloseServiceHandle(manager)

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
        meanings = {0: "Unknown：未知阻止原因", 1: "LegacyDevice：旧式设备", 2: "PendingClose：等待关闭",
                    3: "WindowsApp：Windows 应用仍在使用", 4: "WindowsService：Windows 服务仍在使用",
                    5: "OutstandingOpen：仍有打开的文件或设备句柄", 6: "Device：关联设备阻止移除",
                    7: "Driver：驱动程序阻止移除", 8: "IllegalDeviceRequest：设备不支持此请求",
                    9: "InsufficientPower：供电不足", 10: "NonDisableable：设备不可停用",
                    11: "LegacyDriver：旧式驱动", 12: "InsufficientRights：权限不足",
                    13: "AlreadyRemoved：设备已移除"}
        reason = meanings.get(veto.value, f"Unknown：未知类型 {veto.value}") + (f"；{name.value}" if name.value else "")
        return {"success": False, "message": f"Windows 未允许安全移除 (0x{code:08X})；{reason}", "veto": reason}
