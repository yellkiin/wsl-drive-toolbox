"""Chinese desktop interface. OS work stays on one background worker."""
import datetime
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import __version__
from .settings import load_settings, save_settings

BG = '#f3f6fa'
INK = '#17283d'
MUTED = '#66758a'
NAVY = '#12263a'
TEAL = '#087f83'


class ToolboxApp:
    def __init__(self, root, demo=False, proxy_service=None, disk_service=None, autoload=True):
        self.root, self.demo = root, demo
        if demo:
            from .demo import DemoProxy, DemoDisks
            self.proxy, self.disks = DemoProxy(), DemoDisks()
        else:
            from . import proxy, disks
            self.proxy, self.disks = proxy_service or proxy, disk_service or disks
        self.preferences = {} if demo else load_settings()
        self.busy = False
        self.events = queue.Queue()
        self.controls = []
        self.candidates, self.disk_rows = [], []
        self.inspection = None
        self.page = 'proxy'
        self.distro = tk.StringVar(value=self.preferences.get('distro', ''))
        self.user = tk.StringVar(value=self.preferences.get('user', ''))
        self.family = tk.StringVar(value=self.preferences.get('family', '自动选择'))
        self.codex = tk.BooleanVar(value=self.preferences.get('codex', False))
        self.proxy_status = tk.StringVar(value='准备连接你的 WSL')
        self.proxy_detail = tk.StringVar(value='先打开代理软件，再点击「一键连接 WSL」。')
        self.disk_status = tk.StringVar(value='选择需要弹出的移动硬盘')
        self.disk_detail = tk.StringVar(value='正常关闭占用，交给 Windows 完成安全移除。')
        self.activity = tk.StringVar(value='演示模式 · 不操作真实系统' if demo else '就绪')
        self._build()
        self.poll_id = self.root.after(80, self._drain)
        self.root.protocol('WM_DELETE_WINDOW', self._close)
        if autoload:
            self.root.after(150, self.refresh_distros)

    def _build(self):
        r = self.root
        r.title('WSL 与移动硬盘工具箱' + (' · 演示' if self.demo else ''))
        r.geometry('1120x800')
        r.minsize(1000, 760)
        r.configure(bg=BG)
        r.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(r)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('TButton', padding=(12, 6), background='white', foreground=INK, borderwidth=1)
        style.map('TButton', background=[('active', '#e8eef4')], foreground=[('disabled', '#9aa6b5')])
        style.configure('Primary.TButton', background=TEAL, foreground='white', borderwidth=0, padding=(22, 10), font=('Microsoft YaHei UI', 12, 'bold'))
        style.map('Primary.TButton', background=[('disabled', '#91b9ba'), ('active', '#066b70')], foreground=[('disabled', '#f0f5f5')])
        style.configure('TCombobox', padding=6, fieldbackground='white')
        style.configure('Treeview', rowheight=35, background='white', fieldbackground='white', foreground=INK, borderwidth=0)
        style.configure('Treeview.Heading', background='#eaf0f5', foreground=MUTED, padding=(8, 9), font=('Microsoft YaHei UI', 9))
        style.map('Treeview', background=[('selected', '#dcefee')], foreground=[('selected', '#075c60')])
        style.configure('TCheckbutton', background='white', foreground=MUTED)
        style.configure('Horizontal.TProgressbar', background=TEAL, troughcolor=BG, borderwidth=0, thickness=3)
        sidebar = tk.Frame(r, bg=NAVY, width=210)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text='W / D', bg=NAVY, fg='#5cddd1', font=('Segoe UI', 24, 'bold')).pack(anchor='w', padx=25, pady=(30, 4))
        tk.Label(sidebar, text='WSL 工具箱', bg=NAVY, fg='white', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w', padx=25)
        tk.Label(sidebar, text='连接网络 · 安心拔盘', bg=NAVY, fg='#92aabc', font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=25, pady=(10, 32))
        self.nav = {}
        for name, label in [('proxy', '01    WSL 代理'), ('disk', '02    移动硬盘')]:
            b = tk.Button(sidebar, text=label, command=lambda n=name: self.show_page(n), anchor='w', bg=NAVY, fg='#b6c8d5', activebackground='#244156', activeforeground='white', relief='flat', bd=0, padx=24, pady=15, cursor='hand2')
            b.pack(fill='x', padx=12, pady=4)
            self.nav[name] = b
        tk.Label(sidebar, text=f'开源 · 本地运行\nv{__version__}' + ('  /  DEMO' if self.demo else ''), justify='left', bg=NAVY, fg='#92aabc', font=('Microsoft YaHei UI', 9)).pack(side='bottom', anchor='w', padx=25, pady=28)
        main = tk.Frame(r, bg=BG)
        main.pack(side='left', fill='both', expand=True, padx=30, pady=20)
        self.heading = tk.Label(main, text='连接，一步到位。', bg=BG, fg=INK, font=('Microsoft YaHei UI', 21, 'bold'))
        self.heading.pack(anchor='w')
        self.subtitle = tk.Label(main, text='自动识别 SakuraCat / Clash Verge，让 WSL 使用正确的代理。', bg=BG, fg=MUTED)
        self.subtitle.pack(anchor='w', pady=(7, 14))
        self.container = tk.Frame(main, bg=BG)
        self.pages = {k: tk.Frame(self.container, bg=BG) for k in ('proxy', 'disk')}
        self._proxy_page(self.pages['proxy'])
        self._disk_page(self.pages['disk'])
        footer = tk.Frame(main, bg=BG)
        footer.pack(side='bottom', fill='x', pady=(14, 0))
        tk.Label(footer, textvariable=self.activity, bg=BG, fg=MUTED, anchor='w').pack(side='left', fill='x', expand=True)
        ttk.Button(footer, text='操作记录', command=self.toggle_log).pack(side='right')
        self.progress = ttk.Progressbar(main, mode='indeterminate')
        self.progress.pack(side='bottom', fill='x', pady=(9, 0))
        self.log = tk.Text(main, height=5, wrap='word', bg='#e9eff5', fg=INK, bd=0, padx=10, pady=8, font=('Consolas', 9), state='disabled')
        self.container.pack(fill='both', expand=True)
        self.show_page('proxy')

    def card(self, parent):
        frame = tk.Frame(parent, bg='white', highlightbackground='#e1e8ef', highlightthickness=1)
        frame.pack(fill='x', pady=(0, 14))
        inner = tk.Frame(frame, bg='white')
        inner.pack(fill='both', expand=True, padx=21, pady=14)
        return inner

    def button(self, parent, text, command, primary=False):
        b = ttk.Button(parent, text=text, command=command, style='Primary.TButton' if primary else 'TButton')
        self.controls.append((b, 'normal'))
        return b

    def combo(self, parent, variable, values=(), width=20):
        c = ttk.Combobox(parent, textvariable=variable, values=values, state='readonly', width=width)
        self.controls.append((c, 'readonly'))
        return c

    def _proxy_page(self, parent):
        card = self.card(parent)
        tk.Label(card, text='工作环境', bg='white', fg=INK, font=('Microsoft YaHei UI', 11, 'bold')).grid(row=0, column=0, sticky='w', columnspan=4, pady=(0, 13))
        for col, text in [(0, 'WSL 发行版'), (1, 'Linux 用户'), (2, '代理来源')]:
            tk.Label(card, text=text, bg='white', fg=MUTED, font=('Microsoft YaHei UI', 9)).grid(row=1, column=col, sticky='w', padx=(0, 12), pady=(0, 5))
        self.distro_combo = self.combo(card, self.distro, width=22)
        self.distro_combo.grid(row=2, column=0, sticky='ew', padx=(0, 12))
        self.distro_combo.bind('<<ComboboxSelected>>', lambda _: self.target_changed(new_distro=True))
        self.user_combo = self.combo(card, self.user, width=16)
        self.user_combo.grid(row=2, column=1, sticky='ew', padx=(0, 12))
        self.user_combo.bind('<<ComboboxSelected>>', lambda _: self.target_changed())
        self.family_combo = self.combo(card, self.family, ['自动选择', 'SakuraCat', 'Clash Verge'], width=17)
        self.family_combo.grid(row=2, column=2, sticky='ew', padx=(0, 12))
        self.family_combo.bind('<<ComboboxSelected>>', lambda _: self.target_changed())
        self.button(card, '刷新', self.refresh_distros).grid(row=0, column=3, sticky='e', pady=(0, 9))
        for i in range(3):
            card.columnconfigure(i, weight=1)
        check = ttk.Checkbutton(card, text='同时更新 WSL 内的 Codex .env', variable=self.codex)
        check.grid(row=3, column=0, columnspan=3, sticky='w', pady=(14, 0))
        self.controls.append((check, 'normal'))
        self.button(card, '读取用户', self.refresh_users).grid(row=2, column=3)
        status = self.card(parent)
        tk.Label(status, text='WSL 连接', bg='white', fg=TEAL, font=('Microsoft YaHei UI', 9, 'bold')).pack(anchor='w')
        tk.Label(status, textvariable=self.proxy_status, bg='white', fg=INK, font=('Microsoft YaHei UI', 17, 'bold'), wraplength=740, justify='left').pack(anchor='w', pady=(8, 6))
        tk.Label(status, textvariable=self.proxy_detail, bg='white', fg=MUTED, wraplength=740, justify='left').pack(anchor='w')
        row = tk.Frame(status, bg='white')
        row.pack(fill='x', pady=(19, 0))
        self.button(row, '一键连接 WSL', self.connect, True).pack(side='left')
        self.button(row, '重新检测', self.scan).pack(side='left', padx=10)
        self.button(row, '打开新终端', self.open_terminal).pack(side='left')
        tk.Label(parent, text='可用代理   ·   检测后可点选一行，指定本次连接', bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(0, 6))
        self.proxy_tree = ttk.Treeview(parent, columns=('app', 'endpoint', 'state'), show='headings', height=3, selectmode='browse')
        for key, title, width in [('app', '代理软件', 170), ('endpoint', 'WSL 可达地址', 220), ('state', '验证结果', 240)]:
            self.proxy_tree.heading(key, text=title)
            self.proxy_tree.column(key, width=width, anchor='w')
        self.proxy_tree.bind('<Button-1>', self._freeze_selection)
        self.proxy_tree.bind('<Key>', self._freeze_selection)
        row = tk.Frame(parent, bg=BG)
        row.pack(side='bottom', fill='x', pady=(14, 0))
        for title, fn in [('关闭显式代理', self.disable), ('回滚上次配置', self.rollback), ('网络诊断', self.diagnose), ('手动端口', self.manual)]:
            self.button(row, title, fn).pack(side='left', padx=(0, 9))
        self.proxy_tree.pack(fill='both', expand=True)

    def _disk_page(self, parent):
        status = self.card(parent)
        tk.Label(status, text='移动硬盘', bg='white', fg=TEAL, font=('Microsoft YaHei UI', 9, 'bold')).pack(anchor='w')
        tk.Label(status, textvariable=self.disk_status, bg='white', fg=INK, font=('Microsoft YaHei UI', 17, 'bold'), wraplength=740, justify='left').pack(anchor='w', pady=(8, 6))
        tk.Label(status, textvariable=self.disk_detail, bg='white', fg=MUTED, wraplength=740, justify='left').pack(anchor='w')
        row = tk.Frame(status, bg='white')
        row.pack(fill='x', pady=(18, 0))
        self.button(row, '安全弹出', self.eject, True).pack(side='left')
        self.button(row, '查看占用', self.inspect_disk).pack(side='left', padx=10)
        self.button(row, '刷新硬盘', self.refresh_disks).pack(side='left')
        self.disk_tree = ttk.Treeview(parent, columns=('letters', 'name', 'size'), show='headings', height=3, selectmode='browse')
        for key, title, width in [('letters', '盘符', 100), ('name', '设备', 370), ('size', '容量', 130)]:
            self.disk_tree.heading(key, text=title)
            self.disk_tree.column(key, width=width, anchor='w')
        self.disk_tree.pack(fill='x', pady=(0, 14))
        self.disk_tree.bind('<<TreeviewSelect>>', self._disk_changed)
        self.disk_tree.bind('<Button-1>', self._freeze_selection)
        self.disk_tree.bind('<Key>', self._freeze_selection)
        panel = self.card(parent)
        top = tk.Frame(panel, bg='white')
        top.pack(fill='x')
        tk.Label(top, text='占用情况', bg='white', fg=INK, font=('Microsoft YaHei UI', 11, 'bold')).pack(side='left')
        self.button(top, '正常关闭占用并重试', self.close_and_eject).pack(side='right')
        self.occupancy = tk.Text(panel, height=6, wrap='word', bg='white', fg=MUTED, bd=0, font=('Microsoft YaHei UI', 10), state='disabled')
        self.occupancy.pack(fill='both', expand=True, pady=(10, 0))
        self._occupancy_text('选择硬盘后点击「查看占用」。\n只请求程序正常退出；遇到保存提示，请先完成保存。')
        tk.Label(parent, text='只有显示「可以拔出」后，再拔下硬盘。系统拒绝移除时会保留连接。', bg=BG, fg=MUTED, wraplength=740, justify='left', font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(0, 5))

    def show_page(self, page):
        self.page = page
        for key, frame in self.pages.items():
            frame.pack_forget()
            self.nav[key].configure(bg='#244156' if key == page else NAVY, fg='white' if key == page else '#b6c8d5')
        self.pages[page].pack(fill='both', expand=True)
        self.heading.configure(text='连接，一步到位。' if page == 'proxy' else '拔出之前，交给工具箱。')
        self.subtitle.configure(text='自动识别 SakuraCat / Clash Verge，让 WSL 使用正确的代理。' if page == 'proxy' else '找到占用，正常关闭，让 Windows 确认安全移除。')
        if page == 'disk' and not self.disk_rows and not self.busy:
            self.refresh_disks()

    def _log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', datetime.datetime.now().strftime('%H:%M:%S') + '  ' + str(text) + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 200:
            self.log.delete('1.0', '30.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def toggle_log(self):
        if self.log.winfo_manager():
            self.log.pack_forget()
        else:
            self.log.pack(side='bottom', fill='x', pady=(8, 0), before=self.container)

    def _freeze_selection(self, _):
        return 'break' if self.busy else None

    def run(self, label, work, done, quiet=False):
        if self.busy:
            return
        self.busy = True
        self.activity.set(label + '…')
        self._log(label)
        for control, _ in self.controls:
            control.configure(state='disabled')
        self.progress.start(15)

        def worker():
            try:
                self.events.put((done, work(), None, quiet))
            except Exception as error:
                self.events.put((done, None, error, quiet))
        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            done, result, error, quiet = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.progress.stop()
            for control, state in self.controls:
                control.configure(state=state)
            if error:
                self.activity.set('未完成 · 查看提示')
                self._log(str(error))
                if hasattr(error, 'candidates'):
                    self._scan_result({'candidates': error.candidates, 'selected': None, 'reason': error.reason, 'issues': []})
                    self.proxy_status.set('请选择本次使用的代理')
                elif not quiet:
                    messagebox.showerror('操作未完成', str(error), parent=self.root)
            else:
                self.activity.set('演示模式 · 不操作真实系统' if self.demo else '就绪')
                try:
                    done(result)
                except Exception as callback_error:
                    self._log(str(callback_error))
                    messagebox.showerror('显示结果时发生错误', str(callback_error), parent=self.root)
        self.poll_id = self.root.after(80, self._drain)

    def _remember(self):
        if not self.demo:
            self.preferences.update(distro=self.distro.get(), user=self.user.get(), family=self.family.get(), codex=self.codex.get())
            try:
                save_settings(self.preferences)
            except OSError as error:
                self._log('偏好未保存：' + str(error))

    def target(self):
        distro, user = self.distro.get(), self.user.get()
        if not distro or not user:
            messagebox.showinfo('选择工作环境', '请选择发行版并点击「读取用户」，再选择你日常使用的 Linux 用户。', parent=self.root)
            return None
        self._remember()
        return distro, user

    def refresh_distros(self):
        def done(rows):
            names = [r['name'] for r in rows]
            self.distro_combo['values'] = names
            if self.distro.get() not in names:
                self.distro.set(next((r['name'] for r in rows if r.get('default')), names[0] if names else ''))
                self.user.set('')
            if self.user.get():
                self.user_combo['values'] = [self.user.get()]
            self.proxy_status.set('准备连接你的 WSL' if names else '尚未发现 WSL 发行版')
            self._log('已发现 ' + str(len(names)) + ' 个 WSL 发行版。')
        self.run('读取 WSL 发行版', self.proxy.list_distros, done)

    def refresh_users(self):
        distro = self.distro.get()
        if not distro:
            return
        self.candidates = []
        self.proxy_tree.delete(*self.proxy_tree.get_children())
        def done(users):
            self.user_combo['values'] = users
            if self.user.get() not in users:
                normal = [u for u in users if u != 'root']
                self.user.set(normal[0] if len(normal) == 1 else '')
            self.proxy_detail.set('请选择日常使用的 Linux 用户；此操作可能启动所选发行版。')
        self.run('读取 Linux 用户（可能启动所选 WSL）', lambda: self.proxy.list_users(distro), done)

    def target_changed(self, new_distro=False):
        self.candidates = []
        self.proxy_tree.delete(*self.proxy_tree.get_children())
        self.proxy_status.set('准备连接所选环境')
        self.proxy_detail.set('点击「一键连接 WSL」检测并应用当前代理。')
        if new_distro:
            self.user.set('')
            self.user_combo['values'] = []
            self.refresh_users()

    def _scan_result(self, result):
        self.candidates = result.get('candidates', [])
        selected = result.get('selected')
        reason = result.get('reason', '')
        if self.family.get() != '自动选择':
            self.candidates = [c for c in self.candidates if c['app'] == self.family.get()]
            selected = self.candidates[0] if len(self.candidates) == 1 else None
            reason = '已筛选 ' + self.family.get() + '；点击连接，或从列表中选择端口。'
        self.proxy_tree.delete(*self.proxy_tree.get_children())
        for i, c in enumerate(self.candidates):
            self.proxy_tree.insert('', 'end', iid=str(i), values=(c['app'], f"{c['host']}:{c['port']}", '本机代理验证通过'))
            if c == selected:
                self.proxy_tree.selection_set(str(i))
        self.proxy_status.set(f'发现 {len(self.candidates)} 个可用代理' if self.candidates else '没有找到可确认的代理')
        self.proxy_detail.set(reason or '选中一行后点击「一键连接 WSL」。')
        for issue in result.get('issues', []):
            self._log(str(issue))

    def scan(self):
        target = self.target()
        if target:
            self.run('检测实际代理端口', lambda: self.proxy.scan(*target), self._scan_result)

    def connect(self, candidate=None):
        target = self.target()
        if not target:
            return
        selected = self.proxy_tree.selection()
        if candidate is None and selected:
            candidate = self.candidates[int(selected[0])]
        family = None if self.family.get() == '自动选择' else self.family.get()
        codex = self.codex.get()
        self.run('验证并连接 WSL', lambda: self.proxy.connect(*target, candidate=candidate, family=family, codex=codex), self._connected)

    def _connected(self, result):
        c = result.get('candidate')
        self.proxy_status.set('WSL 代理已配置')
        self.proxy_detail.set((f"{c['app']}  ·  {c['host']}:{c['port']}\n" if c else '') + result.get('message', '请打开新终端加载代理。'))
        self._log(result.get('message', '已完成'))
        self.candidates = []
        self.proxy_tree.delete(*self.proxy_tree.get_children())

    def disable(self):
        target = self.target()
        codex = self.codex.get()
        if target:
            self.run('关闭显式代理', lambda: self.proxy.disable(*target, codex=codex), self._proxy_message)

    def rollback(self):
        target = self.target()
        if target:
            self.run('回滚上次配置', lambda: self.proxy.rollback(*target), self._proxy_message)

    def _proxy_message(self, result):
        self.proxy_status.set('配置操作已完成')
        self.proxy_detail.set(result.get('message', ''))
        self._log(result.get('message', ''))

    def diagnose(self):
        target = self.target()
        if target:
            self.proxy_detail.set('正在测试外网 HTTPS：https://example.com/（验证 TLS 证书）。')
            self.run('网络诊断', lambda: self.proxy.diagnose(*target), self._diagnosis_result)

    def _diagnosis_result(self, result):
        self.proxy_status.set('网络诊断结果')
        self.proxy_detail.set(result.get('message', ''))
        self._log(result.get('message', ''))

    def manual(self):
        port = simpledialog.askinteger('手动 HTTP / 混合端口', '输入代理软件当前的 HTTP 或混合端口：', parent=self.root, minvalue=1, maxvalue=65535)
        if port:
            self.connect({'app': '手动', 'host': '127.0.0.1', 'port': port})

    def open_terminal(self):
        target = self.target()
        if target:
            self.run('打开新终端', lambda: self.proxy.open_terminal(*target), lambda _: self._log('已请求打开新终端。'))

    def refresh_disks(self):
        def done(rows):
            self.disk_rows = rows
            self.disk_tree.delete(*self.disk_tree.get_children())
            for i, d in enumerate(rows):
                self.disk_tree.insert('', 'end', iid=str(i), values=(', '.join(d['letters']) or '无盘符', d['name'], f"{d['size'] / 1e9:.0f} GB"))
            if rows:
                chosen = next((i for i, d in enumerate(rows) if d['id'] == self.preferences.get('disk_id')), 0)
                self.disk_tree.selection_set(str(chosen))
            else:
                self.disk_status.set('未发现可弹出的外接硬盘')
                self.disk_detail.set('请连接 USB 硬盘后刷新。')
            self.inspection = None
        self.run('读取外接磁盘', self.disks.list_disks, done)

    def _disk_changed(self, _=None):
        self.inspection = None
        disk = self.selected_disk(prompt=False)
        if disk:
            self.preferences['disk_id'] = disk['id']
            self._remember()
            self.disk_status.set(', '.join(disk['letters']) + '  ' + disk['name'])
            self.disk_detail.set(f"{disk['size'] / 1e9:.0f} GB · 选择「安全弹出」，无占用时一步完成。")
            self._occupancy_text('尚未检查占用。点击「查看占用」获取程序和 WSL 信息。')

    def selected_disk(self, prompt=True):
        selection = self.disk_tree.selection()
        if not selection:
            if prompt:
                messagebox.showinfo('选择硬盘', '请先刷新并选择一个外接硬盘。', parent=self.root)
            return None
        return self.disk_rows[int(selection[0])]

    def _occupancy_text(self, text):
        self.occupancy.configure(state='normal')
        self.occupancy.delete('1.0', 'end')
        self.occupancy.insert('end', text)
        self.occupancy.configure(state='disabled')

    def inspect_disk(self):
        disk = self.selected_disk()
        if disk:
            def done(info):
                self.inspection = (disk['id'], info)
                lines = [f"程序：{p['name']}  (PID {p['pid']})" for p in info.get('processes', [])]
                lines += ['盘上 WSL：' + d for d in info.get('distros', [])]
                lines += [f"WSL 挂载：{m['distro']} → {m['target']}" for m in info.get('mounts', [])]
                lines += info.get('warnings', [])
                self._occupancy_text('\n'.join(lines) or '未识别到占用程序；最终以 Windows 移除结果为准。')
                self._log('占用检查完成。')
            self.run('检查硬盘占用', lambda: self.disks.inspect_disk(disk), done)

    def eject(self):
        disk = self.selected_disk()
        if disk:
            self.run('请求 Windows 安全移除', lambda: self.disks.eject(disk), lambda result: self._eject_result(disk, result))

    def close_and_eject(self):
        disk = self.selected_disk()
        if not disk:
            return
        if not self.inspection or self.inspection[0] != disk['id']:
            self.inspect_disk()
            return
        info = self.inspection[1]
        objects = [p['name'] for p in info.get('processes', [])]
        objects += ['停止 WSL：' + d for d in info.get('distros', [])]
        objects += [f"卸载：{m['distro']} {m['target']}" for m in info.get('mounts', [])]
        if objects and not messagebox.askokcancel('正常关闭占用并弹出', '将处理以下占用：\n\n' + '\n'.join(objects) + '\n\n请先保存文件和 WSL 任务。程序弹出保存提示时，请完成保存；不会强制结束程序。', parent=self.root):
            return
        self.run('正常释放占用并请求移除', lambda: self.disks.close_and_eject(disk, info), lambda result: self._eject_result(disk, result))

    def _eject_result(self, disk, result):
        success = result.get('success', False)
        label = ', '.join(disk['letters']) + ' · ' + disk['name']
        self.disk_status.set(label + (' 可以拔出' if success else ' 暂时无法弹出'))
        self.disk_detail.set(result.get('message', ''))
        self._log(result.get('message', ''))
        if success:
            self.inspection = None
            self.disk_tree.selection_remove(*self.disk_tree.selection())
            self._occupancy_text('Windows 已完成安全移除。重新插入后点击「刷新硬盘」。')

    def _close(self):
        if self.busy:
            messagebox.showinfo('操作进行中', '请等待当前操作返回结果后关闭窗口。', parent=self.root)
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def launch(demo=False):
    root = tk.Tk()
    ToolboxApp(root, demo=demo)
    root.mainloop()
