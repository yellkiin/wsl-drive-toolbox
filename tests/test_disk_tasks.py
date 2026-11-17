import tempfile
import unittest
import os
import json
from pathlib import Path
from unittest.mock import patch

from toolbox import disk_tasks
from toolbox.disk_view import inspection_text


class DiskTaskTests(unittest.TestCase):
    def test_internal_task_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            disk_tasks.dispatch({'action':'run_command','command':'anything'})

    def test_scan_task_cannot_eject(self):
        with patch('toolbox.disks.inspect_disk', return_value={'processes':[]}) as inspect, patch('toolbox.disks.eject') as eject:
            result = disk_tasks.dispatch({'action':'inspect','disk':{'id':'demo'}})
        self.assertEqual(result, {'processes':[]})
        inspect.assert_called_once()
        eject.assert_not_called()

    def test_cleanup_passes_only_explicit_service_selection(self):
        selection = [{'name':'ExampleService','pid':123,'created':456}]
        with patch('toolbox.disks.close_and_eject', return_value={'success':False}) as cleanup:
            disk_tasks.dispatch({'action':'cleanup','disk':{'id':'demo'},'inspection':{},'selected_services':selection})
        self.assertEqual(cleanup.call_args.kwargs['selected_services'], selection)

    def test_file_worker_writes_error_result_in_request_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / 'request.json'
            request.write_text('{"action":"bad"}', encoding='utf-8')
            result = disk_tasks.task_file(request)
            self.assertEqual(result, 1)
            self.assertIn('error', disk_tasks.read_result(request))

    def test_evidence_and_history_are_distinct_in_display(self):
        info = {'processes':[{'name':'Example.exe','pid':123,'handle_count':2,'evidence':[{'source':'handle','kind':'file','path':'R:\\example.dat'}]}],
                'services':[{'name':'ExampleService','display_name':'Example Service','pid':123,'can_stop':True}],
                'recent_blocks':[{'historical':True,'time':'2026-11-17','process_name':'System','process_id':4}],
                'coverage':{'scanned_handles':10,'file_handles':12,'denied_processes':2,'partial':True}, 'warnings':[]}
        text = inspection_text(info)
        self.assertIn('R:\\example.dat', text)
        self.assertIn('ExampleService', text)
        self.assertIn('历史', text)
        self.assertIn('2', text)
        self.assertNotIn('没有占用', text)

    @unittest.skipUnless(os.name == 'nt', 'Windows elevation workflow')
    def test_result_file_is_owned_by_parent_before_elevation(self):
        observed = []
        def elevated(request_path):
            output = disk_tasks.result_path(request_path)
            observed.append(output.exists())
            output.write_text(json.dumps({'result':{'processes':[]}}), encoding='utf-8')
        with patch.object(disk_tasks, '_elevate_and_wait', side_effect=elevated):
            disk_tasks.run_elevated({'action':'inspect','disk':{}})
        self.assertEqual(observed, [True])
