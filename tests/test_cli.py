import unittest
from toolbox.__main__ import main


class CliTests(unittest.TestCase):
    def test_demo_cannot_run_live_cli_commands(self):
        with self.assertRaises(SystemExit) as raised:
            main(['--demo', '--cli', 'off', '--distro', 'demo', '--user', 'root'])
        self.assertEqual(raised.exception.code, 2)
