import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from toolbox.__main__ import main


class CliTests(unittest.TestCase):
    def test_renamed_frozen_launcher_defaults_to_quick_connect_and_preserves_explicit_modes(self):
        import sys
        launch = Mock()
        with patch.object(sys, 'frozen', True, create=True), \
             patch.object(sys, 'executable', 'Toolbox (1).exe'), \
             patch.dict(sys.modules, {'toolbox.app': SimpleNamespace(launch=launch)}):
            self.assertEqual(main([]), 0)
            launch.assert_called_once_with(demo=False, quick_connect=True)
            self.assertEqual(main(['--demo']), 0)
            launch.assert_called_with(demo=True, quick_connect=False)
            with patch('toolbox.disk_tasks.task_file', return_value=0) as worker:
                self.assertEqual(main(['--internal-disk-task', 'request']), 0)
            worker.assert_called_once_with('request')

    def test_demo_cannot_run_live_cli_commands(self):
        with self.assertRaises(SystemExit) as raised:
            main(['--demo', '--cli', 'off', '--distro', 'demo', '--user', 'root'])
        self.assertEqual(raised.exception.code, 2)
