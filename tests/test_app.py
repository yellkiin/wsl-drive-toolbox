import os
import sys
import unittest
from unittest.mock import patch

@unittest.skipUnless(sys.platform == 'win32' or os.environ.get('DISPLAY'), 'GUI needs a desktop')
class AppTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from toolbox.app import ToolboxApp
        self.root = tk.Tk()
        self.app = ToolboxApp(self.root, demo=True, autoload=False)
        self.root.update()

    def tearDown(self):
        self.app._close()

    def test_activity_indicator_visible_at_default_size(self):
        self.assertTrue(self.app.progress.winfo_ismapped())
        self.assertLess(self.app.progress.winfo_rooty(), self.root.winfo_rooty() + self.root.winfo_height())
        for control, _ in self.app.controls:
            if str(control.cget('text')) == '手动端口':
                self.assertTrue(control.winfo_ismapped(), 'proxy secondary actions must remain visible')

    def test_cancel_close_dialog_does_not_start_disk_action(self):
        disk = self.app.disks.disk
        self.app.disk_rows = [disk]
        self.app.disk_tree.insert('', 'end', iid='0', values=('E:', 'demo', '1000'))
        self.app.disk_tree.selection_set('0')
        self.root.update()
        self.app.inspection = (disk['id'], self.app.disks.inspect_disk(disk))
        with patch('toolbox.app.messagebox.askokcancel', return_value=False), patch.object(self.app, 'run') as action:
            self.app.close_and_eject()
        action.assert_not_called()

    def test_new_distribution_clears_previous_user_before_query(self):
        self.app.user.set('root')
        self.app.proxy_status.set('WSL 代理已配置')
        with patch.object(self.app, 'refresh_users'):
            self.app.target_changed(new_distro=True)
        self.assertEqual(self.app.user.get(), '')
        self.assertNotEqual(self.app.proxy_status.get(), 'WSL 代理已配置')

    def test_scan_respects_explicit_proxy_family(self):
        sakura = {'app':'SakuraCat','host':'127.0.0.1','port':7897}
        clash = {'app':'Clash Verge','host':'127.0.0.1','port':7890}
        self.app.family.set('Clash Verge')
        self.app._scan_result({'candidates':[sakura,clash], 'selected':sakura, 'reason':'system', 'issues':[]})
        self.assertEqual(self.app.candidates, [clash])
        self.root.update()
        bounds = self.app.proxy_tree.bbox('0')
        self.assertTrue(bounds, 'first candidate must be visible')
        self.assertLessEqual(bounds[1] + bounds[3], self.app.proxy_tree.winfo_height())
