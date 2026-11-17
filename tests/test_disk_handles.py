import ctypes as C
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from toolbox import disk_handles as handles


class HandleMatchingTests(unittest.TestCase):
    def test_volume_and_physical_aliases_use_actual_dos_device_map(self):
        mapping = {'Volume{sample}': [r'\Device\HarddiskVolume19'],
                   'E:': [r'\Device\HarddiskVolume19'],
                   'PhysicalDrive3': [r'\Device\Harddisk3\DR3']}
        disk = {'number': 3, 'letters': ['E:'], 'volumes': [
            {'id': '\\\\?\\Volume{sample}\\', 'letters': ['E:']}]}
        aliases = handles.build_aliases(disk, lambda name: mapping.get(name, []))
        self.assertEqual(handles.match_name(r'\Device\HarddiskVolume19\a.txt', aliases),
                         {'path': r'E:\a.txt', 'kind': 'file'})
        self.assertEqual(handles.match_name(r'\Device\HarddiskVolume19', aliases),
                         {'path': 'E:\\', 'kind': 'volume'})
        self.assertEqual(handles.match_name(r'\Device\Harddisk3\DR3', aliases)['kind'], 'physical_disk')
        for other in (r'\Device\HarddiskVolume190\a', r'\Device\Harddisk30\DR3', r'\Device\HarddiskVolume3\a'):
            self.assertIsNone(handles.match_name(other, aliases))

    def test_guid_only_volume_and_directory_names_are_preserved(self):
        disk = {'number': 4, 'volumes': [{'id': '\\\\?\\Volume{sample}\\', 'letters': []}]}
        aliases = handles.build_aliases(disk, lambda name: [r'\Device\HarddiskVolume8'] if name == 'Volume{sample}' else [])
        self.assertEqual(handles.match_name(r'\Device\HarddiskVolume8\data', aliases, directory=True),
                         {'path': '\\\\?\\Volume{sample}\\data', 'kind': 'directory'})

    def test_extended_snapshot_uses_pointer_sized_pid_and_handle(self):
        # A real class 64 binary layout, independently packed from the PHNT definition.
        fmt = '<QQQIHHII' if C.sizeof(C.c_void_p) == 8 else '<IIIIHHII'
        pointer = '<Q' if C.sizeof(C.c_void_p) == 8 else '<I'
        data = struct.pack(pointer, 1) + struct.pack(pointer, 0)
        data += struct.pack(fmt, 0x1234, 70000, 0x123456, 0x120089, 0, 42, 0, 0)
        rows = handles.parse_snapshot(data)
        self.assertEqual((rows[0].UniqueProcessId, rows[0].HandleValue, rows[0].ObjectTypeIndex), (70000, 0x123456, 42))
        with self.assertRaises(ValueError):
            handles.parse_snapshot(data[:-1])

    def test_unicode_name_obeys_byte_length_not_nul_termination(self):
        text = 'E:\\中文\x00trailing'.encode('utf-16-le')
        buf = C.create_string_buffer(text)
        info = handles.UNICODE_STRING()
        info.Length = len('E:\\中文'.encode('utf-16-le'))
        info.MaximumLength = len(text)
        info.Buffer = C.addressof(buf)
        self.assertEqual(handles.decode_unicode_string(info), 'E:\\中文')

    def test_final_progress_cannot_clear_parent_timeout(self):
        result = {'coverage': {'timed_out': True, 'scanned_handles': 2}, 'warnings': []}
        handles._consume_line(json.dumps({'type': 'progress', 'coverage': {
            'timed_out': False, 'scanned_handles': 7}}), result, {})
        self.assertTrue(result['coverage']['timed_out'])
        self.assertEqual(result['coverage']['scanned_handles'], 7)

    def test_timeout_preserves_streamed_matches_and_coverage(self):
        fixture = ('import json,time,sys; out=open(sys.argv[2], \"w\", encoding=\"utf-8\"); '
                   'print(json.dumps({"type":"progress","coverage":{"scanned_handles":7,"admin":False}}),file=out,flush=True); '
                   'print(json.dumps({"type":"process","process":{"pid":42,"name":"holder.exe","created":123,"executable":"C:\\\\holder.exe","evidence":[{"source":"handle","path":"E:\\\\data","kind":"file"}],"handle_count":1}}),file=out,flush=True); '
                   'time.sleep(30)')
        with patch.object(handles, '_worker_command', return_value=[sys.executable, '-c', fixture]):
            result = handles.scan({}, timeout=0.8)
        self.assertEqual(result['processes'][0]['pid'], 42)
        self.assertEqual(result['coverage']['scanned_handles'], 7)
        self.assertTrue(result['coverage']['timed_out'])
        self.assertTrue(result['coverage']['partial'])
        self.assertTrue(result['warnings'])


@unittest.skipUnless(os.name == 'nt', 'Windows native handle scan')
class LiveHandleTests(unittest.TestCase):
    def test_separate_process_open_temp_file_is_found_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'handle-occupancy-fixture.txt'
            path.write_text('fixture', encoding='utf-8')
            script = (
                'import sys,time,ctypes as C; from pathlib import Path; '
                'f=open(sys.argv[1],"rb"); k=C.WinDLL("kernel32",use_last_error=True); '
                'k.CreateFileW.argtypes=[C.c_wchar_p,C.c_uint32,C.c_uint32,C.c_void_p,C.c_uint32,C.c_uint32,C.c_void_p]; '
                'k.CreateFileW.restype=C.c_void_p; '
                'directory=k.CreateFileW(str(Path(sys.argv[1]).parent),0,7,None,3,0x02000000,None); '
                'assert directory not in (None,C.c_void_p(-1).value); '
                'print("ready",flush=True); time.sleep(90)')
            child = subprocess.Popen([sys.executable, '-u', '-c', script, str(path)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                drive = path.drive
                result = handles.scan({'number': -1, 'letters': [drive], 'volumes': []}, timeout=24)
                matches = [p for p in result['processes'] if p['pid'] == child.pid]
                self.assertTrue(matches, result)
                self.assertIsInstance(matches[0]['created'], int)
                self.assertGreater(matches[0]['created'], 0)
                self.assertTrue(any(e['path'].lower() == str(path).lower() and e['kind'] == 'file' for e in matches[0]['evidence']), matches)
                self.assertTrue(any(e['path'].lower() == directory.lower() and e['kind'] == 'directory' for e in matches[0]['evidence']), matches)
                # Scanner must never close the source handle or stop the holder.
                self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()


if __name__ == '__main__':
    unittest.main()
