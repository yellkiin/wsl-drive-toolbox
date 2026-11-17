import copy
import shutil
import unittest
from unittest.mock import patch

from toolbox import disks
from toolbox import disk_native


def sample_disk():
    return {"id": "USBSTOR\\DISK\\SAMPLE", "pnp_id": "USBSTOR\\DISK\\SAMPLE",
            "number": 3, "name": "Example USB SSD", "size": 1000000000,
            "letters": ["R:"], "volumes": [{"id": "volume-a", "letters": ["R:"],
                                             "disks": [3]}],
            "eject_id": "USB\\VID_1234&PID_5678\\SAMPLE", "external": True,
            "protection_known": True, "protected": []}


class FakeNative:
    def __init__(self):
        self.disk = sample_disk()
        self.info = {"processes": [], "distros": [], "mounts": [], "warnings": []}
        self.events = []
        self.result = {"success": True, "message": "allowed"}
        self.refuse_close = False
        self.refuse_mount = False

    def inventory(self):
        return [copy.deepcopy(self.disk)] if self.disk else []

    def inspect(self, disk):
        return copy.deepcopy(self.info)

    def close_process(self, process):
        self.events.append(("close", process["pid"]))
        return not self.refuse_close

    def close_explorer(self, process, disk):
        self.events.append(("explorer_windows", process["pid"], disk["id"]))
        return {"success": True, "message": "target windows closed"}

    def release_mount(self, mount):
        self.events.append(("umount", mount["distro"], mount["target"]))
        return not self.refuse_mount

    def stop_distro(self, name):
        self.events.append(("stop", name))

    def request_eject(self, disk):
        self.events.append(("eject", disk["id"]))
        return self.result

    def stop_service(self, service):
        self.events.append(("service", service["name"]))
        return {"success": True, "message": "stopped"}


class DiskSafetyTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeNative()
        self.target = sample_disk()
        self.mock = patch.object(disks, "_native", self.native)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def test_fixed_usb_storage_is_listed_by_physical_identity(self):
        self.assertEqual(disks.list_disks()[0]["id"], "USBSTOR\\DISK\\SAMPLE")

    def test_drive_letter_rebound_to_another_disk_is_refused(self):
        self.native.disk["id"] = "other-device"
        with self.assertRaises(RuntimeError):
            disks.eject(self.target)
        self.assertEqual(self.native.events, [])

    def test_changed_volume_map_is_refused(self):
        self.native.disk["volumes"][0]["letters"] = ["S:"]
        with self.assertRaises(RuntimeError):
            disks.eject(self.target)
        self.assertEqual(self.native.events, [])

    def test_directory_mount_reassignment_is_refused(self):
        self.target["volumes"][0]["paths"] = ["C:\\mounted\\external\\"]
        self.native.disk = copy.deepcopy(self.target)
        self.native.disk["volumes"][0]["paths"] = ["C:\\mounted\\other\\"]
        with self.assertRaises(RuntimeError):
            disks.eject(self.target)
        self.assertEqual(self.native.events, [])

    def test_missing_device_is_not_success(self):
        self.native.disk = None
        with self.assertRaises(RuntimeError):
            disks.eject(self.target)

    def test_system_boot_pagefile_and_application_disks_are_protected(self):
        for reason in ("system", "boot", "pagefile", "application"):
            with self.subTest(reason=reason):
                self.native.disk["protected"] = [reason]
                with self.assertRaises(RuntimeError):
                    disks.eject(self.target)
        self.assertEqual(self.native.events, [])

    def test_unknown_protection_or_topology_is_refused(self):
        for change in ({"protection_known": False}, {"eject_id": None},
                       {"external": False}):
            with self.subTest(change=change):
                self.native.disk = dict(sample_disk(), **change)
                with self.assertRaises(RuntimeError):
                    disks.eject(self.target)

    def test_spanned_volume_is_refused(self):
        self.target["volumes"][0]["disks"] = [3, 4]
        self.native.disk = copy.deepcopy(self.target)
        with self.assertRaises(RuntimeError):
            disks.eject(self.target)

    def test_only_native_success_permits_safe_to_unplug_message(self):
        self.assertTrue(disks.eject(self.target)["success"])
        self.native.result = {"success": False, "message": "busy", "veto": "OutstandingOpen"}
        result = disks.eject(self.target)
        self.assertFalse(result["success"])
        self.assertEqual(result["veto"], "OutstandingOpen")
        self.assertIn("remaining", result)

    def test_refused_normal_close_prevents_eject(self):
        self.native.info["processes"] = [{"pid": 42, "name": "editor", "created": 900}]
        self.native.refuse_close = True
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertFalse(result["success"])
        self.assertEqual(self.native.events, [("close", 42)])

    def test_explorer_closes_target_disk_windows_instead_of_entire_process(self):
        self.native.info["processes"] = [{"pid": 42, "name": "explorer.exe", "created": 900}]
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertTrue(result["success"])
        self.assertEqual(self.native.events, [("explorer_windows", 42, self.target["id"]),
                                              ("eject", self.target["id"])])

    def test_new_process_requires_new_confirmation(self):
        accepted = copy.deepcopy(self.native.info)
        self.native.info["processes"] = [{"pid": 42, "name": "editor", "created": 900}]
        result = disks.close_and_eject(self.target, accepted)
        self.assertFalse(result["success"])
        self.assertEqual(self.native.events, [])

    def test_no_second_scan_when_cleanup_then_eject_is_vetoed(self):
        scans = []
        self.native.inspect = lambda _: scans.append(1) or copy.deepcopy(self.native.info)
        self.native.result = {"success": False, "message": "busy", "veto": "device"}
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertEqual(len(scans), 2)
        self.assertIn("remaining", result)

    def test_busy_mount_does_not_force_stop_unconsented_distro(self):
        self.native.info["mounts"] = [{"distro": "OtherLinux", "target": "/mnt/r", "source": "R:\\"}]
        self.native.refuse_mount = True
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertFalse(result["success"])
        self.assertEqual(self.native.events, [("umount", "OtherLinux", "/mnt/r")])

    def test_closes_only_confirmed_workloads_before_eject(self):
        self.native.info.update(processes=[{"pid": 42, "name": "editor", "created": 900}],
                                distros=["ExternalLinux"],
                                mounts=[{"distro": "OtherLinux", "target": "/mnt/r", "source": "R:\\"}])
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertTrue(result["success"])
        self.assertEqual(self.native.events, [("close", 42), ("umount", "OtherLinux", "/mnt/r"),
                                              ("stop", "ExternalLinux"), ("eject", self.target["id"])])

    def test_device_replacement_during_close_is_refused(self):
        self.native.info["distros"] = ["ExternalLinux"]
        def replaced(_name):
            self.native.disk["pnp_id"] = "replacement"
        self.native.stop_distro = replaced
        with self.assertRaises(RuntimeError):
            disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertEqual(self.native.events, [])

    def test_close_refusal_does_not_skip_other_approved_cleanup(self):
        self.native.info.update(processes=[{"pid": 42, "name": "editor", "created": 900},
                                           {"pid": 43, "name": "viewer", "created": 901}],
                                distros=["Linux"])
        self.native.refuse_close = True
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertEqual(self.native.events, [("close", 42), ("close", 43), ("stop", "Linux")])
        self.assertFalse(result["success"])
        self.assertEqual(len(result["outcomes"]), 3)
        self.assertIn("remaining", result)

    def test_services_require_explicit_selection_and_never_window_close_host(self):
        service = {"name": "VendorSvc", "display_name": "Vendor", "pid": 42,
                   "created": 900, "can_stop": True, "reason": ""}
        self.native.info.update(processes=[{"pid": 42, "name": "vendor.exe", "created": 900,
                                            "services": [service], "can_close": False}], services=[service])
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertTrue(result["success"])
        self.assertEqual(self.native.events, [("eject", self.target["id"])])
        self.native.events.clear()
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info), [service])
        self.assertTrue(result["success"])
        self.assertEqual(self.native.events[0], ("service", "VendorSvc"))

    def test_changed_selected_service_identity_requires_confirmation(self):
        service = {"name": "VendorSvc", "pid": 42, "created": 900, "can_stop": True}
        self.native.info["services"] = [dict(service, created=901)]
        result = disks.close_and_eject(self.target, dict(self.native.info, services=[service]), [service])
        self.assertFalse(result["success"])
        self.assertEqual(self.native.events, [])


