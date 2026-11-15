import copy
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

    def release_mount(self, mount):
        self.events.append(("umount", mount["distro"], mount["target"]))
        return not self.refuse_mount

    def stop_distro(self, name):
        self.events.append(("stop", name))

    def request_eject(self, disk):
        self.events.append(("eject", disk["id"]))
        return self.result


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

    def test_refused_normal_close_prevents_eject(self):
        self.native.info["processes"] = [{"pid": 42, "name": "editor", "created": 900}]
        self.native.refuse_close = True
        result = disks.close_and_eject(self.target, copy.deepcopy(self.native.info))
        self.assertFalse(result["success"])
        self.assertEqual(self.native.events, [("close", 42)])

    def test_new_process_requires_new_confirmation(self):
        accepted = copy.deepcopy(self.native.info)
        self.native.info["processes"] = [{"pid": 42, "name": "editor", "created": 900}]
        result = disks.close_and_eject(self.target, accepted)
        self.assertFalse(result["success"])
        self.assertEqual(self.native.events, [])

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


class NativeRulesTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
