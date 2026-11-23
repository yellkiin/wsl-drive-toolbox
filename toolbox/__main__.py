"""GUI by default; explicit --cli for scripts and diagnostics."""
import argparse
import json
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and getattr(sys, 'frozen', False):
        argv = ['--quick-connect']
    if argv and argv[0] == '--internal-handle-worker':
        from .disk_handles import worker_main
        return worker_main(*argv[1:])
    if len(argv) == 2 and argv[0] == '--internal-disk-task':
        from .disk_tasks import task_file
        return task_file(argv[1])
    parser = argparse.ArgumentParser(description='WSL 与移动硬盘工具箱')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--demo', action='store_true', help='使用演示数据打开界面，不操作系统')
    mode.add_argument('--quick-connect', action='store_true', help='使用上次选择的 WSL 账号，重新检测并连接当前代理')
    mode.add_argument('--cli', choices=['status', 'scan', 'connect', 'off', 'rollback', 'diagnose', 'apps'])
    parser.add_argument('--distro')
    parser.add_argument('--user')
    parser.add_argument('--family', help='按检测到的程序标签筛选（可选）')
    parser.add_argument('--extended', action='store_true', help='扫描全部本机 TCP 监听端点')
    parser.add_argument('--codex', action='store_true')
    args = parser.parse_args(argv)
    if not args.cli:
        from .app import launch
        launch(demo=args.demo, quick_connect=args.quick_connect)
        return 0
    from . import proxy
    try:
        if args.cli == 'status':
            result = {'distros': proxy.list_distros()}
        else:
            if not args.distro or not args.user:
                parser.error('--distro 和 --user 为必填项')
            target = (args.distro, args.user)
            if args.cli == 'connect':
                result = proxy.connect(*target, family=args.family, codex=args.codex)
            elif args.cli == 'off':
                result = proxy.disable(*target, codex=args.codex)
            elif args.cli == 'scan':
                result = proxy.scan(*target, extended=args.extended)
            elif args.cli == 'apps':
                result = proxy.check_apps(*target)
            else:
                result = getattr(proxy, args.cli)(*target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