class NativeRulesTests(unittest.TestCase):
    def setUp(self):
        scanner = patch.object(disk_native, "_handle_scan", return_value={
            "processes": [], "coverage": {"partial": False}, "warnings": []})
        scanner.start()
        self.addCleanup(scanner.stop)

    def test_handle_evidence_includes_c_installed_service_and_preserves_image_evidence(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        adapter._running_distros = lambda: []
        adapter._installed_distros = lambda: []
        adapter._path_volume = lambda path: "volume-a" if path.startswith("R:") else "other"
        adapter._process_record = lambda pid, name: {"pid": pid, "name": name, "created": 900}
        adapter._restart_manager = lambda _: []
        adapter._service_inventory = lambda: [{"Name": "VendorSvc", "DisplayName": "Vendor",
            "ProcessId": 42, "State": "Running", "AcceptStop": True, "ServiceType": "Own Process",
            "PathName": '"C:\\Program Files\\Vendor\\svc.exe"', "StartMode": "Auto"}]
        adapter._service_protections = lambda names: {name: {"protected": 0} for name in names}
        adapter._recent_blocks = lambda _: []
        scan = {"processes": [{"pid": 42, "name": "svc.exe", "created": 900,
                "executable": "C:\\Program Files\\Vendor\\svc.exe", "handle_count": 1,
                "evidence": [{"source": "handle", "path": "R:\\data", "kind": "file"}]}],
                "coverage": {"partial": True}, "warnings": ["access denied"]}
        with patch.object(disk_native, "_handle_scan", return_value=scan, create=True), patch.object(
                disk_native, "_ps", return_value=[{"ProcessId": 42, "Name": "svc.exe",
                                                  "ExecutablePath": "R:\\app.exe"}]):
            info = adapter.inspect(sample_disk())
        self.assertEqual(len(info["processes"]), 1)
        self.assertEqual({e["source"] for e in info["processes"][0]["evidence"]}, {"image", "handle"})
        self.assertTrue(info["services"][0]["can_stop"])
        self.assertFalse(info["processes"][0]["can_close"])
        self.assertTrue(info["coverage"]["partial"])
        self.assertEqual(info["warnings"], ["access denied"])

    def test_explorer_only_closes_target_volume_windows_and_not_shell_process(self):
        adapter, windows, closed_handles = explorer_fixture()
        metadata = [{"hwnd": 10, "location_url": "file:///R:/a%20folder"},
                    {"hwnd": 11, "location_url": "file:///D:/unrelated"},
                    {"hwnd": 12, "location_url": "file:///C:/mounts/external/nested"},
                    {"hwnd": 12, "location_url": "file:///R:/other-tab"},
                    {"hwnd": 13, "location_url": "file:///R:/other-process"},
                    {"hwnd": 14, "location_url": "shell:Desktop"}]
        with patch.object(disk_native, "_ps", return_value=metadata):
            result = adapter.close_explorer({"pid": 42, "name": "explorer.exe", "created": 900}, sample_disk())
        self.assertTrue(result["success"])
        self.assertEqual(windows.posted, [(10, 0x10), (12, 0x10)])
        self.assertEqual(windows.alive, {11, 13, 14})
        self.assertEqual(closed_handles, [99])

    def test_restart_manager_friendly_name_with_handle_image_routes_to_target_windows(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        adapter._running_distros = lambda: []
        adapter._installed_distros = lambda: []
        adapter._service_inventory = lambda: []
        adapter._recent_blocks = lambda _: []
        adapter._restart_manager = lambda _: [{"pid": 42, "name": "Windows Explorer", "created": 900}]
        scan = {"processes": [{"pid": 42, "name": "explorer.exe", "created": 900,
                "executable": "C:\\Windows\\explorer.exe", "handle_count": 1,
                "evidence": [{"source": "handle", "path": "R:\\folder", "kind": "directory"}]}]}
        with patch.object(disk_native, "_ps", return_value=[]), patch.object(disk_native, "_handle_scan", return_value=scan):
            info = adapter.inspect(sample_disk())
        native = FakeNative()
        native.info = info
        with patch.object(disks, "_native", native):
            result = disks.close_and_eject(sample_disk(), info)
        self.assertTrue(result["success"])
        self.assertEqual(native.events[0], ("explorer_windows", 42, sample_disk()["id"]))

    def test_explorer_missing_target_window_or_changed_identity_sends_no_close(self):
        for scenario in ("no_window", "identity_changed", "cannot_read_metadata"):
            with self.subTest(scenario=scenario):
                adapter, windows, closed_handles = explorer_fixture()
                if scenario == "identity_changed":
                    adapter._process_record = lambda *_: {"pid": 42, "created": 901}
                metadata = [{"hwnd": 10, "location_url": "file:///D:/unrelated" if scenario == "no_window" else "file:///R:/target"}]
                with patch.object(disk_native, "_ps", side_effect=RuntimeError("denied") if scenario == "cannot_read_metadata" else None,
                                  return_value=metadata):
                    result = adapter.close_explorer({"pid": 42, "name": "explorer.exe", "created": 900}, sample_disk())
                self.assertFalse(result["success"])
                self.assertTrue(result["message"])
                self.assertEqual(windows.posted, [])
                self.assertEqual(closed_handles, [99])

    def test_explorer_close_rejection_or_timeout_is_not_success(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                adapter, windows, _ = explorer_fixture(close_on_post=False, rejected=rejected)
                with patch.object(disk_native, "_ps", return_value=[{"hwnd": 10, "location_url": "file:///R:/target"}]), patch.object(
                        disk_native.time, "monotonic", side_effect=[0, 20, 20]), patch.object(disk_native.time, "sleep"), patch.object(
                        disk_native.C, "get_last_error", return_value=5, create=True):
                    result = adapter.close_explorer({"pid": 42, "name": "explorer.exe", "created": 900}, sample_disk())
                self.assertFalse(result["success"])
                self.assertTrue(result["message"])

    def test_explorer_unknown_window_owner_after_close_is_not_reported_closed(self):
        adapter, windows, _ = explorer_fixture(close_on_post=False)
        query_owner = windows.GetWindowThreadProcessId
        windows.GetWindowThreadProcessId = lambda hwnd, pid: 0 if windows.posted else query_owner(hwnd, pid)
        with patch.object(disk_native, "_ps", return_value=[{"hwnd": 10, "location_url": "file:///R:/target"}]):
            result = adapter.close_explorer({"pid": 42, "name": "explorer.exe", "created": 900}, sample_disk())
        self.assertFalse(result["success"])
        self.assertIn(10, windows.alive)

    def test_protected_or_unverifiable_service_not_offered_for_stopping(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        adapter._running_distros = lambda: []
        adapter._installed_distros = lambda: []
        adapter._restart_manager = lambda _: []
        adapter._service_inventory = lambda: [{"Name": "VendorSvc", "ProcessId": 42, "State": "Running",
            "AcceptStop": True, "ServiceType": "Own Process", "PathName": '"C:\\Vendor\\svc.exe"'}]
        adapter._recent_blocks = lambda _: []
        scan = {"processes": [{"pid": 42, "name": "svc.exe", "created": 900,
                               "evidence": [{"source": "handle", "path": "R:\\data", "kind": "file"}]}]}
        for protected in (1, None):
            with self.subTest(protected=protected):
                adapter._service_protections = lambda _: {"VendorSvc": {"protected": protected}}
                with patch.object(disk_native, "_handle_scan", return_value=scan), patch.object(disk_native, "_ps", return_value=[]):
                    info = adapter.inspect(sample_disk())
                self.assertFalse(info["services"][0]["can_stop"])
                self.assertTrue(info["services"][0]["reason"])

    def test_shared_driver_windows_security_and_nonstoppable_services_are_ineligible(self):
        base = {"Name": "VendorSvc", "ProcessId": 42, "State": "Running", "AcceptStop": True,
                "ServiceType": "Own Process", "PathName": '"C:\\Vendor\\svc.exe"', "StartMode": "Auto"}
        process = {"pid": 42, "created": 900}
        for change, peers in [({"ServiceType": "Share Process"}, 1), ({}, 2),
                              ({"ServiceType": "Kernel Driver"}, 1),
                              ({"PathName": "C:\\Windows\\System32\\svc.exe"}, 1),
                              ({"PathName": r'"\\?\C:\Windows\System32\svc.exe"'}, 1),
                              ({"PathName": r'"\??\C:\Windows\System32\svc.exe"'}, 1),
                              ({"PathName": '"C:\\Program Files\\Windows Defender\\MsMpEng.exe"'}, 1),
                              ({"Name": "WinDefend"}, 1), ({"AcceptStop": False}, 1)]:
            with self.subTest(change=change, peers=peers):
                row = disk_native.service_record(dict(base, **change), process, peers)
                self.assertFalse(row["can_stop"])
                self.assertTrue(row["reason"])

    def test_recent_pnp_blocks_filter_target_and_exclude_commandline(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        events = [{"TimeCreated": "now", "ProcessId": "42", "ProcessName": "C:\\svc.exe",
                   "DeviceInstance": sample_disk()["pnp_id"], "VetoingDevices": [], "CommandLine": "secret"},
                  {"ProcessId": "99", "DeviceInstance": "USBSTOR\\OTHER", "VetoingDevices": []}]
        with patch.object(disk_native, "_ps", return_value=events):
            rows = adapter._recent_blocks(sample_disk())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["process_id"], 42)
        self.assertTrue(rows[0]["historical"])
        self.assertNotIn("CommandLine", rows[0])

    @unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell")
    def test_pnp_script_emits_json_arrays_for_zero_and_one_fixture_event(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        real_ps = disk_native._ps
        for count in (0, 1):
            with self.subTest(count=count):
                fixture = """
function Get-WinEvent {
 [CmdletBinding()] param($FilterHashtable,$MaxEvents)
"""
                if count:
                    fixture += r"""
 $event=[pscustomobject]@{TimeCreated=[datetime]'2026-11-17T12:00:00'}
 $event | Add-Member -MemberType ScriptMethod -Name ToXml -Value {
  '<Event><EventData><Data Name="ProcessId">42</Data><Data Name="ProcessName">C:\fixture.exe</Data><Data Name="DeviceInstance">USBSTOR\DISK\SAMPLE</Data></EventData></Event>'
 } -PassThru
"""
                fixture += "}\n"
                payloads = []
                def run_fixture(script):
                    value = real_ps(fixture + script)
                    payloads.append(value)
                    return value
                with patch.object(disk_native, "_ps", side_effect=run_fixture):
                    rows = adapter._recent_blocks(sample_disk())
                self.assertIsInstance(payloads[0], list)
                self.assertEqual(len(rows), count)
                if count:
                    self.assertEqual(rows[0]["process_id"], 42)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell")
    def test_explorer_script_emits_json_arrays_for_zero_and_one_fixture_window(self):
        real_ps = disk_native._ps
        for count in (0, 1):
            with self.subTest(count=count):
                adapter, windows, _ = explorer_fixture()
                fixture_windows = "@([pscustomobject]@{HWND=10;LocationURL='file:///D:/unrelated'})" if count else "@()"
                fixture = """
function New-Object {
 param($ComObject)
 $shell=[pscustomobject]@{}
 $shell | Add-Member -MemberType ScriptMethod -Name Windows -Value { return """ + fixture_windows + """ } -PassThru
}
"""
                payloads = []
                def run_fixture(script):
                    value = real_ps(fixture + script)
                    payloads.append(value)
                    return value
                with patch.object(disk_native, "_ps", side_effect=run_fixture):
                    result = adapter.close_explorer({"pid": 42, "name": "explorer.exe", "created": 900}, sample_disk())
                self.assertIsInstance(payloads[0], list)
                self.assertEqual(len(payloads[0]), count)
                self.assertFalse(result["success"])
                self.assertIn("未找到", result["message"])
                self.assertEqual(windows.posted, [])

    def test_native_service_stop_checks_identity_type_protection_and_completion(self):
        for scenario, expected, controls in [("ok", True, 1), ("changed_pid", False, 0),
                ("reused_pid", False, 0), ("shared", False, 0), ("protected", False, 0),
                ("denied", False, 0), ("timeout", False, 1), ("dependent", False, 1),
                ("windows", False, 0), ("not_stoppable", False, 0)]:
            with self.subTest(scenario=scenario):
                adapter = object.__new__(disk_native.WindowsDisks)
                api = FakeSCM(scenario)
                adapter.scm = api
                class Kernel:
                    def OpenProcess(self, *_):
                        return 99
                    def CloseHandle(self, _):
                        pass
                adapter.kernel = Kernel()
                adapter._process_record = lambda *_: {"pid": 42, "created": 901 if scenario == "reused_pid" else 900}
                with patch.object(disk_native.time, "sleep"), patch.object(
                        disk_native.time, "monotonic", side_effect=[0, 0, 20, 20]), patch.object(
                        disk_native.C, "get_last_error", return_value=5, create=True):
                    result = adapter.stop_service({"name": "VendorSvc", "pid": 42, "created": 900,
                                                   "can_stop": True})
                self.assertEqual(result["success"], expected)
                self.assertEqual(api.controls, controls)
                self.assertEqual(api.closed, [2, 1])

    def test_wsl_backend_service_uses_targeted_wsl_release_not_window_close(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        adapter._running_distros = lambda: []
        adapter._installed_distros = lambda: []
        adapter._path_volume = lambda _: "volume-a"
        adapter._process_record = lambda *_: None
        adapter._restart_manager = lambda _files: [{"pid": 42, "name": "WSL Service",
                                                   "created": 900, "service": "WslService"}]
        with patch.object(disk_native, "_ps", return_value=[{"ProcessId": 50, "Name": "app",
                                                            "ExecutablePath": "R:\\app.exe"}]):
            info = adapter.inspect(sample_disk())
        self.assertEqual(info["processes"], [])

    def test_volume_mount_directories_belong_only_to_their_own_volume(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        raw = {"physical": [{"Index": 3, "PNPDeviceID": "USBSTOR\\DISK\\SAMPLE", "Model": "USB SSD", "Size": 1000}],
               "storage": [{"Number": 3, "BusType": 7, "IsBoot": False, "IsSystem": False}],
               "partitions": [{"DiskNumber": 3, "AccessPaths": ["VOLUME-A", "C:\\mounts\\a\\"]},
                              {"DiskNumber": 3, "AccessPaths": ["VOLUME-B", "C:\\mounts\\b\\"]}],
               "volumes": [{"DeviceID": "VOLUME-A", "DriveLetter": None},
                           {"DeviceID": "VOLUME-B", "DriveLetter": None}], "pages": []}
        adapter._volume_disks = lambda _: [3]
        adapter._path_volume = lambda _: "VOLUME-A"
        adapter._ancestors = lambda _: [{"id": "USBSTOR\\DISK\\SAMPLE", "caps": 4, "service": "disk"}]
        with patch.object(disk_native, "_ps", return_value=raw):
            disk = adapter.inventory()[0]
        self.assertIn("C:\\mounts\\a\\", disk["volumes"][0]["paths"])
        self.assertNotIn("C:\\mounts\\b\\", disk["volumes"][0]["paths"])

    def test_native_eject_success_and_veto_are_not_inferred_from_disappearance(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        adapter._locate = lambda _identity: 123
        class ConfigManager:
            def __init__(self, code):
                self.code = code
            def CM_Request_Device_EjectW(self, _dev, veto, name, _length, _flags):
                if self.code:
                    veto._obj.value = 6
                    name.value = "example blocker"
                return self.code
        adapter.cm = ConfigManager(0)
        self.assertTrue(adapter.request_eject(sample_disk())["success"])
        adapter.cm = ConfigManager(0x17)
        refused = adapter.request_eject(sample_disk())
        self.assertFalse(refused["success"])
        self.assertIn("example blocker", refused["veto"])
        adapter._locate = lambda _identity: (_ for _ in ()).throw(RuntimeError("device disappeared"))
        with self.assertRaises(RuntimeError):
            adapter.request_eject(sample_disk())

    def test_inaccessible_process_is_not_reported_as_closed(self):
        adapter = object.__new__(disk_native.WindowsDisks)
        class Kernel:
            def OpenProcess(self, *_):
                return None
        adapter.kernel = Kernel()
        with patch.object(disk_native.C, "get_last_error", return_value=5, create=True):
            self.assertFalse(adapter.close_process({"pid": 42, "created": 900, "name": "editor"}))

    def test_generic_close_refuses_explorer_without_disk_window_context(self):
        adapter, windows, closed_handles = explorer_fixture()
        windows.EnumWindows = lambda callback, _: [callback(hwnd, 0) for hwnd in sorted(windows.alive)]
        adapter.kernel.WaitForSingleObject = lambda *_: 0
        self.assertFalse(adapter.close_process({"pid": 42, "created": 900, "name": "explorer.exe"}))
        self.assertEqual(windows.posted, [])

    def test_generic_friendly_name_checks_image_from_held_process_handle(self):
        for image, allowed in [("C:\\Windows\\explorer.exe", False), (None, False),
                               ("C:\\Vendor\\editor.exe", True)]:
            with self.subTest(image=image):
                adapter, windows, closed_handles = explorer_fixture()
                adapter._process_record = disk_native.WindowsDisks._process_record.__get__(adapter)
                queried_handles = []
                def times(handle, created, *_):
                    created._obj.dwLowDateTime = 900
                    return True
                def query_image(handle, _flags, buffer, length):
                    queried_handles.append(handle)
                    if image is None:
                        return False
                    buffer.value = image
                    length._obj.value = len(image)
                    return True
                adapter.kernel.GetProcessTimes = times
                adapter.kernel.QueryFullProcessImageNameW = query_image
                adapter.kernel.WaitForSingleObject = lambda *_: 0
                windows.EnumWindows = lambda callback, _: [callback(hwnd, 0) for hwnd in sorted(windows.alive)]
                result = adapter.close_process({"pid": 42, "created": 900, "name": "Windows Explorer"})
                self.assertEqual(result, allowed)
                self.assertEqual(bool(windows.posted), allowed)
                self.assertEqual(queried_handles, [99])
                self.assertEqual(closed_handles, [99])

    def test_uasp_uses_removable_usb_device_and_never_usb_hub(self):
        nodes = [{"id": "SCSI\\DISK&VEN_EXAMPLE\\1", "caps": 0, "service": "disk"},
                 {"id": "USB\\VID_1234&PID_5678\\1", "caps": 4, "service": "UASPStor"},
                 {"id": "USB\\ROOT_HUB30\\1", "caps": 4, "service": "USBHUB3"}]
        self.assertEqual(disk_native.select_eject_node(nodes), "USB\\VID_1234&PID_5678\\1")

    def test_nonremovable_usb_device_does_not_escalate_to_removable_hub(self):
        nodes = [{"id": "SCSI\\DISK\\1", "caps": 0, "service": "disk"},
                 {"id": "USB\\ROOT_HUB30\\1", "caps": 4, "service": "USBHUB3"}]
        self.assertIsNone(disk_native.select_eject_node(nodes))

    def test_mounts_detect_real_drvfs_source_and_escaped_targets(self):
        text = ("32 1 0:5 / /mnt/custom\\040drive rw - 9p R:\\134 rw,aname=drvfs;path=R:\\134\n"
                "33 1 0:6 / /mnt/r rw - tmpfs tmpfs rw\n"
                "34 1 0:7 / /mnt/s rw - 9p S:\\134 rw,aname=drvfs;path=S:\\134\n")
        mounts = disk_native.parse_mounts("OtherLinux", text, ["R:"])
        self.assertEqual(mounts, [{"distro": "OtherLinux", "target": "/mnt/custom drive", "source": "R:\\"}])

    def test_mounts_detect_bind_mount_of_subdirectory(self):
        text = "32 1 0:5 /folder /work rw - 9p R:\\134 rw,aname=drvfs;path=R:\\134\n"
        self.assertEqual(disk_native.parse_mounts("Linux", text, ["R:"])[0]["target"], "/work")

    def test_directory_mounted_external_volume_is_detected_without_drive_letter(self):
        text = ("32 1 0:5 / /work rw - 9p C:\\134mounts\\134external rw,aname=drvfs\n"
                "33 1 0:6 / /other rw - 9p C:\\134mounts\\134external-other rw,aname=drvfs\n")
        mounts = disk_native.parse_mounts("Linux", text, [], ["C:\\mounts\\external\\"])
        self.assertEqual([m["target"] for m in mounts], ["/work"])


def explorer_fixture(close_on_post=True, rejected=False):
    adapter = object.__new__(disk_native.WindowsDisks)
    closed_handles = []
    class Kernel:
        def OpenProcess(self, *_):
            return 99
        def CloseHandle(self, handle):
            closed_handles.append(handle)
    class Windows:
        def __init__(self):
            self.alive, self.posted = {10, 11, 12, 13, 14}, []
        def IsWindow(self, hwnd):
            return hwnd in self.alive
        def GetWindowThreadProcessId(self, hwnd, pid):
            pid._obj.value = 43 if hwnd == 13 else 42
            return 1
        def PostMessageW(self, hwnd, message, *_):
            self.posted.append((hwnd, message))
            if rejected:
                return False
            if close_on_post:
                self.alive.discard(hwnd)
            return True
    adapter.kernel, adapter.user = Kernel(), Windows()
    adapter._process_record = lambda *_: {"pid": 42, "created": 900}
    adapter._path_volume = lambda path: "volume-a" if path.startswith(("R:\\", "C:\\mounts\\external\\")) else "other-volume"
    return adapter, adapter.user, closed_handles


class FakeSCM:
    """SCM boundary fake; no stop request reaches the machine."""
    def __init__(self, scenario):
        self.scenario, self.controls, self.closed = scenario, 0, []
        self.binary = '"C:\\Windows\\svc.exe"' if scenario == "windows" else '"C:\\Vendor\\svc.exe"'

    def OpenSCManagerW(self, *_):
        return 1

    def OpenServiceW(self, *_):
        return 2

    def CloseServiceHandle(self, handle):
        self.closed.append(handle)

    def QueryServiceStatusEx(self, _handle, _level, buffer, _size, _needed):
        status = disk_native.C.cast(buffer, disk_native.C.POINTER(disk_native._ServiceStatus)).contents
        status.service_type = 0x20 if self.scenario == "shared" else 0x10
        status.state = 1 if self.controls and self.scenario == "ok" else 4
        status.pid = 43 if self.scenario == "changed_pid" else 42
        status.controls = 0 if self.scenario == "not_stoppable" else 1
        return True

    def QueryServiceConfigW(self, _handle, buffer, _size, needed):
        needed._obj.value = disk_native.C.sizeof(disk_native._ServiceConfig)
        if buffer is None:
            return False
        config = disk_native.C.cast(buffer, disk_native.C.POINTER(disk_native._ServiceConfig)).contents
        config.service_type = 0x10
        config.binary = self.binary
        config.display = "Vendor"
        return True

    def QueryServiceConfig2W(self, _handle, _level, buffer, _size, _needed):
        if self.scenario == "denied":
            return False
        disk_native.C.cast(buffer, disk_native.C.POINTER(disk_native.W.DWORD)).contents.value = 1 if self.scenario == "protected" else 0
        return True

    def ControlService(self, *_):
        self.controls += 1
        return self.scenario != "dependent"


if __name__ == "__main__":
    unittest.main()
