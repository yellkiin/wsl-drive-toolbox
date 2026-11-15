"""Explicit demo backend for previews: all data is fictional, no system calls."""
from copy import deepcopy


class DemoProxy:
    def list_distros(self):
        return [{'name': 'Ubuntu', 'running': True, 'default': True}]

    def list_users(self, distro):
        return ['developer']

    def scan(self, distro, user):
        c = {'app': 'Clash Verge', 'host': '127.0.0.1', 'port': 7897, 'pid': 1234, 'created': 'demo'}
        return {'candidates': [c], 'selected': c, 'reason': '匹配 Windows 当前系统代理', 'issues': []}

    def connect(self, distro, user, candidate=None, family=None, codex=False):
        return {'message': '演示：代理配置已更新，新终端将使用此代理。', 'candidate': candidate or self.scan(distro, user)['selected']}

    def disable(self, *args, **kwargs):
        return {'message': '演示：已关闭本工具管理的显式代理。'}

    def rollback(self, *args):
        return {'message': '演示：已恢复上一次配置。'}

    def diagnose(self, *args):
        return {'message': '演示：HTTPS 测试通过。实际测试会访问 https://example.com/。'}

    def open_terminal(self, *args):
        return None


class DemoDisks:
    disk = {'id': 'demo-drive', 'name': 'Portable SSD', 'size': 1000000000000, 'letters': ['E:']}

    def list_disks(self):
        return [deepcopy(self.disk)]

    def inspect_disk(self, disk):
        return {'processes': [{'pid': 4321, 'name': '文件编辑器', 'created': 'demo'}], 'distros': ['Ubuntu-Archive'], 'mounts': [], 'warnings': ['演示数据；未扫描或操作真实硬盘。']}

    def eject(self, disk):
        return {'success': False, 'message': '演示：硬盘仍被占用。可查看占用并正常关闭后重试。'}

    def close_and_eject(self, disk, inspection):
        return {'success': True, 'message': '演示：Windows 已允许安全移除，可以拔出。'}
