import tempfile
import unittest
from pathlib import Path

from toolbox.settings import load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def test_preferences_survive_restart_without_creating_project_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'settings.json'
            save_settings({'distro': 'Ubuntu', 'user': '开发者'}, path)
            self.assertEqual(load_settings(path)['user'], '开发者')

    def test_damaged_preferences_do_not_prevent_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text('{interrupted', encoding='utf-8')
            self.assertEqual(load_settings(path), {})
