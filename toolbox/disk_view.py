"""Plain-language rendering of current evidence and historical removal events."""


def inspection_text(info):
    lines = []
    coverage = info.get('coverage') or {}
    if coverage:
        lines.append('句柄扫描：已检查 {0} / {1} 个文件类句柄；{2} 个进程无权读取。'.format(
            coverage.get('scanned_handles', 0), coverage.get('file_handles', 0), coverage.get('denied_processes', 0)))
        if coverage.get('timed_out'):
            lines.append('扫描已到时间上限；下方保留已找到的占用。')
        if coverage.get('unresolved_names'):
            lines.append('无法解析名称的句柄：' + str(coverage['unresolved_names']))
        if coverage.get('partial') and not coverage.get('admin'):
            lines.append('部分系统服务可能未覆盖，可点击「管理员扫描」。')
        lines.append('')
    processes = info.get('processes', [])
    if processes:
        lines.append('当前关联进程')
    for process in processes:
        lines.append('{0}  ·  PID {1}  ·  匹配句柄 {2}'.format(process['name'], process['pid'], process.get('handle_count', 0)))
        for evidence in process.get('evidence', [])[:6]:
            source = {'handle':'打开的对象', 'image':'运行位置', 'restart_manager':'系统资源关联'}.get(evidence.get('source'), '关联证据')
            lines.append('  ' + source + '：' + evidence.get('path', evidence.get('kind', '已确认'))) 
        if process.get('close_reason'):
            lines.append('  ' + process['close_reason'])
    services = info.get('services', [])
    if services:
        lines += ['', '关联服务（共享宿主内的服务不等于逐项已确认占用）']
        for service in services:
            action = '可选择正常停止' if service.get('can_stop') else service.get('reason', '需人工处理')
            lines.append('{0} [{1}] · PID {2} · {3}'.format(service.get('display_name', service['name']), service['name'], service['pid'], action))
    for name in info.get('distros', []):
        lines.append('盘上运行的 WSL：' + name)
    for mount in info.get('mounts', []):
        lines.append('WSL 挂载：' + mount['distro'] + ' → ' + mount['target'])
    history = info.get('recent_blocks', [])
    if history:
        lines += ['', '历史弹出失败记录（不代表进程现在仍占用）']
        for entry in history[:8]:
            lines.append('{0} · {1} · PID {2}'.format(entry.get('time', ''), entry.get('process_name', ''), entry.get('process_id', '')))
    warnings = info.get('warnings', [])
    if warnings:
        lines += ['', *warnings]
    if not processes and not services and not info.get('distros') and not info.get('mounts'):
        lines.append('本次范围内未找到当前关联对象；这不等于系统已允许拔盘。')
    return '\n'.join(lines)
