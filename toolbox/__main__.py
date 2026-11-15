"""GUI by default; explicit --cli for scripts and diagnostics."""
import argparse
import json
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='WSL 与移动硬盘工具箱')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--demo', action='store_true', help='使用演示数据打开界面，不操作系统')
    mode.add_argument('--cli', choices=['status', 'scan', 'connect', 'off', 'rollback', 'diagnose'])
    parser.add_argument('--distro')
    parser.add_argument('--user')
    parser.add_argument('--family', choices=['SakuraCat', 'Clash Verge'])
    parser.add_argument('--codex', action='store_true')
    args = parser.parse_args(argv)
    if not args.cli:
        from .app import launch
        launch(demo=args.demo)
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
            else:
                result = getattr(proxy, args.cli)(*target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
