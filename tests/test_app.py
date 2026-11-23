import os
import sys
import unittest
import importlib.util
from unittest.mock import patch

@unittest.skipUnless(importlib.util.find_spec('tkinter') and (sys.platform == 'win32' or os.environ.get('DISPLAY')), 'GUI needs Tk and a desktop')
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
            if str(control.cget('text')) == '手动地址':
                self.assertTrue(control.winfo_ismapped(), 'proxy secondary actions must remain visible')

    def test_cancel_close_dialog_does_not_start_disk_action(self):
        disk = self.app.disks.disk
        self.app.disk_rows = [disk]
        self.app.disk_tree.insert('', 'end', iid='0', values=('E:', 'demo', '1000'))
        self.app.disk_tree.selection_set('0')
        self.root.update()
        info = self.app.disks.inspect_disk(disk)
        info['services'] = []
        self.app.inspection = (disk['id'], info)
        with patch('toolbox.app.messagebox.askokcancel', return_value=False), patch.object(self.app, 'run') as action:
            self.app.close_and_eject()
        action.assert_not_called()

    def test_cancel_service_selection_does_not_launch_elevated_cleanup(self):
        disk = self.app.disks.disk
        self.app.disk_rows = [disk]
        self.app.disk_tree.insert('', 'end', iid='0', values=('E:', 'demo', '1000'))
        self.app.disk_tree.selection_set('0')
        self.root.update()
        self.app.inspection = (disk['id'], self.app.disks.inspect_disk(disk))
        with patch.object(self.app, '_select_services', return_value=None), patch.object(self.app, 'run') as action:
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

    def test_scan_recommendation_does_not_pin_upstream_but_manual_selection_does(self):
        self.app.distro.set('Ubuntu')
        self.app.user.set('developer')
        fresh = self.app.proxy.scan('Ubuntu', 'developer')
        self.app._scan_result(fresh)
        self.assertIn(fresh['selected']['app'], self.app.proxy_detail.get())
        self.assertFalse(self.app.proxy_tree.selection())
        with patch.object(self.app, 'run', side_effect=lambda _, work, done: work()), \
             patch.object(self.app.proxy, 'connect') as connect:
            self.app.connect()
            connect.assert_called_with('Ubuntu', 'developer', candidate=None, family=None, codex=False)
            self.app.proxy_tree.selection_set('1')
            self.app.connect()
            connect.assert_called_with('Ubuntu', 'developer', candidate=fresh['candidates'][1], family=None, codex=False)
            self.app._scan_result(fresh)
            self.assertFalse(self.app.proxy_tree.selection())

    def test_connection_card_shows_git_notice_and_keeps_controls_visible(self):
        notice = 'Git 的个人代理已迁移到固定入口。'
        result = self.app.proxy.connect('Ubuntu', 'developer')
        self.app._connected(dict(result, git_notice=notice))
        self.root.update()
        self.assertIn(notice, self.app.proxy_detail.get().splitlines())
        button = next(c for c, _ in self.app.controls if c.cget('text') == '一键连接 WSL')
        self.assertTrue(button.winfo_ismapped())
        self.assertLess(button.winfo_rooty() + button.winfo_height(), self.app.proxy_tree.winfo_rooty())

    def test_quick_connect_uses_saved_account_and_fresh_automatic_proxy(self):
        self.app.distro.set('Ubuntu')
        self.app.user.set('alice')
        self.app.family.set('Old proxy')
        self.app.codex.set(True)
        with patch.object(self.app, 'run', side_effect=lambda _, work, done: done(work())), \
             patch.object(self.app.proxy, 'list_distros', return_value=[{'name': 'Ubuntu'}]), \
             patch.object(self.app.proxy, 'list_users', return_value=['alice', 'root']), \
             patch.object(self.app.proxy, 'connect', return_value={}) as connect:
            self.app.refresh_distros(quick_connect=True)
        connect.assert_called_once_with('Ubuntu', 'alice', candidate=None, family=None, codex=True)

    def test_quick_connect_waits_for_selection_on_missing_changed_or_root_account(self):
        for distro, user in [('', ''), ('Gone', 'alice'), ('Ubuntu', 'gone'), ('Ubuntu', 'root')]:
            with self.subTest(distro=distro, user=user):
                self.app.distro.set(distro)
                self.app.user.set(user)
                with patch.object(self.app, 'run', side_effect=lambda _, work, done: done(work())), \
                     patch.object(self.app.proxy, 'list_distros', return_value=[{'name': 'Ubuntu'}]), \
                     patch.object(self.app.proxy, 'list_users', return_value=['alice', 'root']), \
                     patch.object(self.app.proxy, 'connect') as connect:
                    self.app.refresh_distros(quick_connect=True)
                connect.assert_not_called()

    def test_read_users_displays_default_and_preserves_explicit_selection(self):
        self.app.distro.set('Ubuntu')
        with patch.object(self.app, 'run', side_effect=lambda _, work, done: done(work())), \
             patch.object(self.app.proxy, 'list_users', return_value=['alice', 'sam', 'root']), \
             patch.object(self.app.proxy, 'default_user', return_value='sam'):
            self.app.refresh_users()
            self.assertEqual(self.app.user.get(), 'sam')
            self.assertIn('默认登录', self.app.user_label.cget('text'))
            self.assertIn('sam', self.app.user_label.cget('text'))
            self.app.user.set('alice')
            self.app.refresh_users()
            self.assertEqual(self.app.user.get(), 'alice')

    def test_quick_connect_does_not_switch_to_a_different_default_user(self):
        self.app.user.set('gone')
        self.app.distro.set('Ubuntu')
        with patch.object(self.app, 'run', side_effect=lambda _, work, done: done(work())), \
             patch.object(self.app.proxy, 'list_users', return_value=['alice', 'sam', 'root']), \
             patch.object(self.app.proxy, 'default_user', return_value='sam'), \
             patch.object(self.app.proxy, 'connect') as connect:
            self.app.refresh_users(quick_connect=True)
            self.assertEqual(self.app.user.get(), 'sam')
            connect.assert_not_called()

    def test_default_root_is_not_preselected(self):
        self.app.distro.set('Ubuntu')
        with patch.object(self.app, 'run', side_effect=lambda _, work, done: done(work())), \
             patch.object(self.app.proxy, 'list_users', return_value=['alice', 'sam', 'root']), \
             patch.object(self.app.proxy, 'default_user', return_value='root'):
            self.app.refresh_users()
            self.assertEqual(self.app.user.get(), '')
            self.assertIn('默认登录', self.app.user_label.cget('text'))

    def test_unknown_process_is_selectable_and_unverified_row_not_labeled_passed(self):
        row = {'app':'AnyProxy','host':'proxy.example','port':8080,'protocol':'http','verified':False,'source':'system'}
        self.app._scan_result({'candidates':[row], 'selected':None, 'reason':'待验证', 'issues':[]})
        self.assertIn('AnyProxy', self.app.family_combo['values'])
        self.assertIn('未确认', str(self.app.proxy_tree.item('0')['values']))
        self.assertFalse(self.app.proxy_tree.selection())
